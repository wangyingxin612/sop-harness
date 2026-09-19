"""LLM-free tests for PERCEIVE's parsing (DESIGN.md §7.3).

These moved here from test_generation_helpers.py when perception stopped
being split across two calls. Everything a caller's message can tell us is
now read by the one blocking call, so this is where a dropped field shows up.

The split existed to save latency and cost more than it saved: intensity
arriving a turn late meant the empathy directive never fired on the turn a
caller was actually upset, and carrying a second JSON payload in ACT's token
budget truncated replies in live testing.
"""
from __future__ import annotations

from app.llm.extraction import signals_from_parsed
from app.sop.types import Phase


def _parse(parsed, phase=Phase.PROCESS_CASE, msg="hello", turn=0):
    return signals_from_parsed(parsed, phase, msg, turn)


class TestCaseHints:
    def test_hint_is_built_from_any_present_field(self):
        s = _parse(
            {"case_type": "healthcare", "case_status_hint": "denied", "case_time_ref": "January"},
            msg="my denied healthcare claim from January",
        )
        assert s.case_hint.case_type == "healthcare"
        assert s.case_hint.status == "denied"
        assert s.case_hint.time_ref == "January"
        assert s.case_hint.verbatim_quote == "my denied healthcare claim from January"

    def test_partial_hint_is_still_a_hint(self):
        """R8: a caller who says only "it was denied" has narrowed the search."""
        s = _parse({"case_status_hint": "denied"})
        assert s.case_hint is not None
        assert s.case_hint.case_type is None

    def test_no_hint_when_no_field_present(self):
        s = _parse({"intent": "status_inquiry"})
        assert s.case_hint is None
        assert s.intent == "status_inquiry"


class TestAffect:
    def test_intensity_is_available_on_the_same_turn(self):
        """The whole reason perception was un-split. Empathy is needed on the
        turn the person is upset, not the turn after."""
        s = _parse({"intensity": 2, "refusal": True})
        assert s.intensity == 2
        assert s.refusal is True

    def test_affect_defaults_are_safe_when_the_model_omits_them(self):
        s = _parse({})
        assert s.intensity == 0
        assert s.refusal is False
        assert s.confusion is False


class TestContactChange:
    def test_captured_only_when_flagged(self):
        assert _parse({"contact_change_detail": "new@email.com"}).contact_change_request is None
        s = _parse({"contact_change_requested": True, "contact_change_detail": "new@email.com"})
        assert s.contact_change_request == {"detail": "new@email.com"}


class TestPhaseScopedFields:
    def test_identity_candidates_only_in_verify_id(self):
        payload = {"identity_candidates": {"dob": "1985-03-15", "phone": None}}
        assert _parse(payload, Phase.VERIFY_ID).identity_candidates == {"dob": "1985-03-15"}
        assert _parse(payload, Phase.PROCESS_CASE).identity_candidates == {}

    def test_consent_response_only_in_post_process(self):
        # Tri-state on purpose: "unaddressed" is not "declined". A boolean
        # here would make silence read as a refusal to consent.
        assert _parse({"consent_response": "yes"}, Phase.POST_PROCESS).consent_response is True
        assert _parse({"consent_response": "no"}, Phase.POST_PROCESS).consent_response is False
        assert _parse({}, Phase.POST_PROCESS).consent_response is None
        assert _parse({"consent_response": "yes"}, Phase.PROCESS_CASE).consent_response is None

    def test_wrap_up_read_in_both_late_phases(self):
        for phase in (Phase.PROCESS_CASE, Phase.POST_PROCESS):
            assert _parse({"wrap_up_request": True}, phase).wrap_up_request is True


class TestDegradation:
    def test_a_failed_extraction_still_honours_the_regex_floor(self):
        """DESIGN.md §7.3: DECIDE must be total over partial input, and an
        explicit ask for a human must survive the model failing entirely."""
        s = signals_from_parsed(None, Phase.VERIFY_ID, "let me talk to a human please", 0)
        assert s.escalation_request is True
        assert s.scope.value == "core"
        assert s.identity_candidates == {}

    def test_an_unknown_scope_value_falls_back_to_core(self):
        """A bad enum must not route a legitimate caller to a refusal."""
        assert _parse({"scope": "nonsense"}).scope.value == "core"

    def test_whitelist_overrides_a_wrong_out_of_scope_call(self):
        s = _parse({"scope": "out"}, msg="what documents do I need for my claim appeal?")
        assert s.scope.value == "core"
