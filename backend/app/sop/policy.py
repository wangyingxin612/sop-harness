"""The output half of DECIDE (DESIGN.md §7.4, Appendix A): a pure function
from (already-transitioned) SessionState to a TurnPlan. Never calls an LLM,
never performs I/O. This is what makes policy exhaustively testable — see
tests/test_policy_resolver.py, which iterates phase × signal combinations.

Directive precedence (DESIGN.md §7.4, Appendix A):
    1. explicit request for a human        -> handled in machine.py (phase already HUMAN_HANDOFF)
    2. any hard counter exceeded            -> handled in machine.py
    3. out of scope                         -> REFUSE_AND_REDIRECT (handled here, §7.9)
    4. negative_affect >= 2 or refusal      -> EMPATHIZE + EXPLAIN_WHY, PREPENDED to 5
    5. the phase's own task directive       -> from spec.base_directives
"""
from __future__ import annotations

import hashlib

from app.sop.domain import DomainContext
from app.sop.intent import index_view, merge_case_hints, resolve_candidates
from app.sop.spec import SopSpec
from app.sop.types import (
    Directive,
    Phase,
    ScopeRing,
    SessionState,
    StreamPolicy,
    Tier,
    TurnPlan,
)

# --- directives the engine itself owns (not vertical-specific; DESIGN.md §6
# draws the line at "insurance vocabulary", and these are generic empathy /
# safety moves any SOP would want) ---
ACKNOWLEDGE_EMOTION = Directive(
    id="ACKNOWLEDGE_EMOTION",
    text="Open by acknowledging how the caller feels, in your own words, before anything else.",
)
OFFER_ALTERNATIVE_FACTORS = Directive(
    id="OFFER_ALTERNATIVE_FACTORS",
    text=(
        "Offer a different accepted identity factor the caller can provide instead "
        "(full legal name, date of birth, phone or email on file, or the last four digits "
        "of their SSN or national ID)."
    ),
)
STATE_FACTORS_REMAINING = Directive(
    id="STATE_FACTORS_REMAINING",
    text="State plainly how many more identity factors are still needed.",
)
DISAMBIGUATION_HELP = Directive(
    id="DISAMBIGUATION_HELP",
    text="More than one claim could match — ask a short clarifying question using only type, status, and rough date.",
)
NO_CANDIDATES_HELP = Directive(
    id="NO_CANDIDATES_HELP",
    text="Nothing on file matches what the caller described so far — ask them to describe the claim differently, without implying anything is wrong on their end.",
)
CONSENT_REMINDER = Directive(
    id="CONSENT_REMINDER",
    text="A decision to send or skip the summary is still needed before this can close — ask plainly if it wasn't just answered.",
)
SEND_NOW = Directive(
    id="SEND_NOW",
    text="The caller just agreed to receive the summary — call send_summary_email now, to the address on file.",
)
ACKNOWLEDGE_DECLINE = Directive(
    id="ACKNOWLEDGE_DECLINE",
    text="The caller declined the summary email. Accept that without pushback and ask if there's anything else.",
)
REPRESENTATIVE_SCOPE_NOTE = Directive(
    id="REPRESENTATIVE_SCOPE_NOTE",
    text=(
        "You are speaking with an authorized representative, not the policyholder directly. "
        "Share status and next steps, but do not read out SSN/ID digits or the full clinical/denial narrative "
        "unless the policyholder's consent for full detail has been recorded."
    ),
)

_ALWAYS_AVAILABLE_TOOLS_KEY = "always_available_tools"


def _base_directives(spec: SopSpec, phase: Phase) -> list[Directive]:
    phase_spec = spec.phase_spec(phase)
    if phase_spec is None:
        return []
    return [
        Directive(id=d_id, text=spec.directive_texts.get(d_id, d_id))
        for d_id in phase_spec.base_directives
    ]


def _allowed_tools(spec: SopSpec, phase: Phase, always_available: tuple[str, ...]) -> tuple[str, ...]:
    phase_spec = spec.phase_spec(phase)
    if phase_spec is None:
        return ()
    if phase in (Phase.HUMAN_HANDOFF, Phase.ABUSE_TERMINATED, Phase.CLOSED):
        return tuple(phase_spec.tools)
    return tuple(dict.fromkeys([*phase_spec.tools, *always_available]))  # dedup, keep order


def _refusal_directive(spec: SopSpec, facts) -> Directive:
    idx = int(hashlib.sha256(str(facts.off_topic_strikes).encode()).hexdigest(), 16) % max(
        1, len(spec.refusal_templates)
    )
    template = spec.refusal_templates[idx] if spec.refusal_templates else "I can't help with that here."
    return Directive(id="REFUSAL_TEMPLATE", text=f"Use this exact refusal, verbatim: \"{template}\"")


def resolve(state: SessionState, domain: DomainContext, spec: SopSpec) -> TurnPlan:
    """Pure output function (DESIGN.md Appendix A): a function of `state`
    alone (plus the read-only `domain` and `spec` the engine is configured
    with — conceptually fixed/partially-applied, not per-turn input). Must
    be called AFTER `machine.transition()` has already folded this turn's
    signals into `state` — that is what lets this stay single-argument over
    the thing that actually varies turn to turn."""
    phase = state.phase
    facts = state.facts
    memory = state.memory
    phase_spec = spec.phase_spec(phase)
    scope_this_turn = facts.last_scope
    always_available_tools = spec.always_available_tools or ("transfer_to_human",)

    directives: list[Directive] = []
    required: list[str] = []
    forbidden: list[str] = []
    visible_facts: dict = {}
    route = "normal"
    terminal_reason = None

    model_tier = phase_spec.model_tier if phase_spec else Tier.STRONG
    stream_policy = phase_spec.stream if phase_spec else StreamPolicy.SENTENCE_GATED

    # --- terminal phases short-circuit everything else ---
    if phase in (Phase.HUMAN_HANDOFF, Phase.ABUSE_TERMINATED, Phase.CLOSED):
        route = {
            Phase.HUMAN_HANDOFF: "human_handoff",
            Phase.ABUSE_TERMINATED: "abuse_terminated",
            Phase.CLOSED: "closed",
        }[phase]
        terminal_reason = facts.escalation_reason
        directives = _base_directives(spec, phase)
        return TurnPlan(
            phase=phase,
            allowed_tools=(),
            visible_facts={"handoff_packet": None},  # filled by orchestrator (needs domain+state together)
            directives=tuple(directives),
            required_elements=(),
            forbidden_elements=("any claim amount", "any claim status", "any PII value"),
            model_tier=model_tier,
            stream_policy=stream_policy,
            terminal_reason=terminal_reason,
            route=route,
        )

    # --- precedence rule 3: out of scope -> templated refusal, no phase advance ---
    if scope_this_turn == ScopeRing.OUT:
        directives = [_refusal_directive(spec, facts)]
        forbidden = ["anything other than the refusal", "any answer to the off-topic question"]
        return TurnPlan(
            phase=phase,
            allowed_tools=(),
            visible_facts={},
            directives=tuple(directives),
            required_elements=(),
            forbidden_elements=tuple(forbidden),
            model_tier=Tier.FAST,
            stream_policy=StreamPolicy.BUFFERED,
            route="refuse",
        )

    # --- precedence rule 4: empathy prepends, never replaces (DESIGN.md §7.4/§7.8) ---
    upset = facts.last_intensity >= 2 or facts.refusal_count >= 1
    if upset:
        directives.append(ACKNOWLEDGE_EMOTION)

    # --- precedence rule 5: phase's own task ---
    directives.extend(_base_directives(spec, phase))

    if phase == Phase.VERIFY_ID:
        remaining = max(0, spec.min_distinct_factors - facts.matched_factor_count)
        visible_facts = {
            "factors_matched_count": facts.matched_factor_count,
            "factors_still_needed": remaining,
            "accepted_factor_types": list(spec.identity_factor_types),
            "caller_role": facts.caller_role.value,
        }
        forbidden = [
            "any claim fact",
            "any claim amount",
            "any claim status",
            "any case id",
            "confirmation that a record was found or not found for a specific field",
        ]
        if upset:
            directives.append(OFFER_ALTERNATIVE_FACTORS)
            directives.append(STATE_FACTORS_REMAINING)
            required = ["an offered alternative identity factor", "how many factors remain"]

    elif phase == Phase.RESOLVE_INTENT:
        merged_hint = merge_case_hints(memory.case_hints)
        candidates = resolve_candidates(domain.claims_for_party(facts.verified_party_id), merged_hint, domain.now)
        forbidden = ["denial reason", "documents needed", "any dollar amount"]

        if memory.candidate_case_id:
            claim = domain.claim_by_id(memory.candidate_case_id)
            visible_facts = {"candidate": index_view(claim)} if claim else {}
            required = ["a restatement of the candidate claim (type, status, rough date)", "a request to confirm"]
        elif len(candidates) == 0:
            directives.append(NO_CANDIDATES_HELP)
            visible_facts = {"candidates": []}
        else:
            directives.append(DISAMBIGUATION_HELP)
            visible_facts = {"candidates": [index_view(c) for c in candidates]}

    elif phase == Phase.PROCESS_CASE:
        active_id = memory.active_case_id or memory.confirmed_case_id
        view = domain.view(active_id) if active_id else None
        if view:
            claim = view.record
            visible_facts = {
                "claim": {
                    "case_id": claim.case_id,
                    "case_type": claim.case_type,
                    "status": claim.status,
                    "created_at": claim.created_at,
                    "summary": claim.summary,
                    "denial_reason": claim.denial_reason,
                    "documents_needed": list(claim.documents_needed),
                    "appeal_deadline": claim.appeal_deadline,
                    "expected_reimbursement_amount": claim.expected_reimbursement_amount,
                    "allowed_max_amount": claim.allowed_max_amount,
                    "net_pay": claim.net_pay,
                    "net_fee": claim.net_fee,
                    # pre-computed derivations (DESIGN.md §7.4 — the model does no arithmetic)
                    "unpaid_balance": view.unpaid_balance,
                    "days_until_appeal_deadline": view.days_until_appeal_deadline,
                    "appeal_deadline_passed": view.appeal_deadline_passed,
                },
                "guidance": _gather_guidance(domain, claim),
            }
        if facts.caller_role.value == "representative":
            directives.append(REPRESENTATIVE_SCOPE_NOTE)
        forbidden = ["any amount or date not present in the claim data provided this turn"]

    elif phase == Phase.POST_PROCESS:
        active_id = memory.active_case_id or memory.confirmed_case_id
        view = domain.view(active_id) if active_id else None
        policyholder = domain.policyholder_by_party_id(facts.verified_party_id)
        visible_facts = {
            "claim_summary": {
                "case_id": view.case_id,
                "status": view.record.status,
                "denial_reason": view.record.denial_reason,
                "documents_needed": list(view.record.documents_needed),
            } if view else None,
            "resolved_intent": memory.resolved_intent,
            "file_email": policyholder.email if policyholder else None,
            "pending_action": facts.pending_action.action_type if facts.pending_action else None,
        }
        forbidden = ["sending to any email address other than the one on file"]

        just_approved = bool(memory.consent_events) and memory.consent_events[-1].get(
            "decision"
        ) == "approved" and memory.consent_events[-1].get("turn_index") == state.next_turn_index() - 1
        just_declined = bool(memory.consent_events) and memory.consent_events[-1].get(
            "decision"
        ) == "declined" and memory.consent_events[-1].get("turn_index") == state.next_turn_index() - 1

        if just_approved:
            directives.append(SEND_NOW)
        elif just_declined:
            directives.append(ACKNOWLEDGE_DECLINE)
        elif facts.pending_action is None and not facts.email_sent and not facts.email_skipped:
            directives.append(CONSENT_REMINDER)

    return TurnPlan(
        phase=phase,
        allowed_tools=_allowed_tools(spec, phase, always_available_tools),
        visible_facts=visible_facts,
        directives=tuple(directives),
        required_elements=tuple(required),
        forbidden_elements=tuple(forbidden),
        model_tier=Tier.STRONG if upset and model_tier == Tier.FAST else model_tier,
        stream_policy=stream_policy,
        terminal_reason=terminal_reason,
        route=route,
    )


def _gather_guidance(domain: DomainContext, claim) -> dict:
    kb = domain.guideline_kb
    doc_guidance = {}
    doc_alternatives = {}
    for doc in claim.documents_needed:
        g = kb.document_guidance(doc)
        if g:
            doc_guidance[doc] = g
        doc_alternatives[doc] = kb.document_alternative_guidance(doc)
    return {
        "case_type_guidance": kb.case_type_guidance(claim.case_type),
        "default_guidance": kb.default_guidance(),
        "document_guidance": doc_guidance,
        "document_alternative_guidance": doc_alternatives,
        "followup_fallback": kb.followup_fallback(),
        "followup_settings": kb.followup_settings(),
    }


def match_followup_for_message(domain: DomainContext, claim, message: str, intent: str | None) -> list[str]:
    """Convenience used by ACT-time prompt assembly to add message-specific
    followup guidance (DESIGN.md §7.6's alternative ladder) on top of the
    always-included guidance from `_gather_guidance`."""
    entries = domain.guideline_kb.match_followup(message, intent, bool(claim.documents_needed))
    return [domain.guideline_kb.format_entry(e.text, claim) for e in entries]
