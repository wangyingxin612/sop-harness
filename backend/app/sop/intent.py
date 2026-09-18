"""Deterministic case disambiguation (DESIGN.md §7.5).

"The model proposes, code filters, the caller confirms." This module is the
"code filters" part: given whatever case hints have accumulated in memory
(possibly across several turns, possibly starting in VERIFY_ID), narrow the
verified caller's claims to a candidate set using nothing but comparisons.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date

from app.sop.domain import ClaimRecord, month_from_time_ref
from app.sop.types import CaseHint


def merge_case_hints(hints: list[CaseHint]) -> CaseHint:
    """Later non-null values win per field — later statements refine rather
    than discard earlier ones (DESIGN.md R8: information from any turn is
    available once needed)."""
    merged = CaseHint()
    for h in hints:
        if h.case_type:
            merged = replace(merged, case_type=h.case_type)
        if h.status:
            merged = replace(merged, status=h.status)
        if h.time_ref:
            merged = replace(merged, time_ref=h.time_ref)
    return merged


# Resolved intent -> the status it deterministically implies, used only to
# help narrow candidates (never to grant access). Found via live testing: a
# caller who asks "why was it denied" has, in effect, stated status=denied
# even if that exact word from the caller's own mouth was never captured as
# a case_hint — see PROGRESS.md.
_INTENT_IMPLIED_STATUS = {
    "denial_question": "denied",
    "appeal_request": "denied",
}


def apply_intent_inference(hint: CaseHint, resolved_intent: str | None) -> CaseHint:
    if hint.status or not resolved_intent:
        return hint
    implied = _INTENT_IMPLIED_STATUS.get(resolved_intent)
    return replace(hint, status=implied) if implied else hint


def resolve_candidates(
    claims: list[ClaimRecord], hint: CaseHint, now: date
) -> list[ClaimRecord]:
    """Narrow by whichever hint fields are present. A month reference
    resolves to its most recent past occurrence (DESIGN.md §7.5) — if that
    would eliminate every candidate, the month filter is not applied (so a
    genuine mismatch surfaces as a clarifying question rather than silently
    emptying the result)."""
    candidates = list(claims)

    if hint.case_type:
        candidates = [c for c in candidates if c.case_type == hint.case_type]
    if hint.status:
        candidates = [c for c in candidates if c.status == hint.status]
    if hint.time_ref:
        month = month_from_time_ref(hint.time_ref)
        if month:
            this_cutoff = date(now.year, now.month, 1)
            month_matches = [
                c
                for c in candidates
                if c.created_month() == month
                and date(c.created_year(), month, 1) <= this_cutoff
            ]
            if month_matches:
                candidates = month_matches

    return candidates


def index_view(claim: ClaimRecord) -> dict:
    """RESOLVE_INTENT's visible-facts scope (DESIGN.md §5.3): index fields
    only, never denial_reason / documents_needed / amounts."""
    return {
        "case_id": claim.case_id,
        "case_type": claim.case_type,
        "created_at": claim.created_at,
        "status": claim.status,
    }
