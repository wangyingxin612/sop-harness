"""Gate evaluation (DESIGN.md §7.2): the pieces of `transition()` that decide
whether a privilege boundary has been crossed. Kept separate from
`machine.py` so each gate is independently testable.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.identity.matcher import MatchState, apply_factors
from app.identity.representatives import find_representative
from app.sop.domain import DomainContext
from app.sop.types import CallerRole, SessionFacts, VerificationStatus


def match_state_from_facts(facts: SessionFacts) -> MatchState:
    return MatchState(
        candidate_party_ids=list(facts.candidate_party_ids),
        matched_factor_types=list(facts.matched_factor_types),
        mismatch_count=facts.mismatch_count,
    )


def write_match_state_into_facts(facts: SessionFacts, match: MatchState) -> None:
    facts.candidate_party_ids = list(match.candidate_party_ids)
    facts.matched_factor_types = list(match.matched_factor_types)
    facts.mismatch_count = match.mismatch_count
    facts.matched_factor_count = len(match.matched_factor_types)
    if match.is_locked:
        facts.verification_status = VerificationStatus.LOCKED
    elif match.is_verified:
        facts.verification_status = VerificationStatus.VERIFIED
        facts.verified_party_id = match.verified_party_id
    elif match.is_ambiguous:
        facts.verification_status = VerificationStatus.AMBIGUOUS
    else:
        facts.verification_status = VerificationStatus.UNVERIFIED


@dataclass
class IdentityGateResult:
    facts: SessionFacts
    newly_verified: bool


def evaluate_identity_gate(
    facts: SessionFacts,
    domain: DomainContext,
    identity_candidates: dict[str, str],
    claims_representative: bool,
    representative_name: str | None,
) -> IdentityGateResult:
    """Applies any newly-stated identity factors and updates verification
    status. Representative detection restricts the matcher's search universe
    to the single claimed policyholder (DESIGN.md §7.10), which is what
    removes ambiguity from that path by construction."""

    was_verified = facts.verification_status == VerificationStatus.VERIFIED

    # Representative detection happens once, before any candidate universe
    # is fixed by matched factors, and only if not already committed to a
    # role this session.
    if (
        claims_representative
        and representative_name
        and facts.caller_role == CallerRole.UNKNOWN
    ):
        rep = find_representative(domain.representatives, representative_name)
        if rep is not None:
            facts.caller_role = CallerRole.REPRESENTATIVE
            facts.representative_of_party_id = rep.buyer_party_id
        # If no matching representative record: we deliberately do NOT lock
        # or penalize here. The caller still proceeds through normal
        # identity matching as a (possibly self-identifying-incorrectly)
        # policyholder; unmatched claims-of-representation carry no special
        # privilege and no special penalty on their own.

    if facts.caller_role == CallerRole.UNKNOWN:
        facts.caller_role = CallerRole.POLICYHOLDER  # default once any factor is offered

    if facts.representative_of_party_id:
        record = domain.policyholder_by_party_id(facts.representative_of_party_id)
        universe = [record] if record else []
    else:
        universe = domain.policyholders

    if identity_candidates:
        match = match_state_from_facts(facts)
        match = apply_factors(match, universe, identity_candidates)
        write_match_state_into_facts(facts, match)

    now_verified = facts.verification_status == VerificationStatus.VERIFIED
    return IdentityGateResult(facts=facts, newly_verified=(now_verified and not was_verified))
