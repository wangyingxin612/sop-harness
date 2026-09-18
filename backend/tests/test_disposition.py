"""Disposition classification (app/sop/disposition.py)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.sop.disposition import (
    CONTAINED,
    unmapped_end_reasons,
    DISPOSITIONS,
    TRANSFERRED,
    _REASON_TO_CODE,
    classify,
    containment_rate,
)
from app.sop.types import END_REASONS, ConsentStatus, Phase, SessionState, Turn

APP = Path(__file__).resolve().parents[1] / "app"


def _session(**facts) -> SessionState:
    st = SessionState(session_id="s", sop_name="insurance_claims")
    for k, v in facts.items():
        setattr(st.facts, k, v)
    return st


def _with_phase_trace(st: SessionState, *phases: Phase) -> SessionState:
    for i, ph in enumerate(phases):
        st.transcript.append(
            Turn(turn_index=i, role="agent", text="x",
                 trace_event={"phase_before": ph.value, "phase_after": ph.value})
        )
    return st


# ---------------------------------------------------------------------------
# The guard that makes the module docstring's claim true: a new escalation
# reason must not silently fall through to a generic code.
# ---------------------------------------------------------------------------

def test_every_end_reason_in_the_registry_has_a_disposition():
    """The registry (types.END_REASONS) is the single source of truth for why
    a session ends. Adding one without deciding what it means operationally
    fails here.

    This replaced a test that scanned the source for `escalation_reason = "..."`
    assignments — which then missed a reason written as a ternary in the
    sweeper. The guard was as fragile as the thing it guarded; asking a
    registry is not."""
    assert unmapped_end_reasons() == set()


def test_every_assigned_reason_is_in_the_registry():
    """The other direction: code must not invent a reason the registry has
    never heard of, or it would classify as a generic transfer and nobody
    would notice.

    The match is bounded to the assignment EXPRESSION — a quoted literal, a
    parenthesised ternary, or a variable — rather than to a fixed window of
    characters after the name. A first attempt used `text[m.end():m.end()+200]`
    and picked up string literals from whatever happened to follow, which is
    the same class of mistake as a detector that fires on nearby text instead
    of on its target.
    """
    assign = re.compile(
        r"escalation_reason\s*=\s*"
        r"(\([^)]*\)"        # parenthesised expression, e.g. a ternary
        r'|"[a-z_]+"'         # a bare string literal
        r"|[A-Za-z_][\w.]*)"  # a variable or attribute
    )
    found = set()
    for path in APP.rglob("*.py"):
        for m in assign.finditer(path.read_text()):
            found |= set(re.findall(r'"([a-z_]+)"', m.group(1)))
    assert found, "scan found nothing — the pattern has drifted, fix the test"
    invented = found - set(END_REASONS)
    assert not invented, f"reasons assigned but not in END_REASONS: {sorted(invented)}"


def test_client_may_not_assert_a_server_only_reason():
    """A browser can say it gave up. It does not get to declare an identity
    failure — that is a conclusion only the server is entitled to reach."""
    from app.api.main import CLOSE_REASONS
    assert CLOSE_REASONS <= set(END_REASONS)
    assert "identity_verification_failed" not in CLOSE_REASONS
    assert "caller_window_closed" not in CLOSE_REASONS


def test_every_code_in_the_table_is_self_consistent():
    for key, spec in DISPOSITIONS.items():
        assert key == spec.code
        assert spec.label and spec.routing_hint


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def test_in_progress_while_the_conversation_is_live():
    st = _session()
    st.phase = Phase.PROCESS_CASE
    assert classify(st).code == "IN_PROGRESS"


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("caller_requested_human", "TRANSFERRED_CALLER_REQUEST"),
        ("identity_verification_failed", "TRANSFERRED_IDENTITY_FAILED"),
        ("repeated_off_topic_requests", "TRANSFERRED_OFF_TOPIC"),
        ("repeated_prompt_injection_attempts", "TRANSFERRED_INJECTION_ATTEMPTS"),
        ("agent_initiated_transfer", "TRANSFERRED_AGENT_JUDGEMENT"),
    ],
)
def test_handoff_reasons_map_to_codes(reason, expected):
    st = _session(escalation_reason=reason)
    st.phase = Phase.HUMAN_HANDOFF
    assert classify(st).code == expected


def test_outstanding_consent_reroutes_the_transfer():
    """The proximate reason is 'the agent decided', but the person who can
    unblock it sits in authorisations, not the general queue."""
    st = _session(escalation_reason="agent_initiated_transfer",
                  consent_status=ConsentStatus.TIMED_OUT)
    st.phase = Phase.HUMAN_HANDOFF
    d = classify(st)
    assert d.code == "TRANSFERRED_CONSENT_UNAVAILABLE"
    assert d.routing_hint == "authorisations"


def test_consent_reroute_does_not_override_a_security_outcome():
    """An injection-flagged transfer belongs with trust and safety even if a
    consent request happened to be outstanding — the security signal is the
    one that decides who should be reading this."""
    st = _session(escalation_reason="repeated_prompt_injection_attempts",
                  consent_status=ConsentStatus.TIMED_OUT)
    st.phase = Phase.HUMAN_HANDOFF
    assert classify(st).code == "TRANSFERRED_INJECTION_ATTEMPTS"


def test_abuse_termination():
    st = _session()
    st.phase = Phase.ABUSE_TERMINATED
    assert classify(st).code == "TERMINATED_ABUSE"


def test_silence_close_is_abandonment_not_containment():
    st = _session(escalation_reason="caller_inactive")
    st.phase = Phase.CLOSED
    d = classify(st)
    assert d.code == "ABANDONED_AFTER_SILENCE"
    assert d.outcome_class != CONTAINED


def test_closed_without_reaching_case_work():
    st = _session()
    st.phase = Phase.CLOSED
    _with_phase_trace(st, Phase.VERIFY_ID, Phase.RESOLVE_INTENT)
    assert classify(st).code == "CLOSED_BEFORE_CASE_WORK"


def test_closed_after_case_work_splits_on_the_summary_decision():
    for field, expected in (
        ("email_sent", "SELF_SERVED_SUMMARY_SENT"),
        ("email_skipped", "SELF_SERVED_SUMMARY_DECLINED"),
    ):
        st = _session(**{field: True})
        st.phase = Phase.CLOSED
        _with_phase_trace(st, Phase.PROCESS_CASE, Phase.POST_PROCESS)
        assert classify(st).code == expected


def test_case_worked_but_no_summary_decision_is_flagged_for_review():
    """R7 requires an explicit send/skip choice. Closing without one is a
    contained call that nonetheless failed a requirement, so it is contained
    AND flagged — collapsing those two facts into one would hide it."""
    st = _session()
    st.phase = Phase.CLOSED
    _with_phase_trace(st, Phase.PROCESS_CASE, Phase.POST_PROCESS)
    d = classify(st)
    assert d.code == "SELF_SERVED_NO_SUMMARY_DECISION"
    assert d.outcome_class == CONTAINED
    assert d.review_flag is True


def test_phase_reached_is_read_from_the_trace_not_the_current_phase():
    """A session sitting in CLOSED says nothing about whether a case was
    worked; only the trace does."""
    st = _session(email_sent=True)
    st.phase = Phase.CLOSED
    # no trace at all -> we cannot claim case work happened
    assert classify(st).code == "CLOSED_BEFORE_CASE_WORK"


# ---------------------------------------------------------------------------
# Containment
# ---------------------------------------------------------------------------

def test_containment_excludes_abandonment_and_abuse_from_both_sides():
    codes = [
        "SELF_SERVED_SUMMARY_SENT",      # contained
        "TRANSFERRED_CALLER_REQUEST",    # transferred
        "ABANDONED_AFTER_SILENCE",       # neither
        "TERMINATED_ABUSE",              # neither
    ]
    assert containment_rate(codes) == 0.5


def test_containment_is_none_without_data():
    assert containment_rate([]) is None
    assert containment_rate(["ABANDONED_AFTER_SILENCE"]) is None


def test_containment_ignores_in_progress_sessions():
    assert containment_rate(["IN_PROGRESS", "SELF_SERVED_SUMMARY_SENT"]) == 1.0


# ---------------------------------------------------------------------------
# Close-reason integrity. Found in a live run: the close endpoint defaulted to
# "caller_inactive", so any close that did not say why was filed as an
# abandonment — a completed call reading as a caller who walked away.
# ---------------------------------------------------------------------------

def test_close_requires_an_explicit_reason():
    from fastapi.testclient import TestClient
    from app.api.main import app
    from app.session.store import STORE
    from app.sop.types import Turn

    c = TestClient(app)
    sid = c.post("/api/sessions", json={"sop_name": "insurance_claims"}).json()["session_id"]
    assert c.post(f"/api/sessions/{sid}/close").status_code == 422
    assert c.post(f"/api/sessions/{sid}/close", params={"reason": "made_up"}).status_code == 422

    st = STORE.get(sid)
    st.phase = Phase.POST_PROCESS
    st.facts.email_sent = True
    st.transcript.append(
        Turn(turn_index=0, role="agent", text="x",
             trace_event={"phase_before": "PROCESS_CASE", "phase_after": "POST_PROCESS"})
    )
    STORE.update(st)

    body = c.post(f"/api/sessions/{sid}/close", params={"reason": "caller_finished"}).json()
    assert body["phase"] == "CLOSED"
    # The call was completed, so it must classify on what happened in it —
    # not as an abandonment just because it was closed from outside.
    assert body["disposition"]["code"] == "SELF_SERVED_SUMMARY_SENT"
    assert body["disposition"]["outcome_class"] == CONTAINED
