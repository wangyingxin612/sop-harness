"""SOP state-space coverage.

The suite used to be a hand-written list of situations someone thought of.
That is fine until you ask the obvious question — *what did we NOT think of?*
— which a list cannot answer about itself.

The SOP is a state machine with a finite, enumerable surface: phases,
transitions between them, directives, tools, routes, disposition codes. So
coverage is computable rather than felt. When an audit finally ran this, it
found that none of the twelve original scenarios ever reached CLOSED and six
of fourteen disposition codes were unreachable — and every bug found by hand
over the preceding days lived in exactly that unlit region.

This is the mechanism that replaces "click around and see what breaks": the
untested area announces itself instead of waiting to be stumbled into.

Coverage is a floor, never a ceiling. Exercising every transition does not
mean the behaviour on each is right — that is what the invariants and
attribution claims are for. It only means no part of the machine is dark.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

BACKEND = Path(__file__).resolve().parents[1]


def _declared_directives() -> set[str]:
    """Every directive the system can emit: the Directive() constants in
    policy.py plus the directive_texts each SOP declares."""
    ids = set(re.findall(r'^\s*id="([A-Z_]+)"', (BACKEND / "app/sop/policy.py").read_text(), re.M))
    for f in (BACKEND / "sops").glob("*.yaml"):
        ids |= set(yaml.safe_load(f.read_text()).get("directive_texts", {}))
    return ids


def _declared_phases() -> set[str]:
    from app.sop.types import Phase
    return {p.value for p in Phase}


def _declared_dispositions() -> set[str]:
    from app.sop.disposition import DISPOSITIONS
    return set(DISPOSITIONS)


def analyse(report: dict) -> dict:
    seen_directives: set[str] = set()
    seen_transitions: set[tuple[str, str]] = set()
    seen_phases: set[str] = set()
    seen_routes: set[str] = set()
    seen_tools: set[str] = set()

    for sc in report["scenarios"]:
        for t in sc.get("turns", []):
            seen_directives |= set(t.get("directives") or [])
            before, after = t.get("phase_before"), t.get("phase_after")
            if before and after:
                seen_transitions.add((before, after))
            if before:
                seen_phases.add(before)
            if after:
                seen_phases.add(after)
            if t.get("route"):
                seen_routes.add(t["route"])
            for e in t.get("tool_effects") or []:
                seen_tools.add(e.get("tool", "") if isinstance(e, dict) else str(e))

    declared_directives = _declared_directives()
    declared_phases = _declared_phases()

    # Terminal phases have no outgoing transitions by definition, so they are
    # excluded from the "phases entered" denominator only as SOURCES, never
    # as destinations — a suite that never REACHES a terminal phase is
    # exactly the gap this module was written to catch.
    unreached_phases = sorted(declared_phases - seen_phases)
    unfired_directives = sorted(declared_directives - seen_directives)

    def pct(seen: int, total: int):
        return round(seen / total, 3) if total else None

    return {
        "phases": {
            "declared": len(declared_phases),
            "reached": len(seen_phases & declared_phases),
            "coverage": pct(len(seen_phases & declared_phases), len(declared_phases)),
            "unreached": unreached_phases,
        },
        "directives": {
            "declared": len(declared_directives),
            "fired": len(seen_directives & declared_directives),
            "coverage": pct(len(seen_directives & declared_directives), len(declared_directives)),
            "never_fired": unfired_directives,
        },
        "transitions": {
            "observed": sorted(f"{a} -> {b}" for a, b in seen_transitions),
            "count": len(seen_transitions),
        },
        "routes": sorted(seen_routes),
        "tools_executed": sorted(t for t in seen_tools if t),
        "dispositions_declared": len(_declared_dispositions()),
    }


def gaps(cov: dict) -> list[str]:
    """One line per hole, phrased as the scenario that is missing — a
    coverage report that only prints percentages tells you that you have a
    problem without telling you what to write."""
    out = []
    for p in cov["phases"]["unreached"]:
        out.append(f"No scenario ever reaches phase {p}")
    for d in cov["directives"]["never_fired"]:
        out.append(f"Directive {d} never fires — no scenario creates the condition for it")
    return out
