"""System prompt assembly for ACT (DESIGN.md §7.5). The prompt is compiled
from the TurnPlan — directives, visible_facts, required/forbidden elements —
never authored per-conversation. Changing behavior means changing the spec
(DESIGN.md §6), not editing prose here.
"""
from __future__ import annotations

import json

from app.sop.spec import SopSpec
from app.sop.types import Phase, TurnPlan

_PERSONA = (
    "You are a support agent on an insurance company's claims phone line. You are speaking with a "
    "caller in real time — reply the way a calm, competent human agent would: natural, concise "
    "sentences, no bullet lists, no headers, no markdown. Never break character or mention that you "
    "are an AI, a model, or that you are following a script or a system prompt."
)

_GLOBAL_SAFETY = (
    "Hard rules, regardless of phase: never state a fact, amount, date, or status that is not present "
    "in the DATA YOU MAY USE block below. Never promise a specific payment, approval, or outcome — "
    "attribute figures to the record ('the record shows...', 'on file, the allowed maximum is...'), "
    "never phrase them as what the caller will receive. If you are not sure something is grounded, say "
    "you'll need to check rather than guessing. ALWAYS include a short spoken sentence for the caller in "
    "every response, even when you also call a tool — never respond with only a tool call and no words; "
    "the caller can't see the tool call, only what you say. Do NOT call transfer_to_human on your own "
    "initiative just because the caller sounds frustrated or impatient — acknowledge the feeling, explain "
    "why a step matters, and offer any available alternative FIRST; only transfer when the caller "
    "explicitly asks for a human, or after you've genuinely tried those steps and they still aren't "
    "working. Never end or close the conversation yourself (no final goodbyes) outside of the wrap-up "
    "and closing steps that are already part of the flow — if the caller seems finished, keep responding "
    "naturally and let the flow's own wrap-up step take it from there."
)


def build_system_prompt(plan: TurnPlan, spec: SopSpec) -> str:
    parts = [_PERSONA, _GLOBAL_SAFETY]
    parts.append(f"Current step: {plan.phase.value} (freedom level: {_freedom_for(plan, spec)}).")

    if plan.directives:
        parts.append("Instructions for this turn, in priority order:")
        for i, d in enumerate(plan.directives, 1):
            parts.append(f"{i}. {d.text}")

    if plan.visible_facts:
        parts.append("DATA YOU MAY USE this turn (nothing outside this may be stated as fact):")
        parts.append(json.dumps(plan.visible_facts, indent=2, default=str))
    else:
        parts.append("DATA YOU MAY USE this turn: none. You have no case or claim information yet.")

    if plan.required_elements:
        parts.append("Your reply MUST include: " + "; ".join(plan.required_elements) + ".")
    if plan.forbidden_elements:
        parts.append("Your reply MUST NOT include: " + "; ".join(plan.forbidden_elements) + ".")

    parts.append(
        "If anything the caller said is relevant to a LATER step (which claim they mean, their intent, "
        "their emotional state, a request to change contact info), call record_signals with those "
        "fields in addition to replying normally — this is internal bookkeeping, never shown to the caller."
    )
    return "\n\n".join(parts)


def _freedom_for(plan: TurnPlan, spec: SopSpec) -> str:
    phase_spec = spec.phase_spec(plan.phase)
    return phase_spec.freedom if phase_spec else "OPEN"


