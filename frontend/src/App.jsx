import { useEffect, useState } from "react";
import Chat from "./components/Chat.jsx";
import Inspector from "./components/Inspector.jsx";
import { createSession, exportUrl, listSops, sendMessage } from "./api.js";

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
  const [error, setError] = useState(null);

  useEffect(() => {
    listSops().then(setSops).catch(() => setSops([{ name: "insurance_claims", display_name: "Insurance Claims Support" }]));
  }, []);

  async function handleNewSession() {
    setError(null);
    try {
      const s = await createSession({ sopName, consentScenario, apiKey });
      setSessionId(s.session_id);
      setState(s);
      setMessages([]);
      setStreamingText(null);
    } catch (e) {
      setError(e.message);
    }
  }

  async function handleSend(text) {
    if (!sessionId || busy) return;
    setBusy(true);
    setError(null);
    setMessages((m) => [...m, { role: "caller", text }]);
    setStreamingText("");
    try {
      await sendMessage(sessionId, text, {
        onToken: (partial) => setStreamingText(partial),
        onDone: ({ reply, state: newState }) => {
          setStreamingText(null);
          setMessages((m) => [...m, { role: "agent", text: reply }]);
          setState(newState);
        },
        onError: (e) => {
          setStreamingText(null);
          setError(e.message);
        },
      });
    } finally {
      setBusy(false);
    }
  }

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
          <select value={consentScenario} onChange={(e) => setConsentScenario(e.target.value)} disabled={!!sessionId}>
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

      {error && (
        <div style={{ background: "var(--bad-soft)", color: "var(--bad)", padding: "8px 20px", fontSize: 12.5 }}>
          {error}
        </div>
      )}

      <div className="main-layout">
        <Chat
          messages={messages}
          streamingText={streamingText}
          onSend={handleSend}
          disabled={busy}
          sessionReady={!!sessionId}
        />
        <Inspector state={state} />
      </div>
    </>
  );
}
