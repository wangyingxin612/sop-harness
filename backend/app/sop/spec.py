"""SOP spec loader (DESIGN.md §6: "SOP as configuration").

The engine (this package) never hard-codes insurance vocabulary. What varies
between verticals — phase list, freedom level, tool permissions, streaming
policy, escalation thresholds, refusal copy — lives in a YAML file. Swapping
`insurance_claims.yaml` for `bank_kyc.yaml` changes the product without
touching a line of Python; that is the whole point of §6.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.sop.types import Phase, StreamPolicy, Tier


@dataclass(frozen=True)
class PhaseSpec:
    id: Phase
    freedom: str                      # "STRICT" | "OPEN" — DESIGN.md §6
    tools: tuple[str, ...]
    stream: StreamPolicy
    model_tier: Tier
    base_directives: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class EscalationSpec:
    # Identity lockout is governed by SopSpec.max_mismatches, not a separate
    # "attempts" counter — DESIGN.md §7.2's lockout and this file used to
    # state two different thresholds (3 vs 2) for the same event; resolved
    # in favor of the matcher's mismatch-based lock (see identity/matcher.py)
    # and logged in PROGRESS.md.
    max_off_topic_strikes: int = 3
    max_injection_flags: int = 2
    off_topic_decay_after_turns: int = 2   # DESIGN.md §7.9 — counters must decay
    max_stalled_verify_turns: int = 6      # softer valve: stuck (not necessarily hostile) caller
    # The model may not transfer on its own initiative before this many turns
    # of a gated phase have elapsed — it has to try the persuasion ladder
    # first (§7.8). An explicit caller request bypasses this entirely and is
    # handled deterministically in transition().
    min_turns_before_agent_initiated_transfer: int = 2


@dataclass(frozen=True)
class SopSpec:
    name: str
    display_name: str
    phases: dict[Phase, PhaseSpec]
    escalation: EscalationSpec
    directive_texts: dict[str, str]
    refusal_templates: tuple[str, ...]
    identity_factor_types: tuple[str, ...]
    min_distinct_factors: int
    max_mismatches: int
    always_available_tools: tuple[str, ...] = field(default_factory=tuple)

    def phase_spec(self, phase: Phase) -> PhaseSpec | None:
        return self.phases.get(phase)


def _load_yaml(path: str | Path) -> dict:
    return yaml.safe_load(Path(path).read_text())


def load_spec(path: str | Path) -> SopSpec:
    raw = _load_yaml(path)

    phases: dict[Phase, PhaseSpec] = {}
    for p in raw["phases"]:
        phase_id = Phase(p["id"])
        phases[phase_id] = PhaseSpec(
            id=phase_id,
            freedom=p.get("freedom", "OPEN"),
            tools=tuple(p.get("tools", [])),
            stream=StreamPolicy(p.get("stream", "sentence_gated")),
            model_tier=Tier(p.get("model_tier", "strong")),
            base_directives=tuple(p.get("base_directives", [])),
        )

    esc_raw = raw.get("escalation", {})
    escalation = EscalationSpec(
        max_off_topic_strikes=esc_raw.get("max_off_topic_strikes", 3),
        max_injection_flags=esc_raw.get("max_injection_flags", 2),
        off_topic_decay_after_turns=esc_raw.get("off_topic_decay_after_turns", 2),
        max_stalled_verify_turns=esc_raw.get("max_stalled_verify_turns", 6),
        min_turns_before_agent_initiated_transfer=esc_raw.get(
            "min_turns_before_agent_initiated_transfer", 2
        ),
    )

    identity_raw = raw.get("identity", {})

    return SopSpec(
        name=raw["name"],
        display_name=raw.get("display_name", raw["name"]),
        phases=phases,
        escalation=escalation,
        directive_texts=raw.get("directive_texts", {}),
        refusal_templates=tuple(raw.get("refusal_templates", [])),
        identity_factor_types=tuple(
            identity_raw.get("factors", ["full_name", "dob", "phone", "email", "id_last4"])
        ),
        min_distinct_factors=identity_raw.get("min_distinct", 3),
        max_mismatches=identity_raw.get("max_mismatches", 2),
        always_available_tools=tuple(raw.get("always_available_tools", [])),
    )
