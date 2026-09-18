"""One turn, four stages (DESIGN.md §5.2): PERCEIVE → DECIDE → ACT → VERIFY.

This is the single place that calls the model. Everything upstream
(app/sop/*) and the guard (app/guards/*) is LLM-free and independently
tested; this module is the thin, mostly-untestable-without-a-key glue that
wires them together in the right order with the right timing — including
Appendix A's rule that side-effecting tools execute *after* the guard passes
and *before* the turn is emitted.
"""
from __future__ import annotations

import time

from dataclasses import dataclass, field

from app.llm.extraction import perceive_blocking
from app.llm.generation import act
from app.llm.provider import LLMProvider
from app.sop.domain import DomainContext
from app.sop.machine import decide, settle_phase
from app.sop.spec import SopSpec
from app.sop.types import Phase, SessionState, Turn
from app.tools.effects import execute_tool_call, load_consent_scenarios
from app.tools.registry import tools_for_names

MAX_REPAIR_ATTEMPTS = 1


@dataclass
class TurnResult:
    state: SessionState
    reply: str
    trace_event: dict = field(default_factory=dict)


def _safe_template_for(plan) -> str:
    if plan.phase == Phase.VERIFY_ID:
        return "I'm not able to continue with that just yet — could you confirm a couple more identifying details for me?"
    if plan.phase == Phase.RESOLVE_INTENT:
        return "Let me make sure I have the right claim — could you tell me a bit more about which one you mean?"
    if plan.phase == Phase.PROCESS_CASE:
        return "I want to make sure I give you accurate information — let me note this down for a closer look."
    if plan.phase == Phase.POST_PROCESS:
        return "Would you like me to send that summary to your email on file, or skip it?"
    return "Let me get you connected with someone who can help further."


def _fold_memory_updates(state: SessionState, memory_updates, turn_index: int) -> None:
    """Deferred perception (DESIGN.md §7.3/§7.11): folds ACT's memory_updates
    into state for the NEXT turn's transition() to see. Mirrors the
    unconditional-extraction handling in machine._record_deferred_signals,
    but for signals that arrived via ACT instead of blocking PERCEIVE."""
    memory = state.memory
    if memory_updates.case_hint is not None:
        memory.case_hints.append(memory_updates.case_hint)
    if memory_updates.intent and memory_updates.intent_confidence >= 0.55:
        memory.resolved_intent = memory_updates.intent
        memory.intent_evidence_quote = memory_updates.intent_evidence_quote
        memory.intent_confidence = memory_updates.intent_confidence
    if memory_updates.contact_change_request is not None:
        memory.contact_change_requests.append({**memory_updates.contact_change_request, "turn_index": turn_index})

    facts = state.facts
    if memory_updates.refusal:
        facts.refusal_count += 1
    deterministic_floor = 0
    if facts.refusal_count >= 2:
        deterministic_floor = 2
    elif facts.refusal_count >= 1:
        deterministic_floor = 1
    facts.last_intensity = max(memory_updates.intensity, deterministic_floor, facts.last_intensity)
    facts.peak_intensity = max(facts.peak_intensity, facts.last_intensity)


def run_turn(
    state: SessionState,
    user_message: str,
    domain: DomainContext,
    spec: SopSpec,
    provider: LLMProvider,
    consent_scenarios_path,
) -> TurnResult:
    from app.guards.output_guard import check_reply

    turn_index = state.next_turn_index()
    # Per-stage wall clock. Cost was already recorded here and latency was
    # not, which made "why did that turn feel slow?" a question the system
    # could not answer about itself — the only available response was to
    # guess. Latency is the half of the user's experience that cost does not
    # describe, and the stages have very different fixes: a slow PERCEIVE is
    # a prompt-size problem, a slow ACT is a tier/verbosity problem, and a
    # slow GUARD means the model needed a second attempt.
    t0 = time.perf_counter()

    # 1. PERCEIVE (blocking)
    perceive_result = perceive_blocking(state, user_message, provider, turn_index)
    signals = perceive_result.signals
    t_perceive = time.perf_counter()

    # 2. DECIDE
    new_state, plan = decide(state, signals, domain, spec)
    t_decide = time.perf_counter()

    # 3. ACT
    tool_schemas = tools_for_names(plan.allowed_tools)
    act_output = act(new_state, plan, spec, provider, user_message, tool_schemas)
    t_act = time.perf_counter()

    # 4. VERIFY — guard, with one repair attempt, then a safe template.
    # An empty reply (the model called a tool and said nothing — observed in
    # live testing: e.g. after send_summary_email) is treated as an
    # unconditional failure, not something the keyword-based required-
    # elements heuristic has to happen to catch (DESIGN.md §7.11: the safe
    # template exists precisely so a bad draft never reaches the caller).
    def _hard_fail(output) -> bool:
        return (not output.reply.strip()) or output.truncated

    verdict = check_reply(act_output.reply, plan, domain, new_state)
    guard_attempts = [{"reply": act_output.reply, "violations": verdict.violations, "empty": not act_output.reply.strip(), "truncated": act_output.truncated}]
    if _hard_fail(act_output) or not verdict.ok:
        feedback = "; ".join(verdict.violations) or "your reply had no spoken text for the caller, or was cut off — keep it shorter and always say something, even when calling a tool"
        act_output = act(new_state, plan, spec, provider, user_message, tool_schemas, repair_feedback=feedback)
        verdict = check_reply(act_output.reply, plan, domain, new_state)
        guard_attempts.append({"reply": act_output.reply, "violations": verdict.violations, "empty": not act_output.reply.strip(), "truncated": act_output.truncated})
        if _hard_fail(act_output) or not verdict.ok:
            act_output.reply = _safe_template_for(plan)
            act_output.tool_calls = []  # never execute tool calls from a rejected draft
            verdict = check_reply(act_output.reply, plan, domain, new_state)
            guard_attempts.append({"reply": act_output.reply, "violations": verdict.violations, "fallback": True})

    # append (never block on) missing required elements — DESIGN.md §7.11
    if verdict.missing_required:
        act_output.reply = act_output.reply.rstrip()
        if not act_output.reply.endswith((".", "?", "!")):
            act_output.reply += "."
        act_output.reply += " " + _required_element_fallback_sentence(verdict.missing_required, plan)

    # 4.5 execute side-effecting tool calls (Appendix A timing)
    consent_scenarios = load_consent_scenarios(consent_scenarios_path)
    tool_effects = []
    for call in act_output.tool_calls:
        result = execute_tool_call(call["name"], call["input"], call["id"], new_state, domain, consent_scenarios)
        if result:
            tool_effects.append(result)

    # A tool effect can satisfy a terminal condition that was not yet true
    # when transition() ran (sending the summary is the case that matters).
    settle_phase(new_state)

    # deferred perception -> memory, for the NEXT turn
    _fold_memory_updates(new_state, act_output.memory_updates, turn_index)

    # transcript + trace
    new_state.transcript.append(Turn(turn_index=turn_index, role="caller", text=user_message))
    trace_event = {
        "turn_index": turn_index,
        "phase_before": state.phase.value,
        "phase_after": new_state.phase.value,
        "_reply": act_output.reply,  # for eval invariants (evals/invariants.py); not shown in the Inspector as a duplicate field
        "signals": _signals_for_trace(signals),
        "plan": {
            "allowed_tools": list(plan.allowed_tools),
            "directives": [d.id for d in plan.directives],
            "required_elements": list(plan.required_elements),
            "forbidden_elements": list(plan.forbidden_elements),
            "model_tier": plan.model_tier.value,
            "route": plan.route,
            "visible_facts": plan.visible_facts,  # for the Inspector (D3) and eval invariants
        },
        "guard_attempts": guard_attempts,
        "tool_effects": [
            {"tool": e.tool_name, "summary": e.summary_for_trace} for e in tool_effects
        ],
        "memory_updates": {
            "case_hint": _hint_for_trace(act_output.memory_updates.case_hint),
            "intent": act_output.memory_updates.intent,
            "internal_note": act_output.memory_updates.internal_note,
        },
        "latency_s": {
            "perceive": round(t_perceive - t0, 3),
            "decide": round(t_decide - t_perceive, 3),
            "act": round(t_act - t_decide, 3),
            # Guard time includes any repair attempt, which is where a
            # surprising number comes from: a repair is a whole extra model
            # call, and it is invisible in the reply the caller eventually
            # sees.
            "verify": round(time.perf_counter() - t_act, 3),
            "total": round(time.perf_counter() - t0, 3),
            "reply_words": len(act_output.reply.split()),
        },
        "cost": {
            "perceive_input_tokens": perceive_result.input_tokens,
            "perceive_output_tokens": perceive_result.output_tokens,
            "perceive_cost_usd": perceive_result.cost_usd,
            "act_input_tokens": act_output.input_tokens,
            "act_output_tokens": act_output.output_tokens,
            "act_cost_usd": act_output.cost_usd,
            "total_cost_usd": perceive_result.cost_usd + act_output.cost_usd,
        },
    }
    new_state.facts.cost_usd += trace_event["cost"]["total_cost_usd"]
    new_state.facts.tokens_used += (
        perceive_result.input_tokens + perceive_result.output_tokens
        + act_output.input_tokens + act_output.output_tokens
    )

    new_state.transcript.append(
        Turn(turn_index=turn_index + 1, role="agent", text=act_output.reply, trace_event=trace_event)
    )

    return TurnResult(state=new_state, reply=act_output.reply, trace_event=trace_event)


def _required_element_fallback_sentence(missing: list[str], plan) -> str:
    if plan.phase == Phase.POST_PROCESS:
        return "Would you like me to email you that summary, or skip it?"
    if any("confirm" in m for m in missing):
        return "Can you confirm that's the right one?"
    if any("alternative" in m for m in missing):
        return "If that's not handy, your date of birth, phone, or email on file would work too."
    return "Is there anything else I can help clarify?"


def _signals_for_trace(signals) -> dict:
    return {
        "scope": signals.scope.value,
        "escalation_request": signals.escalation_request,
        "injection_suspected": signals.injection_suspected,
        "identity_candidates_keys": list(signals.identity_candidates.keys()),
        "claims_representative": signals.claims_representative,
        "confirms_proposed_case": signals.confirms_proposed_case,
        "wrap_up_request": signals.wrap_up_request,
        "consent_response": signals.consent_response,
    }


def _hint_for_trace(hint) -> dict | None:
    if hint is None:
        return None
    return {"case_type": hint.case_type, "status": hint.status, "time_ref": hint.time_ref}
