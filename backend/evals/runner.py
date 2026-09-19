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
from evals.typo_noise import apply_noise  # noqa: E402
from evals.scenario import Scenario, TurnSpec, load_all_scenarios  # noqa: E402


def _flatten_keys(d, depth: int = 0) -> set:
    """Every key name anywhere in visible_facts.

    Keys, never values: this is used to assert that claim data was ABSENT
    from the model's context during VERIFY_ID, and a report that quoted the
    values in order to prove they were withheld would be self-defeating.
    """
    keys = set()
    if depth > 6 or not isinstance(d, dict):
        return keys
    for k, v in d.items():
        keys.add(k)
        if isinstance(v, dict):
            keys |= _flatten_keys(v, depth + 1)
        elif isinstance(v, list):
            for item in v:
                keys |= _flatten_keys(item, depth + 1)
    return keys


@dataclass
class TurnOutcome:
    turn: TurnSpec
    sent_text: str          # what was actually sent (differs from turn.user under noise)
    reply: str
    phase_after: str
    route: str
    phase_assertion_ok: bool
    route_assertion_ok: bool
    # Diagnostics: without these, a failing scenario tells you WHAT broke but
    # not WHY, and the only way to find out is to re-run and hope the
    # non-determinism reproduces. Cheap to record, expensive to be without.
    guard_attempts: list = field(default_factory=list)
    tool_effects: list = field(default_factory=list)
    used_fallback: bool = False
    # CONTROL-PLANE FACTS. These were being thrown away, which meant the
    # suite could only check OUTCOMES: "the email was sent" passed
    # identically whether the control plane commanded it or the model
    # improvised it. That is the hole `SEND_NOW` lived in for the whole
    # project — the directive never fired once, the model volunteered the
    # tool call from pending_action, and 12/12 stayed green throughout.
    # Recording these is what makes enforcement checkable (evals/attribution.py).
    phase_before: str = ""
    plan_phase: str = ""        # the phase this turn's rules came from
    directives: list = field(default_factory=list)
    allowed_tools: list = field(default_factory=list)
    visible_fact_keys: list = field(default_factory=list)
    model_tier: str = ""
    latency_s: dict = field(default_factory=dict)
    signals: dict = field(default_factory=dict)


@dataclass
class ScenarioResult:
    scenario: Scenario
    noise_profile: str | None = None
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
    scenario: Scenario,
    domain: DomainContext,
    spec: SopSpec,
    provider: LLMProvider,
    consent_path: str,
    noise_profile: str | None = None,
) -> ScenarioResult:
    """`noise_profile` re-runs the SAME scenario with ASR-corrupted inputs
    (evals/typo_noise.py). Same assertions, harder input — which is the whole
    point: a robustness suite that needed its own expectations wouldn't be
    measuring robustness, it would be measuring a different product."""
    result = ScenarioResult(scenario=scenario, noise_profile=noise_profile)
    state = SessionState(session_id=f"eval-{scenario.id}", sop_name=spec.name, consent_scenario=scenario.consent_scenario)
    events = []
    start = time.monotonic()
    try:
        for i, turn in enumerate(scenario.turns):
            user_text = apply_noise(turn.user, noise_profile, seed=i) if noise_profile else turn.user
            r = run_turn(state, user_text, domain, spec, provider, consent_path)
            state = r.state
            events.append(r.trace_event)
            phase_ok = turn.expect_phase is None or state.phase.value == turn.expect_phase
            route_ok = turn.expect_route is None or r.trace_event["plan"]["route"] == turn.expect_route
            attempts = r.trace_event.get("guard_attempts", [])
            plan = r.trace_event["plan"]
            result.turn_outcomes.append(
                TurnOutcome(
                    turn=turn, sent_text=user_text, reply=r.reply, phase_after=state.phase.value,
                    route=plan["route"], phase_assertion_ok=phase_ok, route_assertion_ok=route_ok,
                    guard_attempts=attempts,
                    tool_effects=r.trace_event.get("tool_effects", []),
                    used_fallback=any(a.get("fallback") for a in attempts),
                    phase_before=r.trace_event["phase_before"],
                    plan_phase=plan.get("phase", ""),
                    directives=list(plan.get("directives", [])),
                    allowed_tools=list(plan.get("allowed_tools", [])),
                    visible_fact_keys=sorted(_flatten_keys(plan.get("visible_facts") or {})),
                    model_tier=plan.get("model_tier", ""),
                    latency_s=r.trace_event.get("latency_s", {}),
                    signals=r.trace_event.get("signals", {}),
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


def run_all(
    scenarios_dir: str,
    filter_tag: str | None = None,
    filter_id: str | None = None,
    noise_profile: str | None = None,
) -> list[ScenarioResult]:
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
        result = run_scenario(scenario, domain, spec, provider, consent_path, noise_profile=noise_profile)
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
    parser.add_argument(
        "--noise", default=None, choices=["light", "moderate", "heavy"],
        help="re-run the same scenarios with ASR-style corrupted input (evals/typo_noise.py)",
    )
    args = parser.parse_args()

    if args.noise:
        print(f"ASR-noise profile: {args.noise}\n")
    results = run_all(args.dir, filter_tag=args.tag, filter_id=args.id, noise_profile=args.noise)
    passed = sum(1 for r in results if r.passed)
    total_cost = sum(r.total_cost_usd for r in results)
    print(f"\n{passed}/{len(results)} scenarios passed. Total cost: ${total_cost:.4f}")

    from evals.report import write_report
    write_report(results, BACKEND_DIR / "evals" / "reports" / "latest.json")

    sys.exit(0 if passed == len(results) else 1)
