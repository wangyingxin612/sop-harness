"""Session persistence round-trip (DESIGN.md §9.3, revised).

These tests exist because of a real production failure: with two machines
behind a load balancer and an in-memory store, a mid-conversation request
routed to the other machine 404'd and the caller had no way back. The
property that matters is not "we can write JSON" — it's that a session
rehydrated from disk is indistinguishable from the one that was lost,
including the bits that are easy to drop in serialization (enum identity,
the Slot supersede chain, pending actions, consent evidence).
"""
from app.session.persistence import session_from_dict, session_to_dict
from app.session.store import SessionStore
from app.sop.machine import decide
from app.sop.types import (
    CaseHint,
    ConsentStatus,
    PendingAction,
    Phase,
    ScopeRing,
    SessionState,
    Slot,
    TurnSignals,
    VerificationStatus,
)


def signals(**overrides) -> TurnSignals:
    base = dict(scope=ScopeRing.CORE, turn_index=0, raw_message="")
    base.update(overrides)
    return TurnSignals(**base)


def test_round_trip_preserves_a_mid_conversation_session(domain, spec):
    state = SessionState(session_id="rt1", sop_name="insurance_claims")
    msg = "Margaret Chen, DOB 1985-03-15, SSN last four 4472, denied healthcare claim from January."
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
    assert state.phase == Phase.PROCESS_CASE

    restored = session_from_dict(session_to_dict(state))

    assert restored.phase == Phase.PROCESS_CASE
    assert restored.facts.verification_status == VerificationStatus.VERIFIED
    assert restored.facts.verified_party_id == "P9"
    assert restored.facts.matched_factor_types == state.facts.matched_factor_types
    assert restored.memory.confirmed_case_id == "CL-2048"
    assert [h.case_type for h in restored.memory.case_hints] == ["healthcare"]
    assert restored.memory.identity_slots["dob"].value == "1985-03-15"
    assert restored.memory.identity_slots["dob"].verbatim_quote == msg


def test_round_trip_preserves_the_supersede_chain():
    """Self-correction provenance is the subtlest thing to lose in
    serialization — and losing it would silently break both the audit
    trail and the inspector's "(corrected)" marker."""
    state = SessionState(session_id="rt2", sop_name="insurance_claims")
    state.memory.set_identity_factor("dob", Slot(value="1985-03-16", verbatim_quote="born 85-03-16", turn_index=0))
    state.memory.set_identity_factor("dob", Slot(value="1985-03-15", verbatim_quote="sorry, the 15th", turn_index=0))

    restored = session_from_dict(session_to_dict(state))
    slot = restored.memory.identity_slots["dob"]
    assert slot.value == "1985-03-16"              # original preserved
    assert slot.current().value == "1985-03-15"    # correction preserved
    assert slot.superseded_by is not None


def test_round_trip_preserves_action_gate_evidence():
    state = SessionState(session_id="rt3", sop_name="insurance_claims", phase=Phase.POST_PROCESS)
    state.facts.pending_action = PendingAction(action_type="send_summary_email", requested_at_turn=4)
    state.facts.consent_status = ConsentStatus.APPROVED
    state.memory.consent_events.append(
        {"action_type": "send_summary_email", "decision": "approved", "turn_index": 5, "quote": "yes please"}
    )

    restored = session_from_dict(session_to_dict(state))
    assert restored.facts.pending_action.action_type == "send_summary_email"
    assert restored.facts.consent_status == ConsentStatus.APPROVED
    assert restored.memory.consent_events[-1]["quote"] == "yes please"


def test_store_rehydrates_after_the_process_loses_memory(tmp_path, domain, spec):
    """The actual production failure, reproduced: a second process (or the
    same process after a restart) must be able to serve a session it has
    never seen in memory."""
    store_a = SessionStore(runs_dir=tmp_path)
    state = SessionState(session_id="shared1", sop_name="insurance_claims")
    state, _ = decide(
        state,
        signals(identity_candidates={"full_name": "Margaret Chen", "dob": "1985-03-15", "id_last4": "4472"}),
        domain,
        spec,
    )
    store_a.update(state)

    store_b = SessionStore(runs_dir=tmp_path)          # a different machine / a cold restart
    recovered = store_b.get("shared1")
    assert recovered is not None
    assert recovered.facts.verified_party_id == "P9"
    assert recovered.phase == Phase.RESOLVE_INTENT


def test_unknown_session_is_still_a_clean_miss(tmp_path):
    assert SessionStore(runs_dir=tmp_path).get("never-existed") is None


def test_corrupt_state_file_does_not_crash_the_server(tmp_path):
    store = SessionStore(runs_dir=tmp_path)
    (tmp_path / "broken.state.json").write_text("{not json at all")
    assert store.get("broken") is None


def test_list_sessions_includes_persisted_not_just_live(tmp_path, domain, spec):
    store = SessionStore(runs_dir=tmp_path)
    store.update(SessionState(session_id="s1", sop_name="insurance_claims"))
    fresh = SessionStore(runs_dir=tmp_path)
    assert "s1" in fresh.list_sessions()
