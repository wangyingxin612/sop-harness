"""ACT (DESIGN.md §7.5, §7.11): generation inside the envelope DECIDE built.
The model sees only `plan.allowed_tools` and `plan.visible_facts` — nothing
else is in context, which is the structural mechanism behind R3 (DESIGN.md
§5.3): there is nothing to leak because nothing was fetched.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.llm.prompts import build_system_prompt
from app.llm.provider import LLMProvider
from app.sop.spec import SopSpec
from app.sop.types import CaseHint, SessionState, TurnPlan


@dataclass
class MemoryUpdates:
    case_hint: CaseHint | None = None
    intent: str | None = None
    intent_confidence: float = 0.0
    intent_evidence_quote: str = ""
    negative_affect: bool = False
    refusal: bool = False
    confusion: bool = False
    intensity: int = 0
    contact_change_request: dict | None = None
    internal_note: str = ""


@dataclass
class ActOutput:
    reply: str
    tool_calls: list[dict] = field(default_factory=list)   # excludes record_signals
    memory_updates: MemoryUpdates = field(default_factory=MemoryUpdates)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""
    latency_ms: float = 0.0
    truncated: bool = False   # stop_reason == "max_tokens" — treat as a guard failure, not just short text


def _parse_record_signals(tool_input: dict, turn_index: int, raw_message: str) -> MemoryUpdates:
    case_hint = None
    if any(tool_input.get(k) for k in ("case_type", "case_status_hint", "case_time_ref")):
        case_hint = CaseHint(
            case_type=tool_input.get("case_type"),
            status=tool_input.get("case_status_hint"),
            time_ref=tool_input.get("case_time_ref"),
            verbatim_quote=raw_message,
            turn_index=turn_index,
        )
    contact_change = None
    if tool_input.get("contact_change_requested"):
        contact_change = {"detail": tool_input.get("contact_change_detail", "")}
    return MemoryUpdates(
        case_hint=case_hint,
        intent=tool_input.get("intent"),
        intent_confidence=float(tool_input.get("intent_confidence", 0.0) or 0.0),
        intent_evidence_quote=tool_input.get("intent_evidence_quote", ""),
        negative_affect=bool(tool_input.get("negative_affect", False)),
        refusal=bool(tool_input.get("refusal", False)),
        confusion=bool(tool_input.get("confusion", False)),
        intensity=int(tool_input.get("intensity", 0) or 0),
        contact_change_request=contact_change,
        internal_note=tool_input.get("internal_note", ""),
    )


def act(
    state: SessionState,
    plan: TurnPlan,
    spec: SopSpec,
    provider: LLMProvider,
    user_message: str,
    tool_schemas: list[dict],
    repair_feedback: str | None = None,
) -> ActOutput:
    from app.sop.types import Turn  # local import avoids a cycle at module load time

    system = build_system_prompt(plan, spec)
    if repair_feedback:
        system += (
            "\n\nYour previous draft violated a hard rule and was discarded before the caller saw it: "
            f"{repair_feedback}\nReply again, correcting this."
        )

    messages = []
    for t in state.transcript[-10:]:
        role = "user" if t.role == "caller" else "assistant"
        messages.append({"role": role, "content": t.text})
    messages.append({"role": "user", "content": user_message})

    result = provider.call(
        tier=plan.model_tier,
        system=system,
        messages=messages,
        tools=tool_schemas,
        # Generous headroom: a reply plus a record_signals tool call (which
        # has a wide schema) can use more output tokens than expected — a
        # tighter budget here was observed live truncating mid-sentence and,
        # worse, silently dropping the record_signals call that hint-replay
        # depends on. See PROGRESS.md.
        max_tokens=1024,
    )

    tool_calls = []
    memory_updates = MemoryUpdates()
    for call in result.tool_calls:
        if call["name"] == "record_signals":
            memory_updates = _parse_record_signals(call["input"], state.next_turn_index(), user_message)
        else:
            tool_calls.append(call)

    return ActOutput(
        reply=result.text.strip(),
        tool_calls=tool_calls,
        memory_updates=memory_updates,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
        model=result.model,
        latency_ms=result.latency_ms,
        truncated=(result.stop_reason == "max_tokens"),
    )
