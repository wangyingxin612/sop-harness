"""Scenario runner (DESIGN.md §8.4). Runs each scenario against the SAME
orchestrator the product uses — this is deliberate: an eval harness that
exercises a separate code path from production isn't testing production.

Doubles as the `reset`/`step` shape DESIGN.md §8.4/§12 describes as an RL
environment: `EnvSession.reset()` returns an initial observation, `step()`
takes an action (a caller message) and returns the next observation plus a
programmatic, verifiable reward computed from the same invariants used here.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.config import load_settings  # noqa: E402
from app.llm.provider import LLMProvider  # noqa: E402
from app.sop.domain import DomainContext, load_domain  # noqa: E402
from app.sop.spec import SopSpec, load_spec  # noqa: E402
from app.sop.types import SessionState  # noqa: E402
from app.session.orchestrator import run_turn  # noqa: E402
from evals.invariants import run_invariants  # noqa: E402
from evals.scenario import Scenario, TurnSpec, load_all_scenarios  # noqa: E402


@dataclass
class TurnOutcome:
    turn: TurnSpec
    reply: str
    phase_after: str
    route: str
    phase_assertion_ok: bool
    route_assertion_ok: bool


@dataclass
class ScenarioResult:
    scenario: Scenario
    turn_outcomes: list[TurnOutcome] = field(default_factory=list)
    invariant_violations: dict[str, list[str]] = field(default_factory=dict)
    final_assertion_failures: list[str] = field(default_factory=list)
    total_cost_usd: float = 0.0
    total_latency_s: float = 0.0
    error: str | None = None

    @property
    def passed(self) -> bool:
        if self.error:
            return False
        if self.invariant_violations or self.final_assertion_failures:
            return False
        return all(o.phase_assertion_ok and o.route_assertion_ok for o in self.turn_outcomes)


def _check_final(scenario: Scenario, state: SessionState) -> list[str]:
    failures = []
    f = scenario.final
    if f.phase is not None and state.phase.value != f.phase:
        failures.append(f"final phase: expected {f.phase}, got {state.phase.value}")
    if f.verified_party_id is not None and state.facts.verified_party_id != f.verified_party_id:
        failures.append(f"verified_party_id: expected {f.verified_party_id}, got {state.facts.verified_party_id}")
    if f.confirmed_case_id is not None and state.memory.confirmed_case_id != f.confirmed_case_id:
        failures.append(f"confirmed_case_id: expected {f.confirmed_case_id}, got {state.memory.confirmed_case_id}")
    if f.email_sent is not None and state.facts.email_sent != f.email_sent:
        failures.append(f"email_sent: expected {f.email_sent}, got {state.facts.email_sent}")
    if f.email_skipped is not None and state.facts.email_skipped != f.email_skipped:
        failures.append(f"email_skipped: expected {f.email_skipped}, got {state.facts.email_skipped}")
    if f.caller_role is not None and state.facts.caller_role.value != f.caller_role:
        failures.append(f"caller_role: expected {f.caller_role}, got {state.facts.caller_role.value}")
    if f.consent_status is not None and state.facts.consent_status.value != f.consent_status:
        failures.append(f"consent_status: expected {f.consent_status}, got {state.facts.consent_status.value}")
    return failures


def run_scenario(
    scenario: Scenario, domain: DomainContext, spec: SopSpec, provider: LLMProvider, consent_path: str
) -> ScenarioResult:
    result = ScenarioResult(scenario=scenario)
    state = SessionState(session_id=f"eval-{scenario.id}", sop_name=spec.name, consent_scenario=scenario.consent_scenario)
    events = []
    start = time.monotonic()
    try:
        for turn in scenario.turns:
            r = run_turn(state, turn.user, domain, spec, provider, consent_path)
            state = r.state
            events.append(r.trace_event)
            phase_ok = turn.expect_phase is None or state.phase.value == turn.expect_phase
            route_ok = turn.expect_route is None or r.trace_event["plan"]["route"] == turn.expect_route
            result.turn_outcomes.append(
                TurnOutcome(
                    turn=turn, reply=r.reply, phase_after=state.phase.value,
                    route=r.trace_event["plan"]["route"], phase_assertion_ok=phase_ok, route_assertion_ok=route_ok,
                )
            )
            result.total_cost_usd += r.trace_event["cost"]["total_cost_usd"]
    except Exception as exc:  # noqa: BLE001
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    result.total_latency_s = time.monotonic() - start
    result.invariant_violations = run_invariants(events, state)
    result.final_assertion_failures = _check_final(scenario, state)
    return result


def run_all(scenarios_dir: str, filter_tag: str | None = None, filter_id: str | None = None) -> list[ScenarioResult]:
    settings = load_settings()
    spec = load_spec(BACKEND_DIR / "sops" / "insurance_claims.yaml")
    domain = load_domain(BACKEND_DIR / "fixtures", now=date.fromisoformat(settings.demo_now))
    provider = LLMProvider(settings)
    consent_path = str(BACKEND_DIR / "fixtures" / "consent_scenarios.json")

    scenarios = load_all_scenarios(scenarios_dir)
    if filter_tag:
        scenarios = [s for s in scenarios if filter_tag in s.tags]
    if filter_id:
        scenarios = [s for s in scenarios if s.id == filter_id]

    results = []
    for scenario in scenarios:
        print(f"running {scenario.id}...", flush=True)
        result = run_scenario(scenario, domain, spec, provider, consent_path)
        status = "PASS" if result.passed else "FAIL"
        print(f"  {status}  (${result.total_cost_usd:.4f}, {result.total_latency_s:.1f}s)")
        if not result.passed:
            if result.error:
                print(f"    ERROR: {result.error}")
            for name, viol in result.invariant_violations.items():
                for v in viol:
                    print(f"    [{name}] {v}")
            for f in result.final_assertion_failures:
                print(f"    [final] {f}")
            for o in result.turn_outcomes:
                if not (o.phase_assertion_ok and o.route_assertion_ok):
                    print(f"    [turn] expected phase={o.turn.expect_phase} route={o.turn.expect_route}, got phase={o.phase_after} route={o.route}")
        results.append(result)
    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default=None)
    parser.add_argument("--id", default=None)
    parser.add_argument("--dir", default=str(BACKEND_DIR / "evals" / "scenarios"))
    args = parser.parse_args()

    results = run_all(args.dir, filter_tag=args.tag, filter_id=args.id)
    passed = sum(1 for r in results if r.passed)
    total_cost = sum(r.total_cost_usd for r in results)
    print(f"\n{passed}/{len(results)} scenarios passed. Total cost: ${total_cost:.4f}")

    from evals.report import write_report
    write_report(results, BACKEND_DIR / "evals" / "reports" / "latest.json")

    sys.exit(0 if passed == len(results) else 1)
