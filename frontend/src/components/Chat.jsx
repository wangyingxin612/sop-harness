import { useEffect, useRef, useState } from "react";

const SUGGESTIONS = [
  "I'm the policyholder. Margaret Chen, policy POL-9921, DOB 1985-03-15, SSN last four 4472. Calling about my denied healthcare claim from January.",
  "I already told you who I am. This is ridiculous. Just tell me why my claim was denied.",
  "I'm David Chen, calling about my mom Margaret Chen's claim.",
];

export default function Chat({ messages, streamingText, onSend, disabled, sessionReady }) {
  const [input, setInput] = useState("");
  const scrollRef = useRef(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, streamingText]);

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
        {messages.map((m, i) => (
          <div key={i} className={`msg-row ${m.role}`}>
            <div className="bubble">{m.text}</div>
          </div>
        ))}
        {streamingText !== null && (
          <div className="msg-row agent">
            <div className="bubble streaming">{streamingText}</div>
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
      <form className="chat-input-row" onSubmit={submit}>
        <input
          type="text"
          placeholder={sessionReady ? "Type a message…" : "Create a session first"}
          value={input}
          disabled={!sessionReady || disabled}
          onChange={(e) => setInput(e.target.value)}
        />
        <button type="submit" className="btn btn-primary" disabled={!sessionReady || disabled}>
          Send
        </button>
      </form>
    </div>
  );
}
