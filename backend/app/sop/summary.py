"""Deterministic POST_PROCESS summary draft (DESIGN.md §7.7): "assembled
from structured state, not free recall. The model phrases it; it does not
decide its contents." Building it here, from `SessionState` alone, means the
email content is exactly what the caller can audit against the transcript —
there is no separate free-text summarization pass for the guard to have to
re-verify.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.sop.domain import DomainContext
from app.sop.types import SessionState


@dataclass
class SummaryDraft:
    case_id: str | None
    case_type: str | None
    status: str | None
    denial_reason: str | None
    documents_needed: list[str]
    appeal_deadline: str | None
    days_until_appeal_deadline: int | None
    resolved_intent: str | None
    follow_up_items: list[str] = field(default_factory=list)
    recipient_email: str | None = None

    def as_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "case_type": self.case_type,
            "status": self.status,
            "denial_reason": self.denial_reason,
            "documents_needed": self.documents_needed,
            "appeal_deadline": self.appeal_deadline,
            "days_until_appeal_deadline": self.days_until_appeal_deadline,
            "resolved_intent": self.resolved_intent,
            "follow_up_items": self.follow_up_items,
            "recipient_email": self.recipient_email,
        }


def build_summary_draft(state: SessionState, domain: DomainContext) -> SummaryDraft:
    memory = state.memory
    facts = state.facts
    active_id = memory.active_case_id or memory.confirmed_case_id
    view = domain.view(active_id) if active_id else None
    policyholder = domain.policyholder_by_party_id(facts.verified_party_id) if facts.verified_party_id else None

    follow_ups: list[str] = []
    if view:
        claim = view.record
        if claim.documents_needed:
            follow_ups.append(f"Submit: {', '.join(claim.documents_needed)}")
        if view.appeal_deadline_passed is False and view.days_until_appeal_deadline is not None:
            follow_ups.append(f"Appeal deadline: {claim.appeal_deadline} ({view.days_until_appeal_deadline} days remaining)")
        elif view.appeal_deadline_passed is True:
            follow_ups.append(f"Appeal deadline of {claim.appeal_deadline} has passed — a representative can review options")
    for note in getattr(memory, "followup_notes", []):
        follow_ups.append(note)

    return SummaryDraft(
        case_id=view.case_id if view else None,
        case_type=view.record.case_type if view else None,
        status=view.record.status if view else None,
        denial_reason=view.record.denial_reason if view else None,
        documents_needed=list(view.record.documents_needed) if view else [],
        appeal_deadline=view.record.appeal_deadline if view else None,
        days_until_appeal_deadline=view.days_until_appeal_deadline if view else None,
        resolved_intent=memory.resolved_intent,
        follow_up_items=follow_ups,
        recipient_email=policyholder.email if policyholder else None,
    )
