import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Inactivity, as a ladder with consequences — checking in, warning, closing.
 *
 * ONE CLOCK, ONE OWNER. An earlier version ran three: a nudge clock that
 * paused when the tab was hidden, a ceiling clock that did not, and a
 * server-side sweep. On top of that the budget was assembled in the browser
 * from a server base plus estimated reading time plus a complexity allowance
 * counted off question marks and list items. The extra terms moved a
 * ninety-second budget by roughly ten seconds, which is precision theatre on
 * a number nobody had calibrated, and the split ownership meant the answer to
 * "when does this session end?" lived in three places that could disagree.
 *
 * Now the server owns the timeout — `idle_policy.seconds` for the current
 * phase, and a hard per-SOP ceiling — and it also owns the actual closing
 * (app/session/sweeper.py), which is what makes closing the tab work at all.
 * The client's job is narrower and honest: show the caller where they are in
 * that budget, and report activity.
 *
 * Stages 1 and 2 are local text with no model call — an idle caller should
 * cost nothing. Stage 3 asks the server to close, and the server decides.
 */

// Multiples of the phase budget. The gap before closing is wider than the gap
// before the first nudge: a caller who is still there has now been given a
// reason to reply, and one who has gone is not coming back, so the extra
// patience costs almost nothing.
const WARN_AT = 1.6;
const CLOSE_AT = 2.4;

const DEFAULT_SECONDS = 150;

export function idleCopy(level, secondsUntilClose) {
  if (level === 1) return "Still there? No rush — I'll hold your place.";
  if (level === 2) {
    // "Shortly" is not an answer to "how long do I have?".
    const s = Math.max(5, Math.round((secondsUntilClose ?? 60) / 5) * 5);
    const when = s >= 90 ? `about ${Math.round(s / 60)} minutes` : `about ${s} seconds`;
    return `I'll close this out in ${when} if you're done — type anything to keep going.`;
  }
  return "";
}

export const IDLE_COPY = { 1: idleCopy(1), 2: idleCopy(2, 60) };

/**
 * @param active      run the ladder (false in terminal phases)
 * @param idlePolicy  { seconds, max_session_idle_seconds } from the server
 * @param onExpire    called once when the ladder reaches stage 3
 * @param onEvent     instrumentation sink
 */
export function useIdleLadder({ active, idlePolicy, onExpire, onEvent }) {
  const [level, setLevel] = useState(0);
  const [secondsUntilClose, setSecondsUntilClose] = useState(null);

  const lastActivity = useRef(Date.now());
  const levelRef = useRef(0);
  const firedExpire = useRef(false);
  const nudgedAt = useRef(null);

  const onExpireRef = useRef(onExpire);
  onExpireRef.current = onExpire;
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  const budgetMs = (idlePolicy?.seconds ?? DEFAULT_SECONDS) * 1000;
  const ceilingMs = (idlePolicy?.max_session_idle_seconds ?? 900) * 1000;

  const setLevelBoth = useCallback((l) => {
    levelRef.current = l;
    setLevel(l);
  }, []);

  const reset = useCallback(() => {
    // A caller who types after being nudged is a nudge we should not have
    // sent. That is the false-positive signal, and it is only observable
    // here, at the moment of recovery.
    if (levelRef.current > 0 && nudgedAt.current != null) {
      onEventRef.current?.("idle_nudge_recovered", {
        level: levelRef.current,
        ms_after_nudge: Date.now() - nudgedAt.current,
      });
    }
    nudgedAt.current = null;
    lastActivity.current = Date.now();
    firedExpire.current = false;
    setSecondsUntilClose(null);
    setLevelBoth(0);
  }, [setLevelBoth]);

  useEffect(() => {
    if (!active) {
      setLevelBoth(0);
      return;
    }
    const tick = setInterval(() => {
      const idle = Date.now() - lastActivity.current;
      const limit = Math.min(budgetMs * CLOSE_AT, ceilingMs);
      setSecondsUntilClose(Math.max(0, Math.round((limit - idle) / 1000)));

      let next = 0;
      if (idle >= limit) next = 3;
      else if (idle >= budgetMs * WARN_AT) next = 2;
      else if (idle >= budgetMs) next = 1;
      if (next === levelRef.current) return;

      if (next === 1 || next === 2) {
        if (nudgedAt.current == null) nudgedAt.current = Date.now();
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
    }, 1000);
    return () => clearInterval(tick);
  }, [active, budgetMs, ceilingMs, setLevelBoth]);

  return { level, reset, budgetSeconds: Math.round(budgetMs / 1000), secondsUntilClose };
}
