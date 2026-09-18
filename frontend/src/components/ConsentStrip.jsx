/**
 * The state of a third-party authorisation, shown to the caller as a SYSTEM
 * strip rather than as a message from the agent.
 *
 * That distinction is deliberate. "Still waiting on your mother's
 * authorisation" is a fact about an external system, not something the agent
 * said, and letting the agent narrate it would mean either a model call every
 * few seconds or a templated line pretending to be one. The strip is styled
 * to read as machinery, and the agent's own replies stay things the agent
 * actually chose to say.
 */
const COPY = {
  pending: {
    title: "Authorisation requested",
    body: "We've asked the policyholder to approve sharing the full detail. You can keep talking in the meantime — I just can't go into specifics until it comes back.",
  },
  approved: {
    title: "Authorisation received",
    body: "The policyholder approved the request. Full claim detail is now available on this call.",
  },
  timed_out: {
    title: "No response to the authorisation request",
    body: "We didn't hear back in time. I can still help with status and next steps, but the detail stays restricted until authorisation is on file.",
  },
  declined: {
    title: "Authorisation declined",
    body: "The policyholder declined the request, so the detail stays restricted.",
  },
};

export default function ConsentStrip({ status, policy, checking }) {
  if (!status || status === "not_requested") return null;
  const copy = COPY[status];
  if (!copy) return null;

  const done = policy?.checks_done ?? 0;
  const total = policy?.checks_before_timeout ?? 0;

  return (
    <div className={`consent-strip consent-${status}`}>
      <div className="consent-head">
        <span className="consent-title">{copy.title}</span>
        {status === "pending" && (
          <span className={`consent-check ${checking ? "active" : ""}`}>
            {checking ? "checking…" : "waiting"}
          </span>
        )}
      </div>
      <div className="consent-body">{copy.body}</div>
      {status === "pending" && total > 0 && (
        /* A bounded wait, not an indefinite one. Someone waiting on another
           person's approval should be able to see that the waiting ends. */
        <div className="consent-progress" title={`checked ${done} of ${total} times before we stop`}>
          {Array.from({ length: total }, (_, i) => (
            <span key={i} className={`tick ${i < done ? "done" : ""}`} />
          ))}
          <span className="consent-count">
            checked {done}/{total}
          </span>
        </div>
      )}
    </div>
  );
}
