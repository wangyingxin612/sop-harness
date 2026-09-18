import { useEffect, useRef, useState } from "react";
import { pollConsent } from "./api.js";

/**
 * Watches a pending third-party authorisation on wall-clock time.
 *
 * Before this existed, consent only advanced when the caller sent a message.
 * That is the wrong way round: a representative who has just asked us to get
 * their mother's authorisation is, overwhelmingly likely, about to sit still
 * and wait. They would have waited forever, and the only way to reach the
 * timeout branch was to type filler at the agent until it gave up.
 *
 * The poll is deliberately cheap and model-free — it observes external state,
 * which is control-plane work. Nobody should be charged tokens for the
 * passage of time.
 */
export function useConsentWatch({ sessionId, status, policy, onResolved, onTick }) {
  const [checking, setChecking] = useState(false);
  const onResolvedRef = useRef(onResolved);
  onResolvedRef.current = onResolved;
  const onTickRef = useRef(onTick);
  onTickRef.current = onTick;

  const intervalSeconds = policy?.poll_interval_seconds ?? 8;
  const pending = status === "pending";

  useEffect(() => {
    if (!sessionId || !pending) {
      setChecking(false);
      return;
    }
    let cancelled = false;

    const id = setInterval(async () => {
      // A visible "checking…" beat, so the strip is not a static label that
      // happens to change. The caller should be able to see that something
      // is actually happening on their behalf.
      setChecking(true);
      const res = await pollConsent(sessionId);
      if (cancelled) return;
      setTimeout(() => !cancelled && setChecking(false), 700);
      if (!res) return;
      onTickRef.current?.(res.state);
      if (res.changed) onResolvedRef.current?.(res.state);
    }, intervalSeconds * 1000);

    return () => {
      cancelled = true;
      clearInterval(id);
      setChecking(false);
    };
  }, [sessionId, pending, intervalSeconds]);

  return { checking };
}
