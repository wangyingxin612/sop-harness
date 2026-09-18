const BASE = "/api";

export class SessionGoneError extends Error {
  constructor() {
    super("session_not_found");
    this.kind = "session_gone";
    this.retryable = true;
  }
}

export async function listSops() {
  const r = await fetch(`${BASE}/sops`);
  return r.json();
}

export async function createSession({ sopName, consentScenario, apiKey }) {
  const r = await fetch(`${BASE}/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      sop_name: sopName,
      consent_scenario: consentScenario,
      api_key: apiKey || null,
    }),
  });
  if (!r.ok) throw new Error((await r.json()).detail || "failed to create session");
  return r.json();
}

export async function getSession(sessionId) {
  const r = await fetch(`${BASE}/sessions/${sessionId}`);
  if (r.status === 404) throw new SessionGoneError();
  return r.json();
}

/**
 * Streams a message turn via SSE. `onToken` fires for each partial reply
 * chunk, `onDone` fires once with the full { reply, state, trace_event }.
 * fetch()+ReadableStream rather than EventSource, because EventSource can't
 * send a POST body (DESIGN.md §7.11's streaming design; see api/main.py).
 *
 * Errors are surfaced as structured objects ({kind, retryable, message}),
 * never as a dead stream — the caller's message is the most expensive thing
 * in the app and losing it is the worst outcome available to us.
 */
export async function sendMessage(sessionId, message, { onToken, onDone, onError }) {
  let resp;
  try {
    resp = await fetch(`${BASE}/sessions/${sessionId}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
  } catch {
    onError?.({
      kind: "network",
      retryable: true,
      message: "Lost connection to the server. Your message is still here — retry when you're ready.",
    });
    return;
  }

  if (resp.status === 404) {
    onError?.({
      kind: "session_gone",
      retryable: true,
      message: "This session is no longer on the server (it was likely redeployed).",
    });
    return;
  }
  if (!resp.ok || !resp.body) {
    onError?.({
      kind: "upstream",
      retryable: true,
      message: `The server returned an error (${resp.status}). Your message is still here.`,
    });
    return;
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let sawDone = false;

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let sepIndex;
      while ((sepIndex = buffer.indexOf("\n\n")) !== -1) {
        const rawEvent = buffer.slice(0, sepIndex);
        buffer = buffer.slice(sepIndex + 2);
        const lines = rawEvent.split("\n");
        let eventName = "message";
        let data = "";
        for (const line of lines) {
          if (line.startsWith("event: ")) eventName = line.slice(7).trim();
          else if (line.startsWith("data: ")) data += line.slice(6);
        }
        if (!data) continue;
        const parsed = JSON.parse(data);
        if (eventName === "token") onToken?.(parsed.text);
        else if (eventName === "done") {
          sawDone = true;
          onDone?.(parsed);
        } else if (eventName === "error") onError?.(parsed);
      }
    }
  } catch {
    onError?.({
      kind: "network",
      retryable: true,
      message: "The connection dropped mid-reply. Your message is still here — retry when you're ready.",
    });
    return;
  }

  if (!sawDone) {
    // The stream ended without a completion event — e.g. the server was
    // stopped mid-turn. Treat it as a failure rather than silently leaving
    // a half-typed reply on screen.
    onError?.({
      kind: "interrupted",
      retryable: true,
      message: "The reply was cut off before it finished. Your message is still here — retry to get a complete answer.",
    });
  }
}

export async function closeSession(sessionId, reason = "caller_inactive") {
  const r = await fetch(`${BASE}/sessions/${sessionId}/close?reason=${encodeURIComponent(reason)}`, {
    method: "POST",
  });
  if (!r.ok) return null;
  return r.json();
}

export function exportUrl(sessionId) {
  return `${BASE}/sessions/${sessionId}/export`;
}
