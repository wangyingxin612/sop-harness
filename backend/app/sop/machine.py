"""The state-transition half of DECIDE (DESIGN.md §7.4): a Mealy-style pure
function. `transition()` never mutates its input — it deep-copies, mutates
the copy, and returns it — so it is safe to call from tests without needing
to guard against aliasing, and so replay (DESIGN.md §8.2) can re-run it
deterministically against recorded signals.

Split from `policy.py` on purpose: `transition` decides *what changed*
(phase, memory, counters). `policy.resolve` decides *what that implies* for
this turn's TurnPlan. Testing them separately keeps each one small enough to
reason about exhaustively.
"""
from __future__ import annotations

import copy

from app.sop.domain import DomainContext
from app.sop.gates import evaluate_identity_gate
from app.sop.intent import merge_case_hints, resolve_candidates
from app.sop.policy import resolve
from app.sop.spec import SopSpec
from app.sop.types import (
    TERMINAL_PHASES,
    Phase,
    PendingAction,
    ScopeRing,
    SessionState,
    Slot,
    TurnSignals,
    VerificationStatus,
)

INTENT_CONFIDENCE_THRESHOLD = 0.55


def _record_deferred_signals(state: SessionState, signals: TurnSignals) -> None:
    """Unconditional memory writes (DESIGN.md §7.3) — happen every turn,
    regardless of phase. This is the whole mechanism behind R8."""
    if signals.case_hint is not None:
        hint = signals.case_hint
        hint.turn_index = signals.turn_index
        state.memory.case_hints.append(hint)

    if signals.intent and signals.intent_confidence >= INTENT_CONFIDENCE_THRESHOLD:
        state.memory.resolved_intent = signals.intent
        state.memory.intent_evidence_quote = signals.intent_evidence_quote
        state.memory.intent_confidence = signals.intent_confidence

    if signals.contact_change_request is not None:
        # §7.12: recorded, never auto-applied. Acting on it is a separate,
        # higher-privilege gate the current SOP routes out of scope.
        state.memory.contact_change_requests.append(
            {**signals.contact_change_request, "turn_index": signals.turn_index}
        )

    # Identity factors: the matcher (via evaluate_identity_gate) is the
    # source of truth for what "matched" means; we additionally keep a
    # provenance record here for audit/inspector purposes and to support
    # self-correction (Slot.superseded_by).
    for factor_type, raw_value in signals.identity_candidates.items():
        state.memory.set_identity_factor(
            factor_type,
            Slot(value=raw_value, verbatim_quote=signals.raw_message, turn_index=signals.turn_index),
        )


def _apply_emotion_and_abuse_counters(state: SessionState, signals: TurnSignals, spec: SopSpec) -> None:
    facts = state.facts
    if signals.refusal:
        facts.refusal_count += 1
    if signals.escalation_request:
        facts.repeated_request_count += 1

    deterministic_floor = 0
    if facts.refusal_count >= 2:
        deterministic_floor = 2
    elif facts.refusal_count >= 1:
        deterministic_floor = 1
    if signals.escalation_request:
        deterministic_floor = max(deterministic_floor, 2)
    facts.last_intensity = max(signals.intensity, deterministic_floor)

    if signals.scope == ScopeRing.OUT:
        facts.off_topic_strikes += 1
    elif facts.off_topic_strikes > 0:
        # Decay: sustained on-topic turns forgive prior off-topic strikes
        # (DESIGN.md §7.9 — security must not punish good-faith users).
        facts.off_topic_strikes = max(0, facts.off_topic_strikes - 1)
    facts.last_scope = signals.scope

    if signals.injection_suspected:
        facts.injection_flags += 1


def _identity_phase_transition(
    state: SessionState, signals: TurnSignals, domain: DomainContext, spec: SopSpec
) -> Phase:
    facts = state.facts
    result = evaluate_identity_gate(
        facts,
        domain,
        signals.identity_candidates,
        signals.claims_representative,
        signals.representative_name,
    )
    # Locking is decided here, not only in the centralized precedence block:
    # the mismatch that *causes* the lock happens inside evaluate_identity_gate
    # above, in this same call, so a pre-dispatch check would see stale state.
    # (The centralized check in transition() is kept as a defensive fallback
    # for a state that somehow arrives already locked.)
    if facts.verification_status == VerificationStatus.LOCKED:
        facts.escalation_reason = "identity_verification_failed"
        return Phase.HUMAN_HANDOFF
    if facts.verification_status == VerificationStatus.VERIFIED:
        return Phase.RESOLVE_INTENT
    return Phase.VERIFY_ID


def _try_narrow_intent_candidate(state: SessionState, domain: DomainContext) -> None:
    """Entry action for RESOLVE_INTENT (DESIGN.md §7.5's HINT_REPLAY): if the
    hints accumulated so far (possibly stated back in VERIFY_ID — this is the
    R8 mechanism) narrow the caller's claims to exactly one, propose it.
    Idempotent and side-effect-free beyond `memory.candidate_case_id`, so it
    is safe to call both on the turn a caller *enters* RESOLVE_INTENT and on
    later turns while still inside it."""
    memory = state.memory
    facts = state.facts
    merged_hint = merge_case_hints(memory.case_hints)
    candidates = resolve_candidates(domain.claims_for_party(facts.verified_party_id), merged_hint, domain.now)
    if len(candidates) == 1:
        memory.candidate_case_id = candidates[0].case_id


def _resolve_intent_phase_transition(
    state: SessionState, signals: TurnSignals, domain: DomainContext, spec: SopSpec
) -> Phase:
    memory = state.memory

    # A candidate is already on the table awaiting confirmation.
    if memory.candidate_case_id is not None:
        if signals.confirms_proposed_case is True:
            memory.confirmed_case_id = memory.candidate_case_id
            memory.active_case_id = memory.candidate_case_id
            memory.candidate_case_id = None
            return Phase.PROCESS_CASE
        if signals.confirms_proposed_case is False:
            memory.candidate_case_id = None
            # fall through to re-disambiguate below

    _try_narrow_intent_candidate(state, domain)
    # 0 or >1 candidates after narrowing: stay in RESOLVE_INTENT; policy.py
    # exposes the index-only candidate list so the model can ask a
    # disambiguating question (DESIGN.md §5.3).
    return Phase.RESOLVE_INTENT


def _process_case_phase_transition(state: SessionState, signals: TurnSignals) -> Phase:
    if signals.wrap_up_request:
        if state.facts.pending_action is None:
            state.facts.pending_action = PendingAction(
                action_type="send_summary_email", requested_at_turn=signals.turn_index
            )
        return Phase.POST_PROCESS
    # Active-case switching happens in policy.py's visible-facts assembly,
    # not here: it never changes the phase (DESIGN.md §7.1).
    return Phase.PROCESS_CASE


def _post_process_phase_transition(state: SessionState, signals: TurnSignals) -> Phase:
    facts = state.facts
    memory = state.memory

    if facts.pending_action is not None and facts.pending_action.action_type == "send_summary_email":
        if signals.consent_response is True:
            memory.consent_events.append(
                {
                    "action_type": "send_summary_email",
                    "decision": "approved",
                    "turn_index": signals.turn_index,
                    "quote": signals.raw_message,
                }
            )
            facts.pending_action = None
            # actual send happens as a tool effect (Appendix A); policy.py's
            # directive tells ACT to call send_summary_email this turn.
        elif signals.consent_response is False:
            memory.consent_events.append(
                {
                    "action_type": "send_summary_email",
                    "decision": "declined",
                    "turn_index": signals.turn_index,
                    "quote": signals.raw_message,
                }
            )
            facts.pending_action = None
            facts.email_skipped = True

    if signals.wrap_up_request and (facts.email_sent or facts.email_skipped):
        return Phase.CLOSED
    return Phase.POST_PROCESS


def transition(state: SessionState, signals: TurnSignals, domain: DomainContext, spec: SopSpec) -> SessionState:
    """Pure: returns a new SessionState. Never mutates `state`."""
    new_state = copy.deepcopy(state)
    new_state.facts.turns_used += 1

    _record_deferred_signals(new_state, signals)
    _apply_emotion_and_abuse_counters(new_state, signals, spec)

    phase = new_state.phase
    facts = new_state.facts

    if phase in TERMINAL_PHASES:
        new_state.phase = phase
        return new_state

    # --- centralized precedence, mirrors DESIGN.md §7.4's directive order ---
    if signals.escalation_request:
        facts.escalation_reason = "caller_requested_human"
        new_state.phase = Phase.HUMAN_HANDOFF
        return new_state

    if facts.verification_status == VerificationStatus.LOCKED:
        facts.escalation_reason = "identity_verification_failed"
        new_state.phase = Phase.HUMAN_HANDOFF
        return new_state

    if facts.injection_flags >= spec.escalation.max_injection_flags:
        facts.escalation_reason = "repeated_prompt_injection_attempts"
        new_state.phase = Phase.HUMAN_HANDOFF
        return new_state

    if facts.off_topic_strikes > spec.escalation.max_off_topic_strikes:
        facts.escalation_reason = "repeated_off_topic_requests"
        new_state.phase = Phase.ABUSE_TERMINATED
        return new_state

    if signals.scope == ScopeRing.OUT:
        # Refuse-and-redirect: explicitly does NOT advance the phase
        # (DESIGN.md §7.4 precedence rule 3).
        new_state.phase = phase
        return new_state

    if phase == Phase.VERIFY_ID:
        new_state.phase = _identity_phase_transition(new_state, signals, domain, spec)
        if new_state.phase == Phase.RESOLVE_INTENT:
            # Chained entry action: don't make the caller wait a whole extra
            # turn to hear the candidate we can already propose from hints
            # they gave during VERIFY_ID (DESIGN.md §7.5 HINT_REPLAY).
            _try_narrow_intent_candidate(new_state, domain)
    elif phase == Phase.RESOLVE_INTENT:
        new_state.phase = _resolve_intent_phase_transition(new_state, signals, domain, spec)
    elif phase == Phase.PROCESS_CASE:
        new_state.phase = _process_case_phase_transition(new_state, signals)
    elif phase == Phase.POST_PROCESS:
        new_state.phase = _post_process_phase_transition(new_state, signals)
    else:
        new_state.phase = phase

    return new_state


def decide(state: SessionState, signals: TurnSignals, domain: DomainContext, spec: SopSpec):
    """Convenience: transition() then resolve() — the full DECIDE stage
    (DESIGN.md §5.2 step 2) in one call. Returns (new_state, TurnPlan)."""
    new_state = transition(state, signals, domain, spec)
    plan = resolve(new_state, domain, spec)
    return new_state, plan
