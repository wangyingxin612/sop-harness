import { useCallback, useEffect, useRef, useState } from "react";
import Chat from "./components/Chat.jsx";
import Inspector from "./components/Inspector.jsx";
import { createSession, exportUrl, listSops, sendMessage } from "./api.js";

// A caller who has already spent five turns establishing context should never
// be told to start over. Everything below exists to honour that: the
// transcript lives in the client, a failed turn keeps its message and offers
// a one-click retry, and a session the server has forgotten is re-created
// silently with the history still on screen.
const IDLE_NUDGE_MS = 45_000;

export default function App() {
  const [sops, setSops] = useState([]);
  const [sopName, setSopName] = useState("insurance_claims");
  const [consentScenario, setConsentScenario] = useState("default");
  const [apiKey, setApiKey] = useState("");
  const [showKeyField, setShowKeyField] = useState(false);

  const [sessionId, setSessionId] = useState(null);
  const [state, setState] = useState(null);
  const [messages, setMessages] = useState([]);
  const [streamingText, setStreamingText] = useState(null);
  const [busy, setBusy] = useState(false);
  const [turnError, setTurnError] = useState(null);   // { kind, retryable, message }
  const [notice, setNotice] = useState(null);
  const [idleNudge, setIdleNudge] = useState(false);

  const pendingMessage = useRef(null);   // the message a retry would resend
  const idleTimer = useRef(null);

  useEffect(() => {
    listSops()
      .then(setSops)
      .catch(() => setSops([{ name: "insurance_claims", display_name: "Insurance Claims Support" }]));
  }, []);

  // Idle handling. A real contact centre checks in rather than sitting
  // silently; this is the text equivalent, done client-side so an idle
  // caller costs nothing (no model call just to say "still there?").
  const resetIdleTimer = useCallback(() => {
    setIdleNudge(false);
    if (idleTimer.current) clearTimeout(idleTimer.current);
    const terminal = ["CLOSED", "HUMAN_HANDOFF", "ABUSE_TERMINATED"];
    if (!sessionId || busy || terminal.includes(state?.phase)) return;
    idleTimer.current = setTimeout(() => setIdleNudge(true), IDLE_NUDGE_MS);
  }, [sessionId, busy, state?.phase]);

  useEffect(() => {
    resetIdleTimer();
    return () => idleTimer.current && clearTimeout(idleTimer.current);
  }, [resetIdleTimer, messages.length]);

  async function startSession({ silent = false, keepMessages = false } = {}) {
    const s = await createSession({ sopName, consentScenario, apiKey });
    setSessionId(s.session_id);
    setState(s);
    if (!keepMessages) setMessages([]);
    setStreamingText(null);
    setTurnError(null);
    if (!silent) setNotice(null);
    return s;
  }

  async function handleNewSession() {
    setNotice(null);
    try {
      await startSession();
    } catch (e) {
      setTurnError({ kind: "internal", retryable: false, message: e.message });
    }
  }

  async function deliver(sid, text) {
    setStreamingText("");
    let failed = null;
    await sendMessage(sid, text, {
      onToken: (partial) => setStreamingText(partial),
      onDone: ({ reply, state: newState }) => {
        setStreamingText(null);
        setMessages((m) => [...m, { role: "agent", text: reply }]);
        setState(newState);
        pendingMessage.current = null;
      },
      onError: (err) => {
        setStreamingText(null);
        failed = err;
      },
    });
    return failed;
  }

  async function handleSend(text) {
    if (!sessionId || busy) return;
    setBusy(true);
    setTurnError(null);
    setNotice(null);
    setIdleNudge(false);
    pendingMessage.current = text;
    setMessages((m) => [...m, { role: "caller", text }]);

    try {
      let failed = await deliver(sessionId, text);

      // The one failure we can fully recover from on the caller's behalf:
      // the server no longer has this session (redeploy). Re-create it and
      // resend once, keeping the transcript on screen. The agent legitimately
      // starts over on its own state — including re-verifying identity, which
      // is correct and non-negotiable (DESIGN.md §9.2: memory may be retained,
      // trust may not) — but the caller doesn't lose the conversation.
      if (failed?.kind === "session_gone") {
        const s = await startSession({ silent: true, keepMessages: true });
        setNotice(
          "The server was redeployed, so the agent lost its place and will need to re-verify you. " +
            "Your conversation above is intact."
        );
        failed = await deliver(s.session_id, text);
      }

      if (failed) setTurnError(failed);
    } finally {
      setBusy(false);
    }
  }

  async function handleRetry() {
    const text = pendingMessage.current;
    if (!text || busy) return;
    // Remove the failed caller bubble before resending so the transcript
    // doesn't accumulate duplicates of the same message.
    setMessages((m) => {
      const last = m[m.length - 1];
      return last?.role === "caller" && last.text === text ? m.slice(0, -1) : m;
    });
    await handleSend(text);
  }

  const terminal = ["CLOSED", "HUMAN_HANDOFF", "ABUSE_TERMINATED"].includes(state?.phase);

  return (
    <>
      <header className="app-header">
        <div className="app-title">
          SOP Harness
          <small>Insurance Claims Support — demo</small>
        </div>

        <div className="field-group">
          <label>SOP</label>
          <select value={sopName} onChange={(e) => setSopName(e.target.value)} disabled={!!sessionId}>
            {sops.map((s) => (
              <option key={s.name} value={s.name}>{s.display_name}</option>
            ))}
          </select>
        </div>

        <div className="field-group">
          <label>Consent scenario</label>
          <select
            value={consentScenario}
            onChange={(e) => setConsentScenario(e.target.value)}
            disabled={!!sessionId}
          >
            <option value="default">default (approves)</option>
            <option value="timeout">timeout (never approves)</option>
          </select>
        </div>

        {showKeyField ? (
          <div className="field-group">
            <label>API key</label>
            <input
              type="password"
              placeholder="sk-ant-…"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              style={{ width: 160 }}
            />
          </div>
        ) : (
          <button className="btn" onClick={() => setShowKeyField(true)}>Use my own API key</button>
        )}

        <div className="spacer" />

        {sessionId && <span className="session-tag">session {sessionId}</span>}
        {sessionId && (
          <a className="btn" href={exportUrl(sessionId)} target="_blank" rel="noreferrer">
            Export transcript
          </a>
        )}
        <button className="btn btn-primary" onClick={handleNewSession}>
          {sessionId ? "New session" : "Start session"}
        </button>
      </header>

      {sopName === "bank_kyc" && (
        <div className="banner banner-warn">
          This SOP's phase/gate/directive config is real and independently loaded (see
          backend/sops/bank_kyc.yaml) — identity verification runs exactly as configured. Case resolution
          past that point still reads the insurance fixture data, since no bank domain adapter exists yet
          (see DESIGN.md §9.2). This option demonstrates that the control layer is vertical-agnostic, not
          a complete second product.
        </div>
      )}

      {notice && <div className="banner banner-info">{notice}</div>}

      <div className="main-layout">
        <Chat
          messages={messages}
          streamingText={streamingText}
          onSend={handleSend}
          onRetry={handleRetry}
          disabled={busy}
          sessionReady={!!sessionId}
          turnError={turnError}
          idleNudge={idleNudge}
          terminal={terminal}
          phase={state?.phase}
        />
        <Inspector state={state} />
      </div>
    </>
  );
}
