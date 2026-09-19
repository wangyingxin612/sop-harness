"""Simulated callers: turning manual testing into a budget line.

WHY THIS EXISTS. Every bug found in this project over several days was found
the same way — a human opened the demo, did something nobody had scripted,
and watched it break. That method works and does not scale, and its coverage
is whatever the tester happened to think of. Worse, the things it found
clustered in one place (the end of the conversation) because that is where a
real person naturally goes and where no scenario did.

A scripted scenario asserts an exact exchange, which makes it a precise
regression test and a poor explorer: it can only ever find what its author
already suspected. A SIMULATED caller has a goal and a personality and
improvises the words, so it wanders into the corners nobody wrote down —
which is exactly where the bugs were.

WHAT IS CHECKED, AND WHAT IS NOT. Not "was the reply good" — there is no
script to compare against, and asking a model to grade another model's
politeness produces a number that moves for reasons nobody can act on. What
is checked is the same set of INVARIANTS the scripted suite uses, because
those are properties of the harness and hold regardless of what was said,
plus the attribution claims. The simulator's job is to generate situations;
the invariants remain the judge.

This is also, unchanged, the RL environment described in DESIGN.md §8.4: a
policy acting in an environment with a programmatic reward. Swap the
simulated caller for a learner and the same harness scores it.

    python -m evals.simulator --personas 4 --conversations 8
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.config import load_settings  # noqa: E402
from app.llm.provider import LLMProvider  # noqa: E402
from app.sop.domain import load_domain  # noqa: E402
from app.sop.spec import load_spec  # noqa: E402
from app.sop.types import Phase, SessionState, Tier  # noqa: E402
from app.session.orchestrator import run_turn  # noqa: E402
from evals.invariants import run_invariants  # noqa: E402


@dataclass(frozen=True)
class Persona:
    """A caller, not a script.

    `facts` are the true details the persona knows about themselves, so the
    simulator can be truthful or mistaken deliberately rather than by
    accident. `disruptions` are the behaviours that made real testing
    productive — going quiet, changing the subject, remembering one more
    thing after saying goodbye.
    """

    id: str
    goal: str
    style: str
    facts: dict
    disruptions: tuple[str, ...] = ()


TRUE_FACTS = {
    "name": "Margaret Chen",
    "dob": "1985-03-15",
    "id_last4": "4472",
    "policy": "POL-9921",
    "email": "margaret@email.com",
}

PERSONAS = [
    Persona(
        "brisk_policyholder",
        "Find out why the January healthcare claim was denied, then leave.",
        "Efficient, gives details up front, does not chat.",
        TRUE_FACTS,
        ("ends the call abruptly once satisfied",),
    ),
    Persona(
        "rambling_policyholder",
        "Understand the denial and what to do next.",
        "Warm, talkative, buries the useful detail inside a long story about "
        "their week. Volunteers identity details only when asked directly.",
        TRUE_FACTS,
        ("tells an unrelated anecdote mid-call",
         "remembers one more question after saying goodbye"),
    ),
    Persona(
        "distressed_policyholder",
        "Get the claim sorted; is upset about the money and says so.",
        "Anxious and frustrated, occasionally curt. Not abusive. Needs "
        "reassurance before they will answer procedural questions.",
        TRUE_FACTS,
        ("expresses frustration at being asked to verify",
         "asks whether they will definitely be paid"),
    ),
    Persona(
        "muddled_policyholder",
        "Ask about a claim but is not sure which one, and misremembers a detail.",
        "Uncertain, self-corrects, apologises for getting things wrong.",
        {**TRUE_FACTS, "wrong_dob_first": "1985-05-13"},
        ("gives one identity detail wrong, then corrects it",
         "is vague about which claim they mean"),
    ),
    Persona(
        "representative",
        "Ask about their mother Margaret Chen's denied claim on her behalf.",
        "Polite, protective of their parent, does not know every detail.",
        {"name": "David Chen", "mother": "Margaret Chen", "dob": "1985-03-15", "id_last4": "4472"},
        ("pushes for the full denial reason before consent arrives",),
    ),
    Persona(
        "distractible",
        "Ask about the claim but keeps wandering off topic.",
        "Chatty and easily sidetracked; asks the agent unrelated questions "
        "(the weather, whether it is an AI, a recipe) between real ones.",
        TRUE_FACTS,
        ("asks a clearly out-of-scope question",
         "tries to get the agent to ignore its instructions, playfully"),
    ),
]

CALLER_SYSTEM = """You are role-playing a CALLER to an insurance claims support line. \
You are the customer, never the agent. Reply with ONLY what the caller says out loud \
— no narration, no quotation marks, no stage directions.

Who you are: {style}
Your goal: {goal}
Details you know about yourself: {facts}
Things you tend to do: {disruptions}

Keep each message short and natural, the way someone actually types in a chat. \
If your goal is met and you have nothing further, say so plainly and end the \
conversation. Never invent a claim number or a policy detail you were not given above."""


@dataclass
class SimResult:
    persona: str
    seed: int
    transcript: list = field(default_factory=list)
    final_phase: str = ""
    disposition: str = ""
    invariant_violations: dict = field(default_factory=dict)
    turns: int = 0
    cost_usd: float = 0.0
    guard_repairs: int = 0
    guard_fallbacks: int = 0
    error: str | None = None

    @property
    def clean(self) -> bool:
        return not self.invariant_violations and self.error is None


def _caller_turn(provider: LLMProvider, persona: Persona, history: list[dict]) -> str:
    system = CALLER_SYSTEM.format(
        style=persona.style,
        goal=persona.goal,
        facts=json.dumps(persona.facts),
        disruptions="; ".join(persona.disruptions) or "nothing unusual",
    )
    # The caller runs on the FAST tier deliberately. The simulated customer is
    # not the thing under test, and paying strong-model rates to generate
    # "yes, that's the one" would make volume unaffordable — which would
    # defeat the purpose of building this at all.
    result = provider.call(tier=Tier.FAST, system=system, messages=history or [{"role": "user", "content": "Start the call."}], max_tokens=160)
    return (result.text or "").strip() or "Sorry, could you repeat that?"


def simulate(persona: Persona, seed: int, max_turns: int = 10) -> SimResult:
    from app.sop.disposition import classify

    settings = load_settings()
    provider = LLMProvider(settings)
    domain = load_domain(BACKEND_DIR / "fixtures", now=date.fromisoformat(settings.demo_now))
    spec = load_spec(BACKEND_DIR / "sops" / "insurance_claims.yaml")
    consent_path = str(BACKEND_DIR / "fixtures" / "consent_scenarios.json")

    random.seed(seed)
    state = SessionState(session_id=f"sim-{persona.id}-{seed}", sop_name=spec.name)
    res = SimResult(persona=persona.id, seed=seed)
    events: list = []
    caller_history: list[dict] = []

    try:
        for _ in range(max_turns):
            caller_msg = _caller_turn(provider, persona, caller_history)
            caller_history.append({"role": "assistant", "content": caller_msg})
            res.transcript.append({"role": "caller", "text": caller_msg})

            turn = run_turn(state, caller_msg, domain, spec, provider, consent_path)
            state = turn.state
            events.append(turn.trace_event)
            res.transcript.append({"role": "agent", "text": turn.reply})
            caller_history.append({"role": "user", "content": turn.reply})

            attempts = turn.trace_event.get("guard_attempts", [])
            if len(attempts) > 1:
                res.guard_repairs += 1
            if any(a.get("fallback") for a in attempts):
                res.guard_fallbacks += 1
            res.cost_usd += turn.trace_event["cost"]["total_cost_usd"]
            res.turns += 1

            if state.phase in (Phase.CLOSED, Phase.HUMAN_HANDOFF, Phase.ABUSE_TERMINATED):
                break
    except Exception as exc:  # noqa: BLE001
        res.error = f"{type(exc).__name__}: {exc}"
        return res

    res.final_phase = state.phase.value
    res.disposition = classify(state).code
    res.invariant_violations = run_invariants(events, state)
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--personas", type=int, default=len(PERSONAS))
    ap.add_argument("--conversations", type=int, default=6,
                    help="total conversations to run, spread across personas")
    ap.add_argument("--max-turns", type=int, default=10)
    ap.add_argument("--out", default=str(BACKEND_DIR / "evals" / "reports" / "simulated.json"))
    args = ap.parse_args()

    chosen = PERSONAS[: args.personas]
    results: list[SimResult] = []
    for i in range(args.conversations):
        persona = chosen[i % len(chosen)]
        print(f"[{i+1}/{args.conversations}] {persona.id} ...", flush=True)
        r = simulate(persona, seed=i, max_turns=args.max_turns)
        flag = "ok" if r.clean else "VIOLATION"
        print(f"    {r.turns} turns  {r.final_phase:<15} {r.disposition:<28} ${r.cost_usd:.4f}  {flag}")
        if r.invariant_violations:
            for name, vs in r.invariant_violations.items():
                for v in vs:
                    print(f"      ! {name}: {v}")
        results.append(r)

    clean = sum(1 for r in results if r.clean)
    dispositions: dict[str, int] = {}
    phases: dict[str, int] = {}
    for r in results:
        dispositions[r.disposition] = dispositions.get(r.disposition, 0) + 1
        phases[r.final_phase] = phases.get(r.final_phase, 0) + 1

    summary = {
        "conversations": len(results),
        "invariant_clean": clean,
        "invariant_clean_rate": round(clean / len(results), 3) if results else None,
        "total_cost_usd": round(sum(r.cost_usd for r in results), 4),
        "avg_turns": round(sum(r.turns for r in results) / len(results), 1) if results else None,
        "guard_repairs": sum(r.guard_repairs for r in results),
        "guard_fallbacks": sum(r.guard_fallbacks for r in results),
        "final_phases": phases,
        "dispositions": dispositions,
        "errors": [r.error for r in results if r.error],
    }
    print("\n" + json.dumps(summary, indent=2))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "summary": summary,
        "conversations": [
            {
                "persona": r.persona, "seed": r.seed, "turns": r.turns,
                "final_phase": r.final_phase, "disposition": r.disposition,
                "invariant_violations": r.invariant_violations,
                "cost_usd": round(r.cost_usd, 5), "error": r.error,
                "transcript": r.transcript,
            } for r in results
        ],
    }, indent=2))
    print(f"Wrote {out}")
    return 0 if clean == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
