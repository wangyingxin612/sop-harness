"""ACT (DESIGN.md §7.5, §7.11): generation inside the envelope DECIDE built.
The model sees only `plan.allowed_tools` and `plan.visible_facts` — nothing
else is in context, which is the structural mechanism behind R3 (DESIGN.md
§5.3): there is nothing to leak because nothing was fetched.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.llm.prompts import build_system_prompt
from app.llm.provider import LLMProvider, LLMResult
from app.sop.spec import SopSpec
from app.sop.types import CaseHint, SessionState, TurnPlan


@dataclass
class MemoryUpdates:
    """What ACT contributes to memory. Only the model's own rationale — every
    perception of the caller's message is now read by the blocking PERCEIVE
    call instead (see app/tools/registry.py)."""

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


def _parse_record_signals(tool_input: dict, turn_index: int, raw_message: str) -> MemoryUpdates:  # noqa: ARG001
    return MemoryUpdates(internal_note=tool_input.get("internal_note", "") or "")


def _tool_ack(tool_name: str, state: SessionState) -> str:
    """What to tell the model a tool call did, when we're completing the
    tool-use exchange before the side effect has actually run.

    This must be SPECIFIC. A generic "accepted" was read by the model as
    "the consent was granted", and it told a caller their mother had
    approved disclosure in the scenario where she never does — a fabricated
    claim about case state, caused by an ambiguous acknowledgement I
    introduced with the continuation fix. Found by the ASR-noise run, which
    had nothing to do with ASR: adversarial variation surfaces unrelated
    defects, which is most of why it's worth running.
    """
    if tool_name == "record_signals":
        return "recorded"
    if tool_name == "request_consent":
        status = state.facts.consent_status.value
        return (
            f"A consent request is open. Its status is currently '{status}'. "
            "Describe it to the caller as exactly this status — do NOT say it has been approved "
            "unless the status is 'approved'."
        )
    if tool_name == "send_summary_email":
        return "Queued for sending to the address on file. You may say it is on its way."
    if tool_name == "create_followup":
        return "The note has been attached to the claim file."
    if tool_name == "transfer_to_human":
        return "The transfer has been initiated."
    return "accepted"


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
        # Generous headroom: a reply plus a record_signals call AND a
        # side-effecting tool call (e.g. request_consent) in the same turn
        # can use more output tokens than expected — observed live both
        # truncating mid-sentence and (worse) spending the entire budget on
        # tool-call JSON before any reply text, triggering the guard's
        # repair path on an otherwise-fine turn. The repair path recovers
        # correctly either way (that's what it's for), but a wider budget
        # means it has to less often. See PROGRESS.md.
        max_tokens=1536,
    )

    # --- Empty-reply continuation -------------------------------------------
    # The model frequently answers a turn with ONLY tool_use blocks and no
    # text. That is correct protocol behavior on its part (stop_reason ==
    # "tool_use" means "I'm waiting for results"), but a single-pass design
    # has nothing to show the caller.
    #
    # Earlier attempts asked the model nicely in the prompt and re-rolled the
    # whole generation on failure. Measured across the eval suite: ~20% of
    # turns needed a repair and ~5% fell through to a safe template, which
    # stalls the conversation. The fix is to stop depending on volunteered
    # text and finish the exchange the protocol's way.
    #
    # ORDERING (Appendix A) is preserved deliberately: the tool_result handed
    # back here is a neutral ACKNOWLEDGEMENT ("accepted"), not an executed
    # outcome. Real side effects still run in the orchestrator, after the
    # guard passes — and if the guard rejects the reply, `tool_calls` is
    # cleared and the side effect never happens at all. The model is only
    # being told "your request was accepted", which is true, and is exactly
    # what it needs in order to narrate "I'm starting that now".
    if not result.text.strip() and result.tool_calls:
        continued = provider.call(
            tier=plan.model_tier,
            system=system,
            messages=[
                *messages,
                {"role": "assistant", "content": result.raw_content},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": c["id"],
                            "content": _tool_ack(c["name"], state),
                        }
                        for c in result.tool_calls
                    ],
                },
            ],
            tools=tool_schemas,
            max_tokens=1536,
        )
        result = LLMResult(
            text=continued.text,
            tool_calls=[*result.tool_calls, *continued.tool_calls],
            raw_content=continued.raw_content,
            input_tokens=result.input_tokens + continued.input_tokens,
            output_tokens=result.output_tokens + continued.output_tokens,
            cost_usd=result.cost_usd + continued.cost_usd,
            model=continued.model,
            stop_reason=continued.stop_reason,
            latency_ms=result.latency_ms + continued.latency_ms,
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
