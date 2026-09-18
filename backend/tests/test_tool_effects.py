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
        assert state.facts.escalation_reason == "agent_initiated_transfer"


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
    def test_default_scenario_approves_on_second_poll(self):
        state = make_state(consent_scenario="default")
        r1 = handle_request_consent(state, {}, "t1", CONSENT_SCENARIOS)
        assert state.facts.consent_status == ConsentStatus.PENDING
        r2 = handle_request_consent(state, {}, "t2", CONSENT_SCENARIOS)
        assert state.facts.consent_status == ConsentStatus.APPROVED
        assert r2.output["status"] == "approved"

    def test_timeout_scenario_never_approves_and_reports_timed_out(self):
        state = make_state(consent_scenario="timeout")
        for _ in range(5):
            handle_request_consent(state, {}, "t", CONSENT_SCENARIOS)
        assert state.facts.consent_status == ConsentStatus.TIMED_OUT

    def test_timeout_scenario_stays_pending_before_exhausted(self):
        state = make_state(consent_scenario="timeout")
        handle_request_consent(state, {}, "t1", CONSENT_SCENARIOS)
        assert state.facts.consent_status == ConsentStatus.PENDING

    def test_poll_count_increments(self):
        state = make_state(consent_scenario="default")
        handle_request_consent(state, {}, "t1", CONSENT_SCENARIOS)
        handle_request_consent(state, {}, "t2", CONSENT_SCENARIOS)
        assert state.facts.consent_poll_count == 2


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
