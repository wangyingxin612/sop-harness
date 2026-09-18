import { useEffect, useRef, useState } from "react";
import { idleCopy } from "../useIdleLadder.js";
import ConsentStrip from "./ConsentStrip.jsx";

const SUGGESTIONS = [
  "I'm the policyholder. Margaret Chen, policy POL-9921, DOB 1985-03-15, SSN last four 4472. Calling about my denied healthcare claim from January.",
  "I already told you who I am. This is ridiculous. Just tell me why my claim was denied.",
  "I'm David Chen, calling about my mom Margaret Chen's claim.",
];

const TERMINAL_COPY = {
  CLOSED: "This conversation is complete.",
  HUMAN_HANDOFF: "Handed off to a human representative — see the packet in the inspector.",
  ABUSE_TERMINATED: "This session was closed after repeated off-topic requests.",
};

export default function Chat({
  messages,
  streamingText,
  onSend,
  onRetry,
  onActivity,
  disabled,
  sessionReady,
  turnError,
  idleLevel,
  consentStatus,
  consentPolicy,
  consentChecking,
  idleSecondsUntilClose,
  terminal,
  phase,
}) {
  const [input, setInput] = useState("");
  const scrollRef = useRef(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, streamingText, turnError, idleLevel]);

  function submit(e) {
    e?.preventDefault();
    const text = input.trim();
    if (!text || disabled) return;
    onSend(text);
    setInput("");
  }

  return (
    <div className="chat-pane">
      <div className="chat-messages" ref={scrollRef}>
        {messages.length === 0 && (
          <div className="empty-hint" style={{ padding: "20px 4px" }}>
            {sessionReady
              ? "Start the conversation — try the identity verification example below, or type your own."
              : "Create a session to begin."}
          </div>
        )}
        {messages.map((m, i) =>
          m.role === "system" ? (
            <ConsentStrip key={i} status={m.status} resolved />
          ) : (
            <div key={i} className={`msg-row ${m.role}`}>
              <div className="bubble">{m.text}</div>
            </div>
          )
        )}
        {/* Pinned at the bottom only while it is LIVE. A pending
            authorisation is something happening now and the caller should be
            able to see it without scrolling; a resolved one is history and is
            rendered above, at the point it happened. */}
        {consentStatus === "pending" && (
          <ConsentStrip status={consentStatus} policy={consentPolicy} checking={consentChecking} />
        )}

        {streamingText !== null && (
          <div className="msg-row agent">
            <div className="bubble streaming">{streamingText}</div>
          </div>
        )}

        {/* A failed turn keeps the caller's message on screen and offers one
            click to resend it. The alternative — a dead UI — costs them the
            whole conversation, which is the single worst outcome here. */}
        {turnError && (
          <div className="turn-error">
            <div className="turn-error-msg">{turnError.message}</div>
            {turnError.retryable && (
              <button className="btn btn-primary btn-sm" onClick={onRetry} disabled={disabled}>
                Retry that message
              </button>
            )}
          </div>
        )}

        {/* The nudge is keyed to the idle LADDER, and typing counts as
            activity — the first version told people who were mid-sentence to
            please start typing, and then did nothing at all afterwards. */}
        {idleLevel > 0 && idleLevel < 3 && !turnError && !terminal && (
          <div className="msg-row agent">
            <div className={`bubble idle-nudge ${idleLevel === 2 ? "urgent" : ""}`}>
              {idleCopy(idleLevel, idleSecondsUntilClose)}
            </div>
          </div>
        )}
      </div>

      {messages.length === 0 && sessionReady && (
        <div className="chat-hint">
          Try:
          {SUGGESTIONS.map((s, i) => (
            <button key={i} onClick={() => onSend(s)}>
              {s.length > 46 ? s.slice(0, 46) + "…" : s}
            </button>
          ))}
        </div>
      )}

      {terminal ? (
        <div className="chat-terminal">{TERMINAL_COPY[phase] || "This conversation has ended."}</div>
      ) : (
        <form className="chat-input-row" onSubmit={submit}>
          <input
            type="text"
            placeholder={sessionReady ? "Type a message…" : "Create a session first"}
            value={input}
            disabled={!sessionReady || disabled}
            onChange={(e) => {
              setInput(e.target.value);
              onActivity?.();     // typing IS activity, not just sending
            }}
            onFocus={() => onActivity?.()}
          />
          <button type="submit" className="btn btn-primary" disabled={!sessionReady || disabled}>
            {disabled ? "…" : "Send"}
          </button>
        </form>
      )}
    </div>
  );
}
