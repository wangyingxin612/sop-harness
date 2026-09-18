"""Deterministic identity matching (DESIGN.md §7.2, §7.1).

This module is the one place where "is this caller who they say they are" is
decided. It is a pure function over fixture data and stated factors — no LLM
involvement, by design: a wrong verification decision is irreversible and
must be provable, so it cannot depend on a model's judgement (DESIGN.md §4.1).

Matching semantics (recap of the algorithm, see inline comments for the why):
  - `policy_number` is NOT an identity factor. It locates a record; it does
    not prove identity (DESIGN.md §7.2 — it's on paperwork anyone can read).
  - A factor that matches no record in the whole book is a MISMATCH.
  - A factor that matches some record, but not any record still consistent
    with previously-matched factors, is ALSO a mismatch relative to this
    caller — we do not silently widen who we think we're talking to.
  - A factor that matches and is consistent narrows (or holds) the candidate
    set and counts toward the ≥3-distinct-factor requirement.
  - 2 mismatches locks the session (must transfer). This does not reveal
    *which* field was wrong (DESIGN.md §7.2 — prevents field-by-field
    enumeration).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from app.identity.normalize import (
    normalize_dob,
    normalize_email,
    normalize_id_last4,
    normalize_name,
    normalize_phone,
)
from app.identity.records import PolicyholderRecord

IDENTITY_FACTOR_TYPES = ("full_name", "dob", "phone", "email", "id_last4")

MIN_DISTINCT_FACTORS = 3
MAX_MISMATCHES = 2


class FactorOutcome(str, Enum):
    MATCHED = "matched"
    MISMATCHED = "mismatched"
    UNPARSEABLE = "unparseable"   # e.g. a DOB we couldn't parse at all


_NORMALIZERS = {
    "full_name": normalize_name,
    "dob": lambda v: normalize_dob(v) or "",
    "phone": normalize_phone,
    "email": normalize_email,
    "id_last4": normalize_id_last4,
}


def _record_values(record: PolicyholderRecord, factor_type: str) -> tuple[str, ...]:
    if factor_type == "full_name":
        return tuple(normalize_name(v) for v in record.all_names)
    if factor_type == "dob":
        return (record.dob,)  # already YYYY-MM-DD in the fixture
    if factor_type == "phone":
        return tuple(normalize_phone(v) for v in record.all_phones)
    if factor_type == "email":
        return tuple(normalize_email(v) for v in record.all_emails)
    if factor_type == "id_last4":
        return (record.id_last4,)
    raise ValueError(f"unknown factor type: {factor_type}")


def records_matching_factor(
    records: list[PolicyholderRecord], factor_type: str, raw_value: str
) -> list[PolicyholderRecord]:
    """All records (in the whole book) whose `factor_type` matches `raw_value`."""
    normalize = _NORMALIZERS[factor_type]
    normalized = normalize(raw_value)
    if not normalized:
        return []
    return [r for r in records if normalized in _record_values(r, factor_type)]


@dataclass
class FactorApplication:
    factor_type: str
    raw_value: str
    outcome: FactorOutcome
    candidates_before: list[str]
    candidates_after: list[str]


@dataclass
class MatchState:
    """Running identity-match state for a session. Mutated turn over turn by
    `apply_factor`. Kept separate from SessionFacts so this module has zero
    dependency on app.sop — it is usable and testable standalone."""

    candidate_party_ids: list[str] = field(default_factory=list)   # empty == "not yet narrowed"
    matched_factor_types: list[str] = field(default_factory=list)  # distinct types matched so far
    mismatch_count: int = 0
    history: list[FactorApplication] = field(default_factory=list)

    @property
    def is_narrowed(self) -> bool:
        return len(self.candidate_party_ids) > 0

    @property
    def is_verified(self) -> bool:
        return (
            len(self.candidate_party_ids) == 1
            and len(self.matched_factor_types) >= MIN_DISTINCT_FACTORS
        )

    @property
    def is_locked(self) -> bool:
        return self.mismatch_count >= MAX_MISMATCHES

    @property
    def is_ambiguous(self) -> bool:
        return len(self.candidate_party_ids) > 1

    @property
    def verified_party_id(self) -> str | None:
        return self.candidate_party_ids[0] if self.is_verified else None

    @property
    def factors_still_needed(self) -> int:
        return max(0, MIN_DISTINCT_FACTORS - len(self.matched_factor_types))


def apply_factor(
    state: MatchState,
    records: list[PolicyholderRecord],
    factor_type: str,
    raw_value: str,
) -> MatchState:
    """Pure: returns a NEW MatchState reflecting one more stated factor.
    Already-locked states are frozen (further factors don't matter)."""
    if state.is_locked:
        return state

    matching = records_matching_factor(records, factor_type, raw_value)
    matching_ids = {r.party_id for r in matching}

    current_candidates = set(state.candidate_party_ids) if state.is_narrowed else None
    candidates_before = list(state.candidate_party_ids)

    if not matching_ids:
        # Matches nobody in the whole book.
        outcome = FactorOutcome.MISMATCHED
        new_candidates = state.candidate_party_ids
        new_matched_types = state.matched_factor_types
        new_mismatch = state.mismatch_count + 1
    else:
        intersection = matching_ids if current_candidates is None else (current_candidates & matching_ids)
        if not intersection:
            # Matches someone, but not anyone consistent with what we already
            # matched for *this* caller — treat as inconsistent, not as new
            # information that widens who we think we're talking to.
            outcome = FactorOutcome.MISMATCHED
            new_candidates = state.candidate_party_ids
            new_matched_types = state.matched_factor_types
            new_mismatch = state.mismatch_count + 1
        else:
            outcome = FactorOutcome.MATCHED
            new_candidates = sorted(intersection)
            new_matched_types = (
                state.matched_factor_types
                if factor_type in state.matched_factor_types
                else [*state.matched_factor_types, factor_type]
            )
            new_mismatch = state.mismatch_count

    new_state = MatchState(
        candidate_party_ids=new_candidates,
        matched_factor_types=new_matched_types,
        mismatch_count=new_mismatch,
        history=[
            *state.history,
            FactorApplication(
                factor_type=factor_type,
                raw_value=raw_value,
                outcome=outcome,
                candidates_before=candidates_before,
                candidates_after=list(new_candidates),
            ),
        ],
    )
    return new_state


def apply_factors(
    state: MatchState, records: list[PolicyholderRecord], factors: dict[str, str]
) -> MatchState:
    """Apply several stated factors in one turn, in a fixed order so results
    are deterministic regardless of dict ordering from an upstream extractor."""
    for factor_type in IDENTITY_FACTOR_TYPES:
        if factor_type in factors and factors[factor_type]:
            state = apply_factor(state, records, factor_type, factors[factor_type])
    return state


def lookup_candidates_by_policy_number(
    records: list[PolicyholderRecord], policy_number: str
) -> list[PolicyholderRecord]:
    """Policy number LOCATES a record; it never counts as a match factor
    (DESIGN.md §7.2). Useful to narrow candidates for phrasing/disambiguation
    only — never to grant or imply verification."""
    if not policy_number:
        return []
    normalized = policy_number.strip().upper()
    return [r for r in records if r.policy_number.upper() == normalized]
