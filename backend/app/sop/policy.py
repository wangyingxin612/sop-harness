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
from app.sop.handoff import build_handoff_packet
from app.sop.intent import apply_intent_inference, index_view, merge_case_hints, resolve_candidates
from app.sop.spec import SopSpec
from app.sop.types import (
    CallerRole,
    ConsentStatus,
    Directive,
    Phase,
    ScopeRing,
    SessionState,
    StreamPolicy,
    Tier,
    TurnPlan,
)

# DESIGN.md §7.10: minimum-necessary fields visible to a representative who
# has not (yet) had consent recorded for full disclosure. Actionable
# (documents/deadline) stays visible; narrative and financial detail does not.
_REDUCED_SCOPE_CLAIM_FIELDS = (
    "case_id", "case_type", "status", "created_at", "documents_needed",
    "appeal_deadline", "days_until_appeal_deadline", "appeal_deadline_passed",
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
    text=(
        "The caller just agreed to receive the summary — call send_summary_email now, to the address "
        "on file. Then confirm in one sentence that it's on its way, and ask whether there's anything "
        "else they need. Do NOT sign off yet: the call isn't over until they say they're done."
    ),
)
SEND_AND_CLOSE = Directive(
    id="SEND_AND_CLOSE",
    text=(
        "The caller just agreed to receive the summary AND has already said they have nothing further. "
        "Call send_summary_email now, to the address on file. Then confirm in one sentence that it's on "
        "its way and close the call warmly. Do NOT ask whether there's anything else — they have "
        "already answered that."
    ),
)
CLOSE_OUT = Directive(
    id="CLOSE_OUT",
    text=(
        "The summary has been handled and the caller has nothing further. Close the call warmly in one "
        "or two sentences."
    ),
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


def _agent_initiated_transfer_allowed(state: SessionState, spec: SopSpec) -> bool:
    """Whether the model may decide *on its own* to transfer (DESIGN.md §7.8:
    "know when to stop persuading" implies persuading first).

    A caller who explicitly asks for a human never depends on this — that path
    is deterministic and runs in `transition()` before ACT is ever called
    (§7.4 precedence rule 1). This governs only the model's own initiative.

    Structural, not a prompt rule: on the very first exchange of a gated phase
    the tool is simply not in `allowed_tools`, so the model cannot call it —
    it has to try the persuasion ladder first. It becomes available once the
    conversation has actually had a chance to go wrong (a second turn, a
    mismatch, or a locked/abusive state).
    """
    facts = state.facts
    if state.phase != Phase.VERIFY_ID:
        return True  # after the identity gate, the model's judgement is trusted
    if facts.mismatch_count >= 1 or facts.off_topic_strikes >= 1:
        return True
    return facts.turns_used >= spec.escalation.min_turns_before_agent_initiated_transfer


def _allowed_tools(
    state: SessionState, spec: SopSpec, phase: Phase, always_available: tuple[str, ...]
) -> tuple[str, ...]:
    phase_spec = spec.phase_spec(phase)
    if phase_spec is None:
        return ()
    if phase in (Phase.HUMAN_HANDOFF, Phase.ABUSE_TERMINATED, Phase.CLOSED):
        return tuple(phase_spec.tools)
    available = [*phase_spec.tools, *always_available]
    if not _agent_initiated_transfer_allowed(state, spec):
        available = [t for t in available if t != "transfer_to_human"]
    return tuple(dict.fromkeys(available))  # dedup, keep order


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
        packet = build_handoff_packet(state, domain).as_dict() if phase == Phase.HUMAN_HANDOFF else None
        return TurnPlan(
            phase=phase,
            allowed_tools=(),
            # DESIGN.md §7.6: "no transfer is ever empty-handed." The model
            # never has to re-derive this — it's handed the packet directly.
            visible_facts={"handoff_packet": packet} if packet else {},
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
            "a farewell or sign-off",
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
        merged_hint = apply_intent_inference(merged_hint, memory.resolved_intent)
        candidates = resolve_candidates(domain.claims_for_party(facts.verified_party_id), merged_hint, domain.now)
        forbidden = ["a farewell or sign-off", "denial reason", "documents needed", "any dollar amount"]

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
        reduced_scope = (
            facts.caller_role == CallerRole.REPRESENTATIVE
            and facts.consent_status != ConsentStatus.APPROVED
        )
        if view:
            claim = view.record
            full_claim = {
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
            }
            if reduced_scope:
                visible_facts = {
                    "claim": {k: v for k, v in full_claim.items() if k in _REDUCED_SCOPE_CLAIM_FIELDS},
                    "disclosure_note": (
                        "Representative without recorded policyholder consent: status and actionable "
                        "next steps only. Denial narrative and dollar amounts are withheld until consent "
                        "is recorded via request_consent."
                    ),
                }
            else:
                visible_facts = {"claim": full_claim, "guidance": _gather_guidance(domain, claim)}

        if facts.caller_role == CallerRole.REPRESENTATIVE:
            # Consent status has to be a GROUNDED fact, not something the model
            # infers from a tool acknowledgement. Without it in visible_facts
            # the model had nothing to check itself against — and in the
            # timeout scenario it told the caller consent had been approved
            # when it never was. Putting it here also makes the claim
            # checkable: the grounding guard can now see the real value.
            visible_facts["consent"] = {
                "status": facts.consent_status.value,
                "times_checked": facts.consent_poll_count,
                "meaning": {
                    "not_requested": "no request has been opened yet",
                    "pending": "requested, still waiting — it has NOT been approved",
                    "approved": "granted; full detail may be shared",
                    "timed_out": "no response after repeated checks; it has NOT been approved",
                    "declined": "explicitly refused",
                }[facts.consent_status.value],
            }
        if facts.caller_role == CallerRole.REPRESENTATIVE:
            directives.append(REPRESENTATIVE_SCOPE_NOTE)
        # "a farewell or sign-off": closing the call is POST_PROCESS's job, and
        # the summary offer (R6) is mandatory — the model doesn't get to skip
        # it by sensing the conversation is over (DESIGN.md §7.7).
        forbidden = [
            "a farewell or sign-off",
            "any amount or date not present in the claim data provided this turn",
        ]
        if reduced_scope:
            forbidden = [*forbidden, "the denial narrative", "any dollar amount", "SSN or ID digits"]
            required = [*required, "an offer to request the policyholder's consent for full detail, if not already declined"]

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

        # "this turn" is next_turn_index(), NOT next_turn_index() - 1.
        #
        # resolve() runs BEFORE the caller's turn is appended to the
        # transcript (the orchestrator appends after ACT), so at this point
        # next_turn_index() already IS the index transition() stamped onto
        # the consent event a moment ago. The `- 1` made both of these
        # permanently False: SEND_NOW never fired once, and the summary email
        # was going out only because the model volunteered the tool call from
        # the pending_action in visible_facts. It worked nearly every time,
        # which is exactly why it went unnoticed — a deterministic
        # instruction had quietly become a hope.
        this_turn = state.next_turn_index()
        last_consent = memory.consent_events[-1] if memory.consent_events else {}
        just_approved = last_consent.get("decision") == "approved" and last_consent.get("turn_index") == this_turn
        just_declined = last_consent.get("decision") == "declined" and last_consent.get("turn_index") == this_turn

        email_decided = facts.email_sent or facts.email_skipped
        # The question is settled the moment the caller answers it, not only
        # once the tool has run. Without `just_approved` here, the turn that
        # SENDS the summary was still carrying "offer the caller a summary"
        # alongside "send it and close" — two instructions that contradict
        # each other, resolved by whichever the model weighted more.
        if email_decided or just_approved or just_declined:
            # The summary question is settled — drop the directives that ask
            # it. Leaving them in made the agent offer the summary a SECOND
            # time after already sending it (found by testing the
            # deployed demo, not by the eval suite: no scenario had a turn
            # AFTER the email decision).
            directives = [d for d in directives if d.id not in ("OFFER_SUMMARY", "CONSENT_REQUIRED")]

        if just_approved:
            # Asking "anything else?" after someone has already said they are
            # done is a round trip that exists only because the system forgot.
            # When the wrap-up intent is on file, send and close in one turn.
            directives.append(SEND_AND_CLOSE if facts.wrap_up_signalled else SEND_NOW)
        elif just_declined:
            directives.append(ACKNOWLEDGE_DECLINE)
        elif email_decided:
            directives.append(CLOSE_OUT)
        elif facts.pending_action is None:
            directives.append(CONSENT_REMINDER)

    return TurnPlan(
        phase=phase,
        allowed_tools=_allowed_tools(state, spec, phase, always_available_tools),
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
