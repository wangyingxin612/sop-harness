import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Inactivity handling, as a ladder with actual consequences.
 *
 * The first version of this was half a feature: one nudge at 45s, no reset
 * when the caller started typing (so it sat there telling someone who was
 * mid-sentence to please type), and nothing after it — the conversation just
 * stopped, open forever, with no conclusion in the audit trail.
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
 * Any activity at all — typing included, not just sending — resets it.
 */
const STAGES = [
  { at: 45_000, level: 1 },
  { at: 105_000, level: 2 },
  { at: 165_000, level: 3 },
];

export const IDLE_COPY = {
  1: "Still there? No rush — I'll hold your place.",
  2: "I'll close this out shortly if you're done. Type anything to keep going.",
};

export function useIdleLadder({ active, onExpire }) {
  const [level, setLevel] = useState(0);
  const timers = useRef([]);
  const onExpireRef = useRef(onExpire);
  onExpireRef.current = onExpire;

  const clear = useCallback(() => {
    timers.current.forEach(clearTimeout);
    timers.current = [];
  }, []);

  const reset = useCallback(() => {
    clear();
    setLevel(0);
    if (!active) return;
    timers.current = STAGES.map(({ at, level: l }) =>
      setTimeout(() => {
        setLevel(l);
        if (l === 3) onExpireRef.current?.();
      }, at)
    );
  }, [active, clear]);

  useEffect(() => {
    reset();
    return clear;
  }, [reset, clear]);

  return { level, reset };
}
