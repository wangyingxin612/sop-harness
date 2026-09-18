"""The identity gate closes behind you.

The Inspector shows a red "N more lock the session" warning while a caller is
being verified, and drops it to a neutral audit note once they are through.
That is only honest if the lock genuinely cannot fire afterwards — otherwise
the UI would be reassuring someone who was still at risk.

This pins the invariant the interface relies on, rather than trusting that
the phase branch in machine.transition() stays the only caller of
_identity_phase_transition.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.main import app
from app.sop.machine import decide
from app.sop.types import (
    CallerRole,
    Phase,
    ScopeRing,
    SessionState,
    TurnSignals,
    VerificationStatus,
)


def _verified_state() -> SessionState:
    st = SessionState(session_id="s", sop_name="insurance_claims", phase=Phase.RESOLVE_INTENT)
    st.facts.verification_status = VerificationStatus.VERIFIED
    st.facts.verified_party_id = "P9"
    st.facts.caller_role = CallerRole.POLICYHOLDER
    st.facts.matched_factor_count = 3
    st.facts.matched_factor_types = ["full_name", "dob", "id_last4"]
    st.facts.mismatch_count = 1          # one wrong answer on the way in
    return st


def _signals(**kw) -> TurnSignals:
    base = dict(scope=ScopeRing.CORE, turn_index=0, raw_message="")
    base.update(kw)
    return TurnSignals(**base)


def test_wrong_details_after_verification_do_not_add_mismatches(domain, spec):
    """A verified caller mentioning a wrong date in passing — a typo, or the
    wrong year on a bill they're reading out — must not creep toward a lock."""
    state = _verified_state()
    for _ in range(4):
        state, _ = decide(
            state,
            _signals(identity_candidates={"dob": "1900-01-01"}, raw_message="sorry, 1900?"),
            domain, spec,
        )
    assert state.facts.mismatch_count == 1
    assert state.facts.verification_status == VerificationStatus.VERIFIED
    assert state.phase != Phase.HUMAN_HANDOFF


def test_verified_session_never_reaches_the_lock(domain, spec):
    state = _verified_state()
    state.facts.mismatch_count = spec.max_mismatches - 1   # one away from the threshold
    for _ in range(5):
        state, _ = decide(
            state,
            _signals(identity_candidates={"id_last4": "0000"}, raw_message="0000"),
            domain, spec,
        )
    assert state.facts.verification_status != VerificationStatus.LOCKED


def test_identity_thresholds_are_published_not_hardcoded():
    """The UI prints the mismatch budget. It has to come from the SOP, or a
    vertical with a different threshold would see a confidently wrong number."""
    c = TestClient(app)
    body = c.post("/api/sessions", json={"sop_name": "insurance_claims"}).json()
    pol = body["identity_policy"]
    assert pol["max_mismatches"] == 2
    assert pol["min_distinct_factors"] == 3
    assert "id_last4" in pol["factor_types"]


def test_a_different_sop_publishes_its_own_thresholds():
    c = TestClient(app)
    body = c.post("/api/sessions", json={"sop_name": "bank_kyc"}).json()
    assert body["identity_policy"]["min_distinct_factors"] == 3
