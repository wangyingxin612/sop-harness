"""Tests for the handoff packet (DESIGN.md §7.6: 'no transfer is ever
empty-handed'). LLM-free."""
from app.sop.handoff import build_handoff_packet
from app.sop.machine import decide
from app.sop.types import CaseHint, Phase, ScopeRing, SessionState, TurnSignals


def make_state(**kwargs) -> SessionState:
    return SessionState(session_id="t", sop_name="insurance_claims", **kwargs)


def signals(**overrides) -> TurnSignals:
    base = dict(scope=ScopeRing.CORE, turn_index=0, raw_message="")
    base.update(overrides)
    return TurnSignals(**base)


def test_packet_reflects_unverified_state_on_lockout(domain, spec):
    state = make_state()
    state, _ = decide(state, signals(identity_candidates={"dob": "1999-01-01"}), domain, spec)
    state, _ = decide(state, signals(identity_candidates={"phone": "555-000-0000"}, turn_index=1), domain, spec)
    assert state.phase == Phase.HUMAN_HANDOFF

    packet = build_handoff_packet(state, domain)
    assert packet.verified is False
    assert packet.reason == "identity_verification_failed"
    assert "Re-verify manually" in packet.recommended_next_step
    assert packet.case_id is None


def test_packet_includes_verified_identity_and_case_on_explicit_request(domain, spec):
    state = make_state()
    msg = "denied healthcare claim from January"
    state, _ = decide(
        state,
        signals(
            identity_candidates={"full_name": "Margaret Chen", "dob": "1985-03-15", "id_last4": "4472"},
            case_hint=CaseHint(case_type="healthcare", status="denied", time_ref="January", verbatim_quote=msg),
            raw_message=msg,
        ),
        domain,
        spec,
    )
    state, _ = decide(state, signals(confirms_proposed_case=True, turn_index=1), domain, spec)
    state, _ = decide(state, signals(escalation_request=True, turn_index=2), domain, spec)
    assert state.phase == Phase.HUMAN_HANDOFF

    packet = build_handoff_packet(state, domain)
    assert packet.verified is True
    assert set(packet.verification_method) == {"full_name", "dob", "id_last4"}
    assert packet.case_id == "CL-2048"
    assert packet.reason == "caller_requested_human"
    assert "no persuasion needed" in packet.recommended_next_step


def test_packet_captures_attempted_paths_and_emotion(domain, spec):
    state = make_state()
    state, _ = decide(state, signals(refusal=True, intensity=3), domain, spec)
    state, _ = decide(state, signals(escalation_request=True, turn_index=1), domain, spec)
    packet = build_handoff_packet(state, domain)
    assert packet.emotional_state == "escalated / abusive language"


def test_packet_included_in_turn_plan_visible_facts(domain, spec):
    state = make_state()
    state, plan = decide(state, signals(escalation_request=True), domain, spec)
    assert plan.phase == Phase.HUMAN_HANDOFF
    assert plan.visible_facts["handoff_packet"] is not None
    assert plan.visible_facts["handoff_packet"]["reason"] == "caller_requested_human"


def test_packet_includes_the_triggering_message_itself(domain, spec):
    """The message must be captured even though it hasn't been appended to
    `transcript` yet at the point resolve() runs (see
    SessionState.current_raw_message)."""
    state = make_state()
    state, plan = decide(
        state, signals(escalation_request=True, raw_message="get me a human right now"), domain, spec
    )
    assert plan.visible_facts["handoff_packet"]["caller_last_message"] == "get me a human right now"


def test_no_packet_for_abuse_terminated(domain, spec):
    """The packet is specific to a HUMAN handoff; abuse termination doesn't hand off to anyone."""
    state = make_state()
    for i in range(4):
        state, plan = decide(state, signals(scope=ScopeRing.OUT, turn_index=i), domain, spec)
    assert plan.phase == Phase.ABUSE_TERMINATED
    assert "handoff_packet" not in plan.visible_facts
