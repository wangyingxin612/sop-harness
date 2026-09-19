"""LLM-free tests for tool side effects (DESIGN.md Appendix A, §7.10)."""
from app.sop.types import CallerRole, ConsentStatus, Phase, SessionState
from app.tools.effects import (
    handle_create_followup,
    handle_request_consent,
    handle_send_summary_email,
    handle_transfer_to_human,
    load_consent_scenarios,
)

CONSENT_SCENARIOS = load_consent_scenarios("fixtures/consent_scenarios.json")


def make_state(**kwargs) -> SessionState:
    return SessionState(session_id="t", sop_name="insurance_claims", **kwargs)


class TestTransferToHuman:
    def test_sets_phase_and_reason(self):
        state = make_state(phase=Phase.PROCESS_CASE)
        handle_transfer_to_human(state, {"reason": "caller upset"}, "tool_1")
        assert state.phase == Phase.HUMAN_HANDOFF
        assert (state.facts.ended.reason if state.facts.ended else None) == "agent_initiated_transfer"


class TestCreateFollowup:
    def test_appends_note(self):
        state = make_state()
        handle_create_followup(state, {"note": "caller needs a duplicate pathology report"}, "t1")
        assert state.memory.followup_notes == ["caller needs a duplicate pathology report"]

    def test_blank_note_not_recorded(self):
        state = make_state()
        handle_create_followup(state, {"note": "  "}, "t1")
        assert state.memory.followup_notes == []


class TestRequestConsent:
    """`request_consent` INITIATES; the state machine advances the poll
    (see tests/test_state_machine.py::TestConsentAutoPolling). Split this
    way on purpose — DESIGN.md §7.10: how often to check an async approval
    is not a judgement call a model should be making."""

    def test_opening_a_request_sets_pending_on_the_default_scenario(self):
        state = make_state(consent_scenario="default")
        r = handle_request_consent(state, {}, "t1", CONSENT_SCENARIOS)
        assert state.facts.consent_status == ConsentStatus.PENDING
        assert state.facts.consent_poll_count == 1
        assert r.output["status"] == "pending"

    def test_reopening_while_pending_is_a_no_op_on_status(self):
        """A caller asking 'has it come through?' must not be able to make
        the model advance the approval by re-calling the tool — otherwise
        the polling cadence leaks back into the model's control."""
        state = make_state(consent_scenario="default")
        handle_request_consent(state, {}, "t1", CONSENT_SCENARIOS)
        for _ in range(5):
            handle_request_consent(state, {}, "tN", CONSENT_SCENARIOS)
        assert state.facts.consent_status == ConsentStatus.PENDING
        assert state.facts.consent_poll_count == 1

    def test_timeout_scenario_opens_pending_like_any_other(self):
        state = make_state(consent_scenario="timeout")
        handle_request_consent(state, {}, "t1", CONSENT_SCENARIOS)
        assert state.facts.consent_status == ConsentStatus.PENDING


class TestSendSummaryEmail:
    def test_blocked_without_a_recorded_consent_event(self, domain):
        state = make_state(phase=Phase.POST_PROCESS)
        state.facts.verified_party_id = "P9"
        result = handle_send_summary_email(state, {}, "t1", domain)
        assert result.output["status"] == "blocked"
        assert state.facts.email_sent is False

    def test_sends_after_approved_consent_event(self, domain):
        state = make_state(phase=Phase.POST_PROCESS)
        state.facts.verified_party_id = "P9"
        state.memory.confirmed_case_id = "CL-2048"
        state.memory.consent_events.append({"action_type": "send_summary_email", "decision": "approved", "turn_index": 3})
        result = handle_send_summary_email(state, {}, "t1", domain)
        assert result.output["status"] == "sent"
        assert result.output["to"] == "margaret@email.com"
        assert state.facts.email_sent is True

    def test_declined_consent_event_does_not_send(self, domain):
        state = make_state(phase=Phase.POST_PROCESS)
        state.facts.verified_party_id = "P9"
        state.memory.consent_events.append({"action_type": "send_summary_email", "decision": "declined", "turn_index": 3})
        result = handle_send_summary_email(state, {}, "t1", domain)
        assert result.output["status"] == "blocked"
        assert state.facts.email_sent is False
