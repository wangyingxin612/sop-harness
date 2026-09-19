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
from app.session.sweeper import idle_seconds, sweep
from app.sop.disposition import DISPOSITIONS, classify, containment_rate
from app.sop.domain import DomainContext, load_domain
from app.sop.spec import SopSpec, load_spec
from app.sop.machine import end_session, poll_pending_consent
from app.sop.types import CLIENT_CLOSE_REASONS, ConsentStatus, Phase, SessionState
from app.api.schemas import serialize_state

BACKEND_DIR = Path(__file__).resolve().parents[2]
SOPS_DIR = BACKEND_DIR / "sops"
FIXTURES_DIR = BACKEND_DIR / "fixtures"
CONSENT_PATH = str(FIXTURES_DIR / "consent_scenarios.json")

# Total wall time spent revealing a reply, however long it is. Short enough
# that it reads as "the message arrived" rather than "the system is slow",
# long enough that text does not simply flash into existence.
REVEAL_BUDGET_S = 0.6

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
    return serialize_state(state, _get_spec(state.sop_name), _get_domain())


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str):
    state = STORE.get(session_id)
    if state is None:
        raise HTTPException(404, "session not found")
    return serialize_state(state, _get_spec(state.sop_name), _get_domain())


@app.get("/api/sessions/{session_id}/export", response_class=HTMLResponse)
def export_session(session_id: str):
    state = STORE.get(session_id)
    if state is None:
        raise HTTPException(404, "session not found")
    return render_transcript_html(state)


# Declared on the reason itself (types.EndReason.client_assertable) rather
# than listed again here. A browser may report that it gave up; it does not
# get to declare an identity failure — that is the server's conclusion.
CLOSE_REASONS = CLIENT_CLOSE_REASONS


@app.post("/api/sessions/{session_id}/close")
def close_session(session_id: str, reason: str):
    """Close a session without a model call.

    An abandoned call still has to END — leaving it open forever is how a
    contact centre loses a line and an audit trail gains a conversation with
    no conclusion. The close is deterministic (no model, no cost): the state
    machine moves to CLOSED and the reason is recorded, which is exactly the
    kind of decision DESIGN.md §4.1 says belongs in code rather than in a
    model's judgement.

    `reason` is REQUIRED. It used to default to "caller_inactive", which was
    a quiet data-integrity bug: every close that forgot to say why was filed
    as an abandonment, so a completed call that happened to be closed by some
    other path would show up on the operations board as a caller who walked
    away. A default value on a field that becomes a business metric is a way
    of guessing, and this one guessed wrong in the direction that flatters
    nothing and confuses everyone."""
    state = STORE.get(session_id)
    if state is None:
        raise HTTPException(404, "session_not_found")
    if reason not in CLOSE_REASONS:
        raise HTTPException(422, f"unknown close reason: {reason!r}; expected one of {sorted(CLOSE_REASONS)}")
    if state.phase not in (Phase.CLOSED, Phase.HUMAN_HANDOFF, Phase.ABUSE_TERMINATED):
        state.phase = end_session(state, reason, len(state.transcript))
        STORE.update(state)
    return serialize_state(state, _get_spec(state.sop_name), _get_domain())


@app.post("/api/sessions/{session_id}/consent/poll")
def poll_consent(session_id: str):
    """Advance a pending third-party consent request on wall-clock time.

    This endpoint exists because the original implementation contradicted its
    own rationale. `machine.poll_pending_consent` argues — correctly —
    that "a real asynchronous approval doesn't wait for an agent to decide
    it's time to check; it resolves on its own schedule and the agent
    observes the result". But it was only ever called from `transition()`,
    which means it advanced once per CALLER TURN. A representative who asked
    for consent and then sat quietly — the single most likely thing for
    someone to do while waiting on someone else's authorisation — would wait
    forever, and the timeout scenario could only be reached by typing filler
    messages at the agent.

    So the clock is now genuinely a clock. No model call: this is an
    observation of external state, which is control-plane work (§4.1), and
    charging a caller tokens for the passage of time would be absurd."""
    state = STORE.get(session_id)
    if state is None:
        raise HTTPException(404, "session_not_found")
    before = state.facts.consent_status
    if state.facts.consent_status == ConsentStatus.PENDING:
        poll_pending_consent(state, _get_domain())
        if state.facts.consent_status != before:
            STORE.update(state)
        else:
            STORE.update(state)      # poll_count moved even when status didn't
    return {
        "changed": state.facts.consent_status != before,
        "state": serialize_state(state, _get_spec(state.sop_name), _get_domain()),
    }


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
    # A closing tab is the one signal the server can never observe for
    # itself, and it is the difference between "walked away mid-conversation"
    # and "closed the tab and left". Recorded as evidence on the session; it
    # only becomes a conclusion if the session then stays silent past its
    # ceiling (see session/sweeper.py).
    if req.event == "session_window_closed" and not state.facts.window_closed:
        state.facts.window_closed = True
        STORE.update(state)
    payload.setdefault("phase", state.phase.value)
    if phase_spec is not None:
        payload.setdefault("response_effort", phase_spec.response_effort)
    record_event(session_id, req.event, payload)
    return {"ok": True}


@app.get("/api/evals/report", response_class=HTMLResponse)
def eval_report(report: str = "latest.json"):
    """The eval suite rendered as a standalone page.

    Served rather than only written to disk so the artifact has a URL: the
    person who needs to read it — a compliance or operations reviewer — is
    not going to clone the repo, and "trust me, the tests pass" is not a
    claim anyone should accept about a system that handles protected health
    information."""
    from evals.html_report import render
    return render(report)


def _ts(value: str):
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _talk_time_s(state: SessionState) -> int | None:
    """First caller message to last caller message.

    This is the number that belongs in an average handle time. Measuring to
    the CLOSE instead would fold in the idle ceiling — so an abandoned call
    would report fifteen minutes of "handling" during which nothing happened,
    and the metric would get worse every time we made the bot more patient."""
    callers = [t for t in state.transcript if t.role == "caller"]
    if len(callers) < 2:
        return 0 if callers else None
    first, last = _ts(callers[0].ts), _ts(callers[-1].ts)
    return round((last - first).total_seconds()) if first and last else None


def _span_s(state: SessionState) -> int | None:
    """Session open to last activity of any kind — including the silence at
    the end. Useful for capacity, misleading for handle time."""
    start = _ts(state.created_at)
    last = _ts(state.transcript[-1].ts) if state.transcript else start
    return round((last - start).total_seconds()) if start and last else None


@app.get("/api/sessions")
def list_sessions():
    """Every session this deployment knows about, live or on disk.

    The operations board is the reason this exists. A single conversation
    demonstrates that the SOP works; a buyer's operations lead is asking a
    different question — across everything that ran today, where did calls
    stop, and which ones need a person? That question is unanswerable from
    inside one chat window, and it is the question the product is actually
    bought to answer.

    Deliberately a summary per session, not full state: the board should
    stay cheap to open when there are a thousand rows, and anything a
    reviewer needs beyond this is one click away in the export."""
    # Balance the books before reporting them. Sessions whose caller closed
    # the window have nothing left running to close them — see
    # session/sweeper.py for why this is lazy rather than a background timer.
    sweep(STORE, _get_spec)

    rows = []
    skipped_empty = 0
    for sid in STORE.list_sessions():
        st = STORE.get(sid)
        if st is None:
            continue
        # A session row is created the moment someone opens the page, before
        # anyone has said anything. Those are not calls, and showing them
        # makes the board read as a wall of stalled verifications when in
        # fact nobody ever spoke. Counted, not listed.
        if not st.transcript:
            skipped_empty += 1
            continue
        d = classify(st)
        last_caller = next((t.text for t in reversed(st.transcript) if t.role == "caller"), "")
        last_ts = st.transcript[-1].ts if st.transcript else st.created_at
        rows.append({
            "session_id": sid,
            "sop_name": st.sop_name,
            "phase": st.phase.value,
            "created_at": st.created_at,
            "last_activity_at": last_ts,
            "turns_used": st.facts.turns_used,
            # Two durations, because they answer different questions and the
            # difference between them is our latency plus the caller's
            # patience, not their engagement. `talk_time_s` is first caller
            # message to last caller message — the part of the call the
            # caller was actually in. `span_s` runs to the close, which for
            # an abandoned session includes the whole idle ceiling and would
            # wreck an average handle time if reported as the same thing.
            "talk_time_s": _talk_time_s(st),
            "span_s": _span_s(st),
            "idle_s": round(idle_seconds(st) or 0),
            "cost_usd": round(st.facts.cost_usd, 5),
            "verified": st.facts.is_verified(),
            "caller_role": st.facts.caller_role.value,
            "peak_intensity": st.facts.peak_intensity,
            "consent_status": st.facts.consent_status.value,
            "case_id": st.memory.active_case_id or st.memory.confirmed_case_id,
            "resolved_intent": st.memory.resolved_intent,
            "last_caller_message": last_caller[:160],
            "disposition": d.as_dict(),
        })
    rows.sort(key=lambda r: r["last_activity_at"], reverse=True)

    codes = [r["disposition"]["code"] for r in rows]
    by_class: dict[str, int] = {}
    for c in codes:
        cls = DISPOSITIONS[c].outcome_class if c in DISPOSITIONS else "unknown"
        by_class[cls] = by_class.get(cls, 0) + 1

    return {
        "sessions": rows,
        "rollup": {
            "total": len(rows),
            "by_outcome_class": by_class,
            "containment_rate": containment_rate(codes),
            "needs_review": sum(1 for r in rows if r["disposition"]["review_flag"]),
            "opened_never_started": skipped_empty,
            "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        },
    }


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

        # Progressive reveal of an ALREADY-GENERATED reply. Nothing is
        # emitted until the guard has passed (§7.11's no-retraction rule), so
        # this is a presentation effect, not real token streaming.
        #
        # It used to sleep a flat 20ms per word, which quietly made the
        # effect a LATENCY TAX proportional to reply length: a 113-word
        # answer spent 2.3s dribbling out text the server already had, on top
        # of the real 6.6s it took to produce. The longest replies — the ones
        # the caller is already waiting hardest for — were penalised most.
        #
        # The budget is now fixed, so reveal time is constant regardless of
        # length and long replies simply reveal faster.
        words = result.reply.split(" ")
        per_word = min(0.02, REVEAL_BUDGET_S / max(len(words), 1))
        buffer = ""
        for w in words:
            buffer += (" " if buffer else "") + w
            yield f"event: token\ndata: {json.dumps({'text': buffer})}\n\n"
            time.sleep(per_word)

        done_payload = {"reply": result.reply, "state": serialize_state(result.state, spec, domain), "trace_event": result.trace_event}
        yield f"event: done\ndata: {json.dumps(done_payload, default=str)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# --- static frontend, served in the Docker image (DESIGN.md §10) ---
_FRONTEND_DIST = BACKEND_DIR.parent / "frontend" / "dist"
if _FRONTEND_DIST.exists():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="frontend")
