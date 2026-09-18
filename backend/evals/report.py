"""Eval report (DESIGN.md §7.6/§8.4): containment rate reported beside the
safety invariants, with transfer attribution broken down — an aggregate
"X% passed" number doesn't say what to fix next; attribution does.
"""
from __future__ import annotations

import json
from pathlib import Path


def _attribution(result) -> str | None:
    """Why a scenario ended in a human/abuse terminal phase, if it did."""
    if result.error:
        return None
    final_phase = result.turn_outcomes[-1].phase_after if result.turn_outcomes else None
    if final_phase == "ABUSE_TERMINATED":
        return "off_topic_persistence"
    if final_phase == "HUMAN_HANDOFF":
        return "escalation_or_gate"  # refined by scenario tags below
    return None


def summarize(results) -> dict:
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    contained = sum(
        1 for r in results
        if r.turn_outcomes and r.turn_outcomes[-1].phase_after not in ("HUMAN_HANDOFF", "ABUSE_TERMINATED")
    )
    attribution: dict[str, int] = {}
    for r in results:
        attr = _attribution(r)
        if attr:
            key = attr
            if "adversarial" in r.scenario.tags or "abuse" in r.scenario.tags:
                key = attr
            elif "representative" in r.scenario.tags:
                key = "gate_pending_consent"
            attribution[key] = attribution.get(key, 0) + 1

    total_cost = sum(r.total_cost_usd for r in results)
    # Guard-intervention rate: how often a draft had to be repaired or
    # replaced. DESIGN.md §8.3 — this is a free model-quality signal, and a
    # rising number after a prompt/model change is an early warning even
    # when every scenario still passes.
    total_turns = sum(len(r.turn_outcomes) for r in results)
    repaired = sum(
        1 for r in results for o in r.turn_outcomes if len(o.guard_attempts) > 1
    )
    fell_back = sum(1 for r in results for o in r.turn_outcomes if o.used_fallback)
    return {
        "total_scenarios": total,
        "passed": passed,
        "pass_rate": round(passed / total, 3) if total else None,
        "containment_rate": round(contained / total, 3) if total else None,
        "transfer_attribution": attribution,
        "total_cost_usd": round(total_cost, 4),
        "avg_cost_usd_per_scenario": round(total_cost / total, 4) if total else None,
        "total_turns": total_turns,
        "guard_repair_rate": round(repaired / total_turns, 3) if total_turns else None,
        "guard_fallback_rate": round(fell_back / total_turns, 3) if total_turns else None,
    }


def write_report(results, out_path: str | Path) -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "summary": summarize(results),
        "scenarios": [
            {
                "id": r.scenario.id,
                "description": r.scenario.description,
                "tags": r.scenario.tags,
                "passed": r.passed,
                "error": r.error,
                "invariant_violations": r.invariant_violations,
                "final_assertion_failures": r.final_assertion_failures,
                "cost_usd": round(r.total_cost_usd, 5),
                "latency_s": round(r.total_latency_s, 2),
                "turns": [
                    {
                        "user": o.turn.user,
                        "reply": o.reply,
                        "phase_after": o.phase_after,
                        "route": o.route,
                        "phase_ok": o.phase_assertion_ok,
                        "route_ok": o.route_assertion_ok,
                        "used_fallback": o.used_fallback,
                        "guard_attempts": o.guard_attempts,
                        "tool_effects": o.tool_effects,
                    }
                    for o in r.turn_outcomes
                ],
            }
            for r in results
        ],
    }
    out_path.write_text(json.dumps(report, indent=2))
    print(f"\nWrote report to {out_path}")
    print(json.dumps(report["summary"], indent=2))
