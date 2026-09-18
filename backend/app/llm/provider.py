"""Thin wrapper around the model API: tiering, cost accounting, and a
structured-output helper (forced tool-choice) used by PERCEIVE.

Provider-agnostic by construction (DESIGN.md §10): Anthropic's Messages API
is the default; `LLM_BASE_URL` can point at any OpenAI-compatible endpoint
without touching call sites, because everything above this module talks to
`LLMProvider`, never to the SDK directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import anthropic

from app.config import Settings
from app.sop.types import Tier

# Approximate list pricing, USD per token. NOTE: placeholder rates — update
# from the current pricing page before relying on absolute cost figures;
# what matters for this deliverable is that cost is tracked per turn at all
# (DESIGN.md §8.3), not that these four constants are precisely current.
_PRICE_PER_TOKEN = {
    "claude-sonnet-5": {"input": 3.0 / 1_000_000, "output": 15.0 / 1_000_000},
    "claude-haiku-4-5-20251001": {"input": 1.0 / 1_000_000, "output": 5.0 / 1_000_000},
}
_DEFAULT_PRICE = {"input": 3.0 / 1_000_000, "output": 15.0 / 1_000_000}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    price = _PRICE_PER_TOKEN.get(model, _DEFAULT_PRICE)
    return input_tokens * price["input"] + output_tokens * price["output"]


@dataclass
class LLMResult:
    text: str
    tool_calls: list[dict] = field(default_factory=list)   # [{"id","name","input"}]
    raw_content: list = field(default_factory=list)        # original content blocks, replayable as an assistant turn
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""
    stop_reason: str = ""
    latency_ms: float = 0.0


class LLMProvider:
    def __init__(self, settings: Settings):
        if not settings.anthropic_api_key:
            raise RuntimeError(
                "No API key configured. Set ANTHROPIC_API_KEY in the environment, "
                "or pass one from the UI (see app/api/routes.py)."
            )
        kwargs = {"api_key": settings.anthropic_api_key}
        if settings.llm_base_url:
            kwargs["base_url"] = settings.llm_base_url
        self.client = anthropic.Anthropic(**kwargs)
        self.settings = settings

    def model_for(self, tier: Tier) -> str:
        return self.settings.model_strong if tier == Tier.STRONG else self.settings.model_fast

    def call(
        self,
        *,
        tier: Tier,
        system: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: dict | None = None,
        max_tokens: int = 1024,
    ) -> LLMResult:
        import time

        model = self.model_for(tier)
        kwargs: dict = dict(model=model, max_tokens=max_tokens, system=system, messages=messages)
        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice

        start = time.monotonic()
        resp = self.client.messages.create(**kwargs)
        latency_ms = (time.monotonic() - start) * 1000

        text = "".join(b.text for b in resp.content if b.type == "text")
        tool_calls = [
            {"id": b.id, "name": b.name, "input": b.input} for b in resp.content if b.type == "tool_use"
        ]
        cost = estimate_cost_usd(model, resp.usage.input_tokens, resp.usage.output_tokens)
        raw_content = [b.model_dump() if hasattr(b, "model_dump") else b for b in resp.content]
        return LLMResult(
            text=text,
            tool_calls=tool_calls,
            raw_content=raw_content,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            cost_usd=cost,
            model=model,
            stop_reason=resp.stop_reason,
            latency_ms=latency_ms,
        )

    def structured_call(
        self,
        *,
        tier: Tier,
        system: str,
        messages: list[dict],
        tool_name: str,
        tool_description: str,
        input_schema: dict,
        max_tokens: int = 512,
    ) -> tuple[dict | None, LLMResult]:
        """Forces the model to respond via a single tool call, so the result
        is schema-validated JSON rather than free text we have to parse
        (DESIGN.md §7.3: 'structured output makes missing a validation
        failure rather than a silent absence'). Returns (parsed_input | None
        on failure, the raw LLMResult for cost/latency accounting)."""
        tool = {"name": tool_name, "description": tool_description, "input_schema": input_schema}
        result = self.call(
            tier=tier,
            system=system,
            messages=messages,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool_name},
            max_tokens=max_tokens,
        )
        if result.tool_calls:
            return result.tool_calls[0]["input"], result
        return None, result
