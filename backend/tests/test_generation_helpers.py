"""LLM-free tests for ACT's deferred-perception parsing (DESIGN.md §7.3/§7.11)."""
from app.llm.generation import _parse_record_signals


def test_parses_case_hint_when_present():
    updates = _parse_record_signals(
        {"case_type": "healthcare", "case_status_hint": "denied", "case_time_ref": "January"},
        turn_index=0,
        raw_message="my denied healthcare claim from January",
    )
    assert updates.case_hint.case_type == "healthcare"
    assert updates.case_hint.status == "denied"
    assert updates.case_hint.time_ref == "January"
    assert updates.case_hint.verbatim_quote == "my denied healthcare claim from January"


def test_no_case_hint_when_fields_absent():
    updates = _parse_record_signals({"intent": "status_inquiry"}, turn_index=0, raw_message="what's the status")
    assert updates.case_hint is None
    assert updates.intent == "status_inquiry"


def test_contact_change_request_captured_only_when_flagged():
    updates = _parse_record_signals(
        {"contact_change_requested": True, "contact_change_detail": "new phone number"},
        turn_index=2,
        raw_message="I got a new phone number",
    )
    assert updates.contact_change_request == {"detail": "new phone number"}

    updates2 = _parse_record_signals({}, turn_index=0, raw_message="hi")
    assert updates2.contact_change_request is None


def test_emotion_fields_default_safely():
    updates = _parse_record_signals({}, turn_index=0, raw_message="hi")
    assert updates.negative_affect is False
    assert updates.refusal is False
    assert updates.intensity == 0
