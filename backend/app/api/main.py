"""FastAPI app (DESIGN.md §10). Serves the API and, in the Docker image,
the built frontend static files too — one container, one process.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from app.config import Settings, load_settings
from app.llm.provider import LLMProvider
from app.obs.export import render_transcript_html
from app.obs.idle_metrics import build_report, record_event
from app.session.orchestrator import run_turn
from app.session.store import STORE
from app.sop.domain import DomainContext, load_domain
from app.sop.spec import SopSpec, load_spec
from app.sop.types import Phase, SessionState
from app.api.schemas import serialize_state

BACKEND_DIR = Path(__file__).resolve().parents[2]
SOPS_DIR = BACKEND_DIR / "sops"
FIXTURES_DIR = BACKEND_DIR / "fixtures"
CONSENT_PATH = str(FIXTURES_DIR / "consent_scenarios.json")

app = FastAPI(title="SOP Harness API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # demo scope — a real deployment would scope this to the frontend origin
    allow_methods=["*"],
    allow_headers=["*"],
)

_SETTINGS = load_settings()
_SPEC_CACHE: dict[str, SopSpec] = {}
_DOMAIN_CACHE: dict[str, DomainContext] = {}


def _get_spec(sop_name: str) -> SopSpec:
    if sop_name not in _SPEC_CACHE:
        path = SOPS_DIR / f"{sop_name}.yaml"
        if not path.exists():
            raise HTTPException(404, f"unknown SOP: {sop_name}")
        _SPEC_CACHE[sop_name] = load_spec(path)
    return _SPEC_CACHE[sop_name]


def _get_domain() -> DomainContext:
    # Single fixture set for this build; keyed for future multi-SOP fixture sets.
    if "default" not in _DOMAIN_CACHE:
        _DOMAIN_CACHE["default"] = load_domain(FIXTURES_DIR, now=date.fromisoformat(_SETTINGS.demo_now))
    return _DOMAIN_CACHE["default"]


def _provider_for(session_id: str) -> LLMProvider:
    override = STORE.api_key_for(session_id)
    if override:
        settings = Settings(
            anthropic_api_key=override,
            model_strong=_SETTINGS.model_strong,
            model_fast=_SETTINGS.model_fast,
            demo_now=_SETTINGS.demo_now,
            llm_base_url=_SETTINGS.llm_base_url,
        )
        return LLMProvider(settings)
    if not _SETTINGS.anthropic_api_key:
        raise HTTPException(
            400,
            "No API key configured. Set ANTHROPIC_API_KEY on the server, or pass api_key when creating a session.",
        )
    return LLMProvider(_SETTINGS)


def _classify_error(exc: Exception) -> dict:
    """Turn an exception into something the UI can act on. Every branch
    answers two questions for the user: what happened, and what can they do
    about it."""
    import anthropic

    if isinstance(exc, anthropic.AuthenticationError):
        return {
            "kind": "auth",
            "retryable": False,
            "message": "The API key was rejected. Enter a valid key with “Use my own API key”, "
                       "or set ANTHROPIC_API_KEY on the server.",
        }
    if isinstance(exc, anthropic.RateLimitError):
        return {
            "kind": "rate_limit",
            "retryable": True,
            "message": "The model is rate-limited right now. Your message is still here — retry in a moment.",
        }
    if isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return {
            "kind": "network",
            "retryable": True,
            "message": "Couldn’t reach the model. Your message is still here — retry when you’re ready.",
        }
    if isinstance(exc, anthropic.APIStatusError):
        return {
            "kind": "upstream",
            "retryable": True,
            "message": f"The model service returned an error ({exc.status_code}). Your message is still here.",
        }
    return {
        "kind": "internal",
        "retryable": True,
        "message": "Something went wrong handling that turn. Your message and everything before it are intact.",
    }


class CreateSessionRequest(BaseModel):
    sop_name: str = "insurance_claims"
    consent_scenario: str = "default"
    api_key: str | None = None


class ClientEventRequest(BaseModel):
    event: str
    payload: dict = {}


class MessageRequest(BaseModel):
    message: str


@app.get("/api/health")
def health():
    return {"status": "ok", "has_server_api_key": bool(_SETTINGS.anthropic_api_key)}


@app.get("/api/sops")
def list_sops():
    return [
        {"name": p.stem, "display_name": load_spec(p).display_name}
        for p in sorted(SOPS_DIR.glob("*.yaml"))
    ]


@app.post("/api/sessions")
def create_session(req: CreateSessionRequest):
    spec = _get_spec(req.sop_name)  # validates the SOP exists
    session_id = str(uuid.uuid4())[:8]
    state = SessionState(session_id=session_id, sop_name=req.sop_name, consent_scenario=req.consent_scenario)
    STORE.create(state, api_key=req.api_key)
    return serialize_state(state, _get_spec(state.sop_name))


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str):
    state = STORE.get(session_id)
    if state is None:
        raise HTTPException(404, "session not found")
    return serialize_state(state, _get_spec(state.sop_name))


@app.get("/api/sessions/{session_id}/export", response_class=HTMLResponse)
def export_session(session_id: str):
    state = STORE.get(session_id)
    if state is None:
        raise HTTPException(404, "session not found")
    return render_transcript_html(state)


@app.post("/api/sessions/{session_id}/close")
def close_session(session_id: str, reason: str = "caller_inactive"):
    """Close a session without a model call.

    An abandoned call still has to END — leaving it open forever is how a
    contact centre loses a line and an audit trail gains a conversation with
    no conclusion. The close is deterministic (no model, no cost): the state
    machine moves to CLOSED and the reason is recorded, which is exactly the
    kind of decision DESIGN.md §4.1 says belongs in code rather than in a
    model's judgement."""
    state = STORE.get(session_id)
    if state is None:
        raise HTTPException(404, "session_not_found")
    if state.phase not in (Phase.CLOSED, Phase.HUMAN_HANDOFF, Phase.ABUSE_TERMINATED):
        state.phase = Phase.CLOSED
        state.facts.escalation_reason = reason
        STORE.update(state)
    return serialize_state(state, _get_spec(state.sop_name))


@app.post("/api/sessions/{session_id}/events")
def post_client_event(session_id: str, req: ClientEventRequest):
    """Sink for the few facts only the browser knows.

    Whether a tab is visible, and whether someone was mid-sentence when we
    nudged them, are not observable server-side — and they are exactly the
    facts needed to tell a well-timed nudge from a rude one. Unknown event
    names are dropped rather than rejected: an older client that has been
    left open in a tab should not start throwing errors because the server
    moved on."""
    state = STORE.get(session_id)
    if state is None:
        raise HTTPException(404, "session_not_found")
    payload = dict(req.payload)
    spec = _get_spec(state.sop_name)
    phase_spec = spec.phase_spec(state.phase)
    # Stamp the phase and effort level server-side. The client could send
    # them, but then the metric would be reporting the client's belief about
    # the policy rather than the policy — and the whole point is to find out
    # whether the policy is right.
    payload.setdefault("phase", state.phase.value)
    if phase_spec is not None:
        payload.setdefault("response_effort", phase_spec.response_effort)
    record_event(session_id, req.event, payload)
    return {"ok": True}


@app.get("/api/metrics/idle")
def idle_metrics():
    """Whether the current idle budget is actually right — see
    app/obs/idle_metrics.py for what the two rates mean and which way each
    one should move the setting."""
    terminal_phases = (Phase.CLOSED, Phase.HUMAN_HANDOFF, Phase.ABUSE_TERMINATED)
    terminal_by_session: dict[str, bool] = {}
    for sid in STORE.list_sessions():
        st = STORE.get(sid)
        if st is not None:
            terminal_by_session[sid] = st.phase in terminal_phases
    return build_report(terminal_by_session)


@app.post("/api/sessions/{session_id}/messages")
def post_message(session_id: str, req: MessageRequest):
    """SSE stream: the reply is generated and guard-checked synchronously
    (DESIGN.md §7.11 — nothing is committed to the caller before the guard
    passes), then delivered progressively for a natural typing feel. This is
    NOT raw incremental token streaming with mid-stream abort — that
    architecture is described in DESIGN.md §7.11 as future work; what's
    delivered here already satisfies the guard-before-emit safety property,
    which is the property that actually matters."""
    state = STORE.get(session_id)
    if state is None:
        # After a redeploy the on-disk state is gone too (new machine, new
        # filesystem). Say so precisely rather than leaving a dead UI.
        raise HTTPException(404, "session_not_found")

    spec = _get_spec(state.sop_name)
    domain = _get_domain()
    provider = _provider_for(session_id)

    def event_stream():
        try:
            result = run_turn(state, req.message, domain, spec, provider, CONSENT_PATH)
        except Exception as exc:  # noqa: BLE001
            # A failed turn must never cost the caller their conversation.
            # The error is classified so the UI can say something actionable
            # and, crucially, so it knows whether offering "Retry" is honest:
            # the session state was not mutated (run_turn works on a copy and
            # only the store.update below commits it), so retrying the same
            # message is safe and idempotent.
            yield f"event: error\ndata: {json.dumps(_classify_error(exc))}\n\n"
            return

        STORE.update(result.state)
        STORE.append_trace(session_id, result.trace_event)

        words = result.reply.split(" ")
        buffer = ""
        for i, w in enumerate(words):
            buffer += (" " if buffer else "") + w
            yield f"event: token\ndata: {json.dumps({'text': buffer})}\n\n"
            time.sleep(0.02)

        done_payload = {"reply": result.reply, "state": serialize_state(result.state, spec), "trace_event": result.trace_event}
        yield f"event: done\ndata: {json.dumps(done_payload, default=str)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# --- static frontend, served in the Docker image (DESIGN.md §10) ---
_FRONTEND_DIST = BACKEND_DIR.parent / "frontend" / "dist"
if _FRONTEND_DIST.exists():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="frontend")
