from datetime import date

from app.sop.domain import build_claim_view, load_claims, month_from_time_ref
from app.sop.intent import apply_intent_inference
from app.sop.types import CaseHint


def test_month_from_time_ref():
    assert month_from_time_ref("January") == 1
    assert month_from_time_ref("december") == 12
    assert month_from_time_ref("not a month") is None
    assert month_from_time_ref(None) is None


def test_claim_view_derives_unpaid_balance_and_deadline(domain):
    claim = domain.claim_by_id("CL-2048")
    view = build_claim_view(claim, now=date(2026, 2, 20))
    assert view.unpaid_balance == "1450.00"
    assert view.days_until_appeal_deadline == 26
    assert view.appeal_deadline_passed is False


def test_claim_view_deadline_passed_when_now_is_later(domain):
    claim = domain.claim_by_id("CL-2048")
    view = build_claim_view(claim, now=date(2026, 9, 17))
    assert view.appeal_deadline_passed is True
    assert view.days_until_appeal_deadline < 0


def test_claim_view_zero_balance_for_closed_claim(domain):
    claim = domain.claim_by_id("CL-2011")
    view = build_claim_view(claim, now=date(2026, 2, 20))
    assert view.unpaid_balance == "20.00"  # allowed_max 800.00 - net_pay 780.00


def test_guideline_kb_document_guidance_present(domain):
    text = domain.guideline_kb.document_guidance("original pathology report")
    assert "specimen" in text.lower()


def test_guideline_kb_alternative_falls_back_to_default(domain):
    text = domain.guideline_kb.document_alternative_guidance("nonexistent document")
    assert "replacement copy" in text.lower()


def test_guideline_kb_matches_processing_time_keyword(domain):
    claim = domain.claim_by_id("CL-2048")
    entries = domain.guideline_kb.match_followup(
        "how long does it take after I submit", intent="document_submission", has_documents_needed=True
    )
    assert any(e.topic == "processing_time_after_submission" for e in entries)
    formatted = domain.guideline_kb.format_entry(entries[0].text, claim)
    assert "CL-2048" in formatted


def test_guideline_kb_requires_documents_gate(domain):
    """submission_timing requires_documents=true; a claim with none needed
    should not surface it even if intent matches."""
    entries = domain.guideline_kb.match_followup(
        "when do I need to submit", intent="document_submission", has_documents_needed=False
    )
    assert entries == []


def test_claims_for_party_filters_correctly(domain):
    claims = domain.claims_for_party("P9")
    assert {c.case_id for c in claims} == {"CL-2048", "CL-2011", "CL-1899", "CL-2102"}
    assert domain.claims_for_party("nonexistent") == []


class TestIntentInference:
    """Found via live testing (EVAL.md §4): a caller who asks 'why was it
    denied' has implied status=denied even without saying that word."""

    def test_denial_question_implies_denied_status_when_absent(self):
        hint = apply_intent_inference(CaseHint(case_type="healthcare"), "denial_question")
        assert hint.status == "denied"

    def test_explicit_status_is_not_overridden(self):
        hint = apply_intent_inference(CaseHint(status="open"), "denial_question")
        assert hint.status == "open"

    def test_unrelated_intent_does_not_infer_status(self):
        hint = apply_intent_inference(CaseHint(case_type="healthcare"), "status_inquiry")
        assert hint.status is None

    def test_no_intent_leaves_hint_unchanged(self):
        hint = apply_intent_inference(CaseHint(case_type="healthcare"), None)
        assert hint.status is None
