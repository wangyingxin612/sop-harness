const BASE = "/api";

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
  return r.json();
}

/**
 * Streams a message turn via SSE. `onToken` fires for each partial reply
 * chunk, `onDone` fires once with the full { reply, state, trace_event }.
 * fetch()+ReadableStream rather than EventSource, because EventSource can't
 * send a POST body (DESIGN.md §7.11's streaming design; see api/main.py).
 */
export async function sendMessage(sessionId, message, { onToken, onDone, onError }) {
  const resp = await fetch(`${BASE}/sessions/${sessionId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  if (!resp.ok || !resp.body) {
    onError?.(new Error("request failed"));
    return;
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

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
      else if (eventName === "done") onDone?.(parsed);
      else if (eventName === "error") onError?.(new Error(parsed.message));
    }
  }
}

export function exportUrl(sessionId) {
  return `${BASE}/sessions/${sessionId}/export`;
}
