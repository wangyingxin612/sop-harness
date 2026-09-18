import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Inactivity handling, as a ladder with actual consequences.
 *
 * The first version of this was half a feature: one nudge at a flat 45s, no
 * reset when the caller started typing (so it sat there telling someone who
 * was mid-sentence to please type), and nothing after it — the conversation
 * just stopped, open forever, with no conclusion in the audit trail.
 *
 * A contact centre does three things instead, and so does this:
 *
 *   1. checks in                 "still there?"
 *   2. warns before acting       "I'll close this out shortly"
 *   3. actually closes           and records why
 *
 * Stages 1 and 2 are client-side text with no model call — an idle caller
 * should cost nothing. Stage 3 hits a deterministic close endpoint; no model
 * is asked whether to hang up.
 *
 * ---------------------------------------------------------------------------
 * WHY THE BUDGET IS COMPUTED RATHER THAN CONFIGURED
 *
 * A single fixed number is wrong in both directions at once. 45 seconds is
 * rude after "do you have the pathology report from your January visit?" —
 * the caller is walking to a filing cabinet. It is also *slow* after "was
 * that a yes?". The wait has to be a function of what we just asked someone
 * to do, so it is built from three parts:
 *
 *   base       what the SOP says this phase's typical question costs to
 *              answer (`response_effort` — see backend/app/sop/spec.py).
 *              Server-owned, because it is policy.
 *   reading    how long OUR OWN last message takes to read. We do not get
 *              to start the clock on a reply the caller has not finished
 *              reading yet — a long one buys its own grace.
 *   complexity how many separate things the reply asked for. Two questions
 *              and a list of documents is not one question.
 *
 * ---------------------------------------------------------------------------
 * WHY THERE ARE TWO CLOCKS
 *
 * The nudge clock PAUSES when the tab is hidden, and grants fresh reading
 * time when it comes back — someone who switched to their email to find a
 * claim number is doing exactly what we asked, and nudging a tab nobody is
 * looking at is both useless and, when they return to three stacked
 * "still there?" bubbles, insulting.
 *
 * But if that were the only clock, a hidden tab would keep a VERIFIED
 * session open indefinitely, which is a security problem, not a UX one. So
 * the ceiling clock (`max_session_idle_seconds`, per-SOP) runs on wall time,
 * never pauses, and is not negotiable. That is why the bank SOP sets it
 * shorter than the claims SOP: the difference between those two verticals
 * is a security ceiling, not a patience level.
 */

// Deliberate reading speed for consequential text — slower than the ~240wpm
// usually quoted for skimming prose, because people re-read the sentence
// with the deadline in it.
const MS_PER_WORD = 300;
const MAX_READING_MS = 45_000;     // beyond this the reply is too long anyway
const MAX_COMPLEXITY_MS = 60_000;

// Multiples of the computed budget. The gap between a nudge and a close is
// wider than the gap to the first nudge: having been nudged once, a caller
// who is genuinely still there has been given a reason to reply, and a
// caller who has gone is not coming back — so the cost of waiting longer
// falls almost entirely on sessions that are already over.
const LEVEL_2_FACTOR = 1.6;
const LEVEL_3_FACTOR = 2.4;

const DEFAULT_BASE_SECONDS = 150;

// "Shortly" is not an answer to "how long do I have?". Once we have decided
// to close the session we owe the caller the actual number, the same way a
// queue that tells you the wait is easier to sit through than one that says
// "soon".
export function idleCopy(level, secondsUntilClose) {
  if (level === 1) return "Still there? No rush — I'll hold your place.";
  if (level === 2) {
    const s = Math.max(5, Math.round((secondsUntilClose ?? 60) / 5) * 5);
    const when = s >= 90 ? `about ${Math.round(s / 60)} minutes` : `about ${s} seconds`;
    return `I'll close this out in ${when} if you're done — type anything to keep going.`;
  }
  return "";
}

// Kept for tests and any caller that only wants the static shape.
export const IDLE_COPY = {
  1: idleCopy(1),
  2: "I'll close this out shortly if you're done. Type anything to keep going.",
};

function wordCount(text) {
  if (!text) return 0;
  return text.trim().split(/\s+/).filter(Boolean).length;
}

export function readingMs(text) {
  return Math.min(wordCount(text) * MS_PER_WORD, MAX_READING_MS);
}

/**
 * Extra time for a reply that asked for more than one thing. Counted from
 * the SHAPE of the text rather than its meaning: asking a model "how hard is
 * this to answer?" would cost a call and a round trip to produce a number
 * we only need to be roughly right.
 */
export function complexityMs(text) {
  if (!text) return 0;
  const questions = (text.match(/\?/g) || []).length;
  const listItems = (text.match(/^\s*(?:[-•*]|\d+[.)])\s+/gm) || []).length;
  const extraQuestions = Math.max(0, questions - 1) * 15_000;
  const items = listItems * 10_000;
  return Math.min(extraQuestions + items, MAX_COMPLEXITY_MS);
}

export function computeBudgetMs({ baseSeconds, lastAgentText }) {
  const base = (baseSeconds ?? DEFAULT_BASE_SECONDS) * 1000;
  return base + readingMs(lastAgentText) + complexityMs(lastAgentText);
}

/**
 * @param active          run the ladder at all (false in terminal phases)
 * @param idlePolicy      { base_seconds, max_session_idle_seconds } from the server
 * @param lastAgentText   the reply the caller is currently looking at
 * @param onExpire        called once, when the ladder reaches stage 3
 * @param onEvent         instrumentation sink (see App.jsx)
 */
export function useIdleLadder({ active, idlePolicy, lastAgentText, onExpire, onEvent }) {
  const [level, setLevel] = useState(0);
  const [secondsUntilClose, setSecondsUntilClose] = useState(null);

  const nudgeElapsed = useRef(0);      // pauses when the tab is hidden
  const ceilingStart = useRef(Date.now());  // wall clock, never pauses
  const lastTick = useRef(Date.now());
  const levelRef = useRef(0);
  const firedExpire = useRef(false);
  const nudgedAt = useRef(null);       // for nudge-accuracy instrumentation

  const onExpireRef = useRef(onExpire);
  onExpireRef.current = onExpire;
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  const budgetMs = computeBudgetMs({
    baseSeconds: idlePolicy?.base_seconds,
    lastAgentText,
  });
  const ceilingMs = (idlePolicy?.max_session_idle_seconds ?? 900) * 1000;

  const setLevelBoth = useCallback((l) => {
    levelRef.current = l;
    setLevel(l);
  }, []);

  const reset = useCallback(
    ({ hard = true } = {}) => {
      // A caller who typed after being nudged is a nudge we should not have
      // sent — that is the false-positive signal, and it is only observable
      // here, at the moment of recovery.
      if (levelRef.current > 0 && nudgedAt.current != null) {
        onEventRef.current?.("idle_nudge_recovered", {
          level: levelRef.current,
          ms_after_nudge: Date.now() - nudgedAt.current,
        });
      }
      nudgedAt.current = null;
      nudgeElapsed.current = 0;
      setSecondsUntilClose(null);
      lastTick.current = Date.now();
      // `hard` distinguishes real activity (which also restarts the security
      // ceiling) from merely returning to the tab (which does not).
      if (hard) {
        ceilingStart.current = Date.now();
        firedExpire.current = false;
      }
      setLevelBoth(0);
    },
    [setLevelBoth]
  );

  // Restart the budget whenever a new reply lands — new text, new reading time.
  useEffect(() => {
    reset();
  }, [lastAgentText, reset]);

  useEffect(() => {
    if (!active) {
      setLevelBoth(0);
      return;
    }

    function onVisibility() {
      if (document.hidden) {
        // Freeze the nudge clock. The ceiling clock keeps running.
        nudgeElapsed.current += Date.now() - lastTick.current;
        lastTick.current = Date.now();
      } else {
        // Back on screen, and they have not read the reply yet. Give the
        // reading time back rather than nudging them a second later.
        nudgeElapsed.current = 0;
        lastTick.current = Date.now();
        if (levelRef.current > 0 && levelRef.current < 3) setLevelBoth(0);
      }
    }

    const tick = setInterval(() => {
      const now = Date.now();
      if (!document.hidden) {
        nudgeElapsed.current += now - lastTick.current;
      }
      lastTick.current = now;

      const idle = nudgeElapsed.current;
      const sinceActivity = now - ceilingStart.current;

      // Whichever limit bites first is the one the caller should be told about.
      const untilLadderClose = budgetMs * LEVEL_3_FACTOR - idle;
      const untilCeiling = ceilingMs - sinceActivity;
      setSecondsUntilClose(Math.max(0, Math.round(Math.min(untilLadderClose, untilCeiling) / 1000)));

      // The ceiling is absolute: hidden tab, long reply, doesn't matter.
      if (sinceActivity >= ceilingMs && !firedExpire.current) {
        firedExpire.current = true;
        onEventRef.current?.("idle_ceiling_reached", { seconds: Math.round(sinceActivity / 1000) });
        setLevelBoth(3);
        onExpireRef.current?.();
        return;
      }

      let next = 0;
      if (idle >= budgetMs * LEVEL_3_FACTOR) next = 3;
      else if (idle >= budgetMs * LEVEL_2_FACTOR) next = 2;
      else if (idle >= budgetMs) next = 1;

      if (next !== levelRef.current) {
        if (next === 1 || next === 2) {
          if (nudgedAt.current == null) nudgedAt.current = now;
          onEventRef.current?.("idle_nudge_shown", {
            level: next,
            budget_seconds: Math.round(budgetMs / 1000),
          });
        }
        if (next === 3 && !firedExpire.current) {
          firedExpire.current = true;
          onEventRef.current?.("idle_expired", { budget_seconds: Math.round(budgetMs / 1000) });
          setLevelBoth(3);
          onExpireRef.current?.();
          return;
        }
        setLevelBoth(next);
      }
    }, 1000);

    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      clearInterval(tick);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [active, budgetMs, ceilingMs, setLevelBoth]);

  return {
    level,
    reset,
    budgetSeconds: Math.round(budgetMs / 1000),
    secondsUntilClose,
  };
}
