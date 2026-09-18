import { useCallback, useEffect, useRef, useState } from "react";
import Chat from "./components/Chat.jsx";
import Inspector from "./components/Inspector.jsx";
import { closeSession, createSession, exportUrl, listSops, sendMessage, reportEvent } from "./api.js";
import { useIdleLadder } from "./useIdleLadder.js";

// A caller who has already spent five turns establishing context should never
// be told to start over. Everything below exists to honour that: the
// transcript lives in the client, a failed turn keeps its message and offers
// a one-click retry, and a session the server has forgotten is re-created
// silently with the history still on screen.

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

  const pendingMessage = useRef(null);   // the message a retry would resend

  // Dark by default — this is an operations instrument, not a consumer chat
  // app — but the preference is remembered and respected.
  const [theme, setTheme] = useState(() => localStorage.getItem("sop-theme") || "dark");
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem("sop-theme", theme); } catch { /* private mode */ }
  }, [theme]);

  useEffect(() => {
    listSops()
      .then(setSops)
      .catch(() => setSops([{ name: "insurance_claims", display_name: "Insurance Claims Support" }]));
  }, []);

  const terminal = ["CLOSED", "HUMAN_HANDOFF", "ABUSE_TERMINATED"].includes(state?.phase);

  const handleIdleExpire = useCallback(async () => {
    if (!sessionId) return;
    const closed = await closeSession(sessionId, "caller_inactive");
    if (closed) setState(closed);
    setNotice("This call was closed after a long silence. The transcript and audit trail are complete.");
  }, [sessionId]);

  // What the caller is currently looking at — the reply whose reading time
  // the idle budget has to cover.
  const lastAgentText = [...messages].reverse().find((m) => m.role === "agent")?.text ?? "";

  const handleIdleEvent = useCallback(
    (event, payload) => reportEvent(sessionId, event, payload),
    [sessionId]
  );

  const {
    level: idleLevel,
    reset: resetIdle,
    budgetSeconds,
    secondsUntilClose,
  } = useIdleLadder({
    active: !!sessionId && !busy && !terminal,
    idlePolicy: state?.idle_policy,
    lastAgentText,
    onExpire: handleIdleExpire,
    onEvent: handleIdleEvent,
  });

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
    resetIdle();
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

  return (
    <>
      <header className="app-header">
        <div className="brand">
          <span className="brand-mark">SOP</span>
          <span className="brand-text">
            <strong>SOP Harness</strong>
            <span>Insurance claims · enforced procedure</span>
          </span>
        </div>

        {/* Both of these are locked once a call is under way — you can't swap
            the procedure or the fixture mid-conversation. Previously they were
            just `disabled`, which reads as broken rather than as deliberate,
            so a locked control now says so and says how to unlock it. */}
        {sessionId ? (
          <div className="locked-setting" title="Settings are fixed for the duration of a call — start a new session to change them">
            <span className="locked-setting-val">{sops.find((s) => s.name === sopName)?.display_name || sopName}</span>
            <span className="locked-setting-val">
              consent: {consentScenario === "default" ? "approves" : "never approves"}
            </span>
            <span className="locked-setting-hint">locked for this call</span>
          </div>
        ) : (
          <>
            <div className="field-group">
              <label title="Which procedure the agent must follow. Each SOP is a YAML spec — see backend/sops/">
                Procedure
              </label>
              <select value={sopName} onChange={(e) => setSopName(e.target.value)}>
                {sops.map((s) => (
                  <option key={s.name} value={s.name}>{s.display_name}</option>
                ))}
              </select>
            </div>

            <div className="field-group">
              <label title="A test fixture, not a live system: when an authorised representative asks to discuss someone else's claim, does the policyholder's consent come back approved, or never arrive? Pick 'never approves' to see the agent degrade gracefully instead of stalling.">
                Consent test ⓘ
              </label>
              <select value={consentScenario} onChange={(e) => setConsentScenario(e.target.value)}>
                <option value="default">approves on 2nd check</option>
                <option value="timeout">never approves</option>
              </select>
            </div>
          </>
        )}

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
          <button className="btn" onClick={() => setShowKeyField(true)} title="Use your own Anthropic API key for this session">Own key</button>
        )}

        <div className="spacer" />

        {sessionId && <span className="session-tag">session {sessionId}</span>}
        {sessionId && (
          <a className="btn" href={exportUrl(sessionId)} target="_blank" rel="noreferrer">
            Export
          </a>
        )}
        <button
          className="btn btn-icon"
          onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
          title={theme === "dark" ? "Switch to light" : "Switch to dark"}
        >
          {theme === "dark" ? "☾" : "☀"}
        </button>
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
          onActivity={resetIdle}
          disabled={busy}
          sessionReady={!!sessionId}
          turnError={turnError}
          idleLevel={idleLevel}
          idleSecondsUntilClose={secondsUntilClose}
          terminal={terminal}
          phase={state?.phase}
        />
        <Inspector state={state} idleBudgetSeconds={budgetSeconds} />
      </div>
    </>
  );
}
