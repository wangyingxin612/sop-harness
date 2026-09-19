"""Tests for the state-transition half of DECIDE (DESIGN.md §7.4, machine.py).

Pure deterministic tests: TurnSignals are hand-authored, exactly the way an
extraction call's structured output would look, so these exercise the same
contract PERCEIVE must satisfy without needing a model in the loop.
"""
import pytest

from app.sop.machine import decide, transition
from app.sop.types import (
    CallerRole,
    CaseHint,
    ConsentStatus,
    Phase,
    ScopeRing,
    SessionState,
    TurnSignals,
    VerificationStatus,
)


def make_state(**kwargs) -> SessionState:
    return SessionState(session_id="test", sop_name="insurance_claims", **kwargs)


def verify_signals(**overrides) -> TurnSignals:
    base = dict(scope=ScopeRing.CORE, turn_index=0, raw_message="")
    base.update(overrides)
    return TurnSignals(**base)


class TestIdentityGate:
    def test_stays_in_verify_id_with_two_factors(self, domain, spec):
        state = make_state()
        signals = verify_signals(identity_candidates={"full_name": "Margaret Chen", "dob": "1985-03-15"})
        state = transition(state, signals, domain, spec)
        assert state.phase == Phase.VERIFY_ID
        assert not state.facts.is_verified()

    def test_advances_on_three_factors_in_one_turn(self, domain, spec):
        state = make_state()
        signals = verify_signals(
            identity_candidates={"full_name": "Margaret Chen", "dob": "1985-03-15", "id_last4": "4472"}
        )
        state = transition(state, signals, domain, spec)
        assert state.phase == Phase.RESOLVE_INTENT
        assert state.facts.verification_status == VerificationStatus.VERIFIED
        assert state.facts.verified_party_id == "P9"

    def test_advances_across_multiple_turns(self, domain, spec):
        state = make_state()
        state = transition(state, verify_signals(identity_candidates={"full_name": "Margaret Chen"}), domain, spec)
        assert state.phase == Phase.VERIFY_ID
        state = transition(state, verify_signals(identity_candidates={"dob": "1985-03-15"}, turn_index=1), domain, spec)
        assert state.phase == Phase.VERIFY_ID
        state = transition(state, verify_signals(identity_candidates={"id_last4": "4472"}, turn_index=2), domain, spec)
        assert state.phase == Phase.RESOLVE_INTENT

    def test_two_mismatches_locks_and_routes_to_human_handoff(self, domain, spec):
        state = make_state()
        state = transition(state, verify_signals(identity_candidates={"dob": "1999-01-01"}), domain, spec)
        state = transition(
            state, verify_signals(identity_candidates={"phone": "555-000-0000"}, turn_index=1), domain, spec
        )
        assert state.phase == Phase.HUMAN_HANDOFF
        assert (state.facts.ended.reason if state.facts.ended else None) == "identity_verification_failed"

    def test_verify_id_never_re_entered_once_left(self, domain, spec):
        """DESIGN.md §7.1: the one-way gate. Once verified, nothing an
        attacker or a confused extractor can put in TurnSignals sends the
        session back to VERIFY_ID."""
        state = make_state()
        state = transition(
            state,
            verify_signals(
                identity_candidates={"full_name": "Margaret Chen", "dob": "1985-03-15", "id_last4": "4472"}
            ),
            domain,
            spec,
        )
        assert state.phase == Phase.RESOLVE_INTENT
        # Restating identity data (or anything else) after verification must not un-verify.
        weird_signals = verify_signals(
            identity_candidates={"full_name": "somebody else"}, turn_index=1, scope=ScopeRing.CORE
        )
        state = transition(state, weird_signals, domain, spec)
        assert state.phase != Phase.VERIFY_ID
        assert state.facts.verification_status == VerificationStatus.VERIFIED


class TestRepresentativeFlow:
    def test_representative_verifies_against_the_buyer_only(self, domain, spec):
        state = make_state()
        signals = verify_signals(
            claims_representative=True,
            representative_name="David Chen",
            identity_candidates={"full_name": "Margaret Chen", "dob": "1985-03-15", "id_last4": "4472"},
        )
        state = transition(state, signals, domain, spec)
        assert state.phase == Phase.RESOLVE_INTENT
        assert state.facts.caller_role.value == "representative"
        assert state.facts.representative_of_party_id == "P9"
        assert state.facts.verified_party_id == "P9"

    def test_representative_cannot_verify_as_a_different_policyholder(self, domain, spec):
        """David Chen is only authorized for P9 (Margaret). Even if the
        matching identity factors happen to belong to another record, the
        candidate universe is restricted to P9 and the match must fail."""
        state = make_state()
        signals = verify_signals(
            claims_representative=True,
            representative_name="David Chen",
            # Ava Lopez's real factors:
            identity_candidates={"full_name": "Ava Lopez", "dob": "1990-08-21", "id_last4": "9180"},
        )
        state = transition(state, signals, domain, spec)
        assert state.facts.verification_status != VerificationStatus.VERIFIED
        assert state.facts.mismatch_count >= 1

    def test_unmatched_representative_claim_falls_back_to_normal_path_without_penalty(self, domain, spec):
        state = make_state()
        signals = verify_signals(
            claims_representative=True,
            representative_name="Nobody Authorized",
            identity_candidates={"full_name": "Margaret Chen", "dob": "1985-03-15", "id_last4": "4472"},
        )
        state = transition(state, signals, domain, spec)
        assert state.facts.representative_of_party_id is None
        assert state.facts.verification_status == VerificationStatus.VERIFIED
        assert state.facts.verified_party_id == "P9"


class TestCrossCuttingEscalation:
    @pytest.mark.parametrize(
        "phase", [Phase.VERIFY_ID, Phase.RESOLVE_INTENT, Phase.PROCESS_CASE, Phase.POST_PROCESS]
    )
    def test_explicit_escalation_request_wins_from_any_phase(self, domain, spec, phase):
        state = make_state(phase=phase)
        state = transition(state, verify_signals(escalation_request=True), domain, spec)
        assert state.phase == Phase.HUMAN_HANDOFF
        assert (state.facts.ended.reason if state.facts.ended else None) == "caller_requested_human"

    def test_injection_flags_exceed_threshold_routes_to_handoff(self, domain, spec):
        state = make_state()
        state = transition(state, verify_signals(injection_suspected=True), domain, spec)
        assert state.phase == Phase.VERIFY_ID  # one flag: not yet over threshold (max=2)
        state = transition(state, verify_signals(injection_suspected=True, turn_index=1), domain, spec)
        assert state.phase == Phase.HUMAN_HANDOFF
        assert (state.facts.ended.reason if state.facts.ended else None) == "repeated_prompt_injection_attempts"

    def test_off_topic_strikes_exceed_threshold_routes_to_abuse_terminated(self, domain, spec):
        state = make_state()
        for i in range(4):  # max_off_topic_strikes = 3; the 4th strike exceeds it
            state = transition(state, verify_signals(scope=ScopeRing.OUT, turn_index=i), domain, spec)
        assert state.phase == Phase.ABUSE_TERMINATED
        assert (state.facts.ended.reason if state.facts.ended else None) == "repeated_off_topic_requests"

    def test_off_topic_strikes_decay_on_sustained_on_topic_turns(self, domain, spec):
        """DESIGN.md §7.9: security must not punish good-faith users."""
        state = make_state()
        state = transition(state, verify_signals(scope=ScopeRing.OUT), domain, spec)
        state = transition(state, verify_signals(scope=ScopeRing.OUT, turn_index=1), domain, spec)
        assert state.facts.off_topic_strikes == 2
        state = transition(
            state, verify_signals(scope=ScopeRing.CORE, identity_candidates={"full_name": "Margaret Chen"}, turn_index=2),
            domain, spec,
        )
        assert state.facts.off_topic_strikes == 1

    def test_out_of_scope_does_not_advance_phase_even_with_identity_data_present(self, domain, spec):
        """DESIGN.md §7.4 precedence rule 3: refuse-and-redirect does not
        advance the phase, even if the same message happens to also carry
        identity data. The data is retained in memory (nothing is silently
        dropped) but does not purchase verification progress this turn —
        see DESIGN.md §11 for the documented limitation this implies."""
        state = make_state()
        signals = verify_signals(
            scope=ScopeRing.OUT,
            identity_candidates={"full_name": "Margaret Chen", "dob": "1985-03-15", "id_last4": "4472"},
        )
        state = transition(state, signals, domain, spec)
        assert state.phase == Phase.VERIFY_ID
        assert state.facts.matched_factor_count == 0
        assert "full_name" in state.memory.identity_slots  # retained for audit/provenance


class TestIntentResolution:
    def _verified_state(self, domain, spec):
        state = make_state()
        return transition(
            state,
            verify_signals(
                identity_candidates={"full_name": "Margaret Chen", "dob": "1985-03-15", "id_last4": "4472"}
            ),
            domain,
            spec,
        )

    def test_hint_from_verify_id_immediately_proposes_a_candidate(self, domain, spec):
        """The brief's worked example (DESIGN.md §7.5 HINT_REPLAY): the hint
        stated during VERIFY_ID resolves the candidate the moment the caller
        is verified, in the SAME turn — no re-asking from scratch."""
        state = make_state()
        msg = "calling about my denied healthcare claim from January"
        signals = verify_signals(
            identity_candidates={"full_name": "Margaret Chen", "dob": "1985-03-15", "id_last4": "4472"},
            case_hint=CaseHint(case_type="healthcare", status="denied", time_ref="January", verbatim_quote=msg),
            raw_message=msg,
        )
        state = transition(state, signals, domain, spec)
        assert state.phase == Phase.RESOLVE_INTENT
        assert state.memory.candidate_case_id == "CL-2048"

    def test_confirmation_moves_to_process_case(self, domain, spec):
        state = self._verified_state(domain, spec)
        msg = "denied healthcare claim from January"
        state = transition(
            state,
            verify_signals(
                case_hint=CaseHint(case_type="healthcare", status="denied", time_ref="January", verbatim_quote=msg),
                raw_message=msg,
                turn_index=1,
            ),
            domain,
            spec,
        )
        assert state.memory.candidate_case_id == "CL-2048"
        state = transition(
            state, verify_signals(confirms_proposed_case=True, turn_index=2), domain, spec
        )
        assert state.phase == Phase.PROCESS_CASE
        assert state.memory.confirmed_case_id == "CL-2048"
        assert state.memory.active_case_id == "CL-2048"

    def test_rejecting_the_candidate_clears_it_and_stays_in_resolve_intent(self, domain, spec):
        state = self._verified_state(domain, spec)
        msg = "denied healthcare claim from January"
        state = transition(
            state,
            verify_signals(
                case_hint=CaseHint(case_type="healthcare", status="denied", time_ref="January", verbatim_quote=msg),
                raw_message=msg,
                turn_index=1,
            ),
            domain,
            spec,
        )
        state = transition(
            state, verify_signals(confirms_proposed_case=False, turn_index=2), domain, spec
        )
        assert state.phase == Phase.RESOLVE_INTENT
        assert state.memory.confirmed_case_id is None

    def test_ambiguous_hints_stay_in_resolve_intent(self, domain, spec):
        """P9 has two healthcare claims. A hint of just 'healthcare' with no
        status/time narrows to 2, not 1 — must not silently guess."""
        state = self._verified_state(domain, spec)
        msg = "my healthcare claim"
        state = transition(
            state,
            verify_signals(
                case_hint=CaseHint(case_type="healthcare", verbatim_quote=msg), raw_message=msg, turn_index=1
            ),
            domain,
            spec,
        )
        assert state.phase == Phase.RESOLVE_INTENT
        assert state.memory.candidate_case_id is None


class TestPostProcess:
    def _in_process_case(self, domain, spec):
        state = make_state()
        msg = "denied healthcare claim from January"
        state = transition(
            state,
            verify_signals(
                identity_candidates={"full_name": "Margaret Chen", "dob": "1985-03-15", "id_last4": "4472"},
                case_hint=CaseHint(case_type="healthcare", status="denied", time_ref="January", verbatim_quote=msg),
                raw_message=msg,
            ),
            domain,
            spec,
        )
        state = transition(state, verify_signals(confirms_proposed_case=True, turn_index=1), domain, spec)
        assert state.phase == Phase.PROCESS_CASE
        return state

    def test_wrap_up_sets_pending_action_and_moves_to_post_process(self, domain, spec):
        state = self._in_process_case(domain, spec)
        state = transition(state, verify_signals(wrap_up_request=True, turn_index=2), domain, spec)
        assert state.phase == Phase.POST_PROCESS
        assert state.facts.pending_action is not None
        assert state.facts.pending_action.action_type == "send_summary_email"

    def test_consent_approved_records_event_and_clears_pending(self, domain, spec):
        state = self._in_process_case(domain, spec)
        state = transition(state, verify_signals(wrap_up_request=True, turn_index=2), domain, spec)
        state = transition(state, verify_signals(consent_response=True, turn_index=3, raw_message="yes please"), domain, spec)
        assert state.facts.pending_action is None
        assert state.memory.consent_events[-1]["decision"] == "approved"
        assert state.memory.consent_events[-1]["quote"] == "yes please"

    def test_consent_declined_sets_email_skipped(self, domain, spec):
        state = self._in_process_case(domain, spec)
        state = transition(state, verify_signals(wrap_up_request=True, turn_index=2), domain, spec)
        state = transition(state, verify_signals(consent_response=False, turn_index=3, raw_message="no thanks"), domain, spec)
        assert state.facts.email_skipped is True
        assert state.facts.pending_action is None

    def test_closes_after_decline_and_goodbye(self, domain, spec):
        state = self._in_process_case(domain, spec)
        state = transition(state, verify_signals(wrap_up_request=True, turn_index=2), domain, spec)
        state = transition(state, verify_signals(consent_response=False, turn_index=3), domain, spec)
        state = transition(state, verify_signals(wrap_up_request=True, turn_index=4, raw_message="bye"), domain, spec)
        assert state.phase == Phase.CLOSED


def test_transition_does_not_mutate_input_state(domain, spec):
    """Purity check: transition() must not mutate the state object passed
    in, so tests (and replay, DESIGN.md §8.2) can safely reuse a base state."""
    state = make_state()
    original_phase = state.phase
    _ = transition(
        state,
        verify_signals(identity_candidates={"full_name": "Margaret Chen", "dob": "1985-03-15", "id_last4": "4472"}),
        domain,
        spec,
    )
    assert state.phase == original_phase
    assert state.facts.matched_factor_count == 0


class TestConsentAutoPolling:
    """DESIGN.md §7.10 — polling cadence belongs to the state machine, not
    the model. These tests moved down from test_tool_effects.py when the
    behavior moved; they assert the same properties (default approves on the
    2nd observation, timeout never approves and eventually reports
    TIMED_OUT) at the layer that now owns them."""

    def _pending_state(self, consent_scenario: str) -> SessionState:
        state = make_state(consent_scenario=consent_scenario)
        state.facts.consent_status = ConsentStatus.PENDING
        state.facts.consent_poll_count = 1  # a request was opened
        return state

    def test_default_scenario_approves_on_the_next_turn(self, domain, spec):
        state = self._pending_state("default")
        state = transition(state, verify_signals(), domain, spec)
        assert state.facts.consent_status == ConsentStatus.APPROVED

    def test_timeout_scenario_never_approves_and_ends_timed_out(self, domain, spec):
        state = self._pending_state("timeout")
        for i in range(4):
            state = transition(state, verify_signals(turn_index=i), domain, spec)
            assert state.facts.consent_status in (ConsentStatus.PENDING, ConsentStatus.TIMED_OUT)
        assert state.facts.consent_status == ConsentStatus.TIMED_OUT

    def test_timeout_scenario_stays_pending_before_the_sequence_is_exhausted(self, domain, spec):
        state = self._pending_state("timeout")
        state = transition(state, verify_signals(), domain, spec)
        assert state.facts.consent_status == ConsentStatus.PENDING

    def test_no_polling_when_no_request_is_open(self, domain, spec):
        """Never-requested consent must not drift on its own — polling only
        advances something a caller's representative actually asked for."""
        state = make_state(consent_scenario="default")
        for i in range(4):
            state = transition(state, verify_signals(turn_index=i), domain, spec)
        assert state.facts.consent_status == ConsentStatus.NOT_REQUESTED
        assert state.facts.consent_poll_count == 0

    def test_approved_consent_does_not_regress(self, domain, spec):
        state = make_state(consent_scenario="default")
        state.facts.consent_status = ConsentStatus.APPROVED
        state = transition(state, verify_signals(), domain, spec)
        assert state.facts.consent_status == ConsentStatus.APPROVED


def test_representative_lookup_survives_typo_noise(domain, spec):
    """"David Chen" arrives as "david chan" through a speech recognizer. The
    representative table needs the same typo tolerance as the
    policyholder table — without it the caller was silently downgraded to a
    policyholder and the whole representative flow was lost."""
    state = make_state()
    state = transition(
        state,
        verify_signals(
            claims_representative=True,
            representative_name="david chan",
            identity_candidates={
                "full_name": "margret chan",
                "dob": "march fifteenth nineteen eighty-five",
                "id_last4": "four four seven two",
            },
        ),
        domain,
        spec,
    )
    assert state.facts.caller_role.value == "representative"
    assert state.facts.representative_of_party_id == "P9"
    assert state.facts.verification_status == VerificationStatus.VERIFIED


def test_repeated_identical_hints_are_recorded_once(domain, spec):
    """The model re-reports the same hint every turn because the directive
    asks it to. Memory absorbs that; four copies of one hint is noise, and
    it surfaced in the inspector as a meaningless list of turn indices."""
    state = make_state()
    msg = "my denied healthcare claim from January"
    for i in range(4):
        state = transition(
            state,
            verify_signals(
                case_hint=CaseHint(case_type="healthcare", status="denied", time_ref="January", verbatim_quote=msg),
                raw_message=msg,
                turn_index=i,
            ),
            domain,
            spec,
        )
    assert len(state.memory.case_hints) == 1
    assert state.memory.case_hints[0].turn_index == 0   # the first mention, with its quote


def test_a_genuinely_new_hint_is_still_recorded(domain, spec):
    state = make_state()
    state = transition(
        state,
        verify_signals(case_hint=CaseHint(case_type="healthcare", verbatim_quote="my healthcare claim")),
        domain, spec,
    )
    state = transition(
        state,
        verify_signals(case_hint=CaseHint(case_type="healthcare", status="denied", verbatim_quote="the denied one"), turn_index=1),
        domain, spec,
    )
    assert len(state.memory.case_hints) == 2


class TestExplicitConsentRequest:
    """An explicit "please ask my mother" is an instruction, not a judgement
    call — the same class as an explicit request for a human.

    Found via a flaky eval: the caller asked on one turn and the model opened
    the request on the next, which reads as the agent ignoring them. The
    docstring on handle_request_consent said the model's only decision was
    "whether to ask", which is true right up until the caller has already
    asked.
    """

    def _rep_state(self):
        st = make_state(phase=Phase.PROCESS_CASE)
        st.facts.caller_role = CallerRole.REPRESENTATIVE
        st.facts.verification_status = VerificationStatus.VERIFIED
        st.facts.verified_party_id = "P9"
        st.memory.confirmed_case_id = "CL-2048"
        return st

    def test_an_explicit_request_opens_consent_on_that_turn(self, domain, spec):
        st = self._rep_state()
        st, _ = decide(st, verify_signals(requests_consent=True,
                                   raw_message="can you request her consent?"), domain, spec)
        assert st.facts.consent_status == ConsentStatus.PENDING

    def test_merely_wanting_detail_does_not_open_it(self, domain, spec):
        """The signal is narrow on purpose: wanting information that would
        require consent is not the same as asking us to go and get it."""
        st = self._rep_state()
        st, _ = decide(st, verify_signals(raw_message="why exactly was it denied?"), domain, spec)
        assert st.facts.consent_status == ConsentStatus.NOT_REQUESTED

    def test_a_policyholder_asking_does_not_open_a_third_party_request(self, domain, spec):
        """Consent is a representative-path concept. A policyholder has no
        third party to authorise anything."""
        st = make_state(phase=Phase.PROCESS_CASE)
        st.facts.caller_role = CallerRole.POLICYHOLDER
        st.facts.verification_status = VerificationStatus.VERIFIED
        st, _ = decide(st, verify_signals(requests_consent=True), domain, spec)
        assert st.facts.consent_status == ConsentStatus.NOT_REQUESTED

    def test_asking_twice_does_not_double_advance_the_sequence(self, domain, spec):
        """Re-asking must report the existing request, not burn a poll and
        skip the caller past 'pending' to a timeout."""
        st = self._rep_state()
        st, _ = decide(st, verify_signals(requests_consent=True), domain, spec)
        first = st.facts.consent_poll_count
        st, _ = decide(st, verify_signals(requests_consent=True), domain, spec)
        assert st.facts.consent_poll_count == first + 1   # the per-turn poll, not a second open
