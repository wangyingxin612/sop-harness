"""Scenario schema (DESIGN.md §8.4). A scenario is a scripted caller side of
a conversation plus assertions — deterministic, reproducible, and cheap
enough to run against the real API repeatedly. This is intentionally
scripted rather than LLM-simulated-caller for this build (see DESIGN.md §9.2
P2/P3 backlog for the adversarial-persona simulator); a fixed script is what
let this suite catch six real bugs during the first live runs (EVAL.md §4).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class TurnSpec:
    user: str
    expect_phase: str | None = None
    expect_route: str | None = None
    note: str = ""


@dataclass
class ScenarioFinal:
    phase: str | None = None
    verified_party_id: str | None = None
    confirmed_case_id: str | None = None
    email_sent: bool | None = None
    email_skipped: bool | None = None
    caller_role: str | None = None
    consent_status: str | None = None


@dataclass
class Scenario:
    id: str
    description: str
    turns: list[TurnSpec]
    consent_scenario: str = "default"
    final: ScenarioFinal = field(default_factory=ScenarioFinal)
    tags: list[str] = field(default_factory=list)


def load_scenario(path: str | Path) -> Scenario:
    raw = yaml.safe_load(Path(path).read_text())
    turns = [TurnSpec(**t) for t in raw["turns"]]
    final_raw = raw.get("final", {})
    return Scenario(
        id=raw["id"],
        description=raw.get("description", ""),
        turns=turns,
        consent_scenario=raw.get("consent_scenario", "default"),
        final=ScenarioFinal(**final_raw),
        tags=raw.get("tags", []),
    )


def load_all_scenarios(directory: str | Path) -> list[Scenario]:
    directory = Path(directory)
    return [load_scenario(p) for p in sorted(directory.glob("*.yaml"))]
