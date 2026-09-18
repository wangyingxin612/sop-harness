"""Cost/quality experiment (DESIGN.md §8.3, §9.2 P1).

DESIGN.md claims per-phase model routing makes the harness cheaper without
costing quality. That is a claim, not a result, until it's measured against
the alternative — so this runs the identical scenario suite under three
configurations and reports quality and cost side by side:

    all-strong   every call on the strong model (the "just use the best
                 model" baseline most people ship)
    routed       what the SOP spec actually asks for: fast tier for
                 extraction and for STRICT phases, strong for the rest
    all-fast     every call on the cheap model (the "just use the cheap
                 model" temptation)

The interesting column is not cost — it's `guard_repair_rate` alongside it.
Cost tells you what you saved; the guard rate tells you whether you paid for
it in behavior. Reporting one without the other is how teams talk themselves
into a downgrade that quietly breaks (DESIGN.md §12: the safety layer is
what makes the cost reduction *decidable*).

    python -m evals.cost_experiment            # all three configs
    python -m evals.cost_experiment --configs routed all-fast
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

CONFIGS = {
    "all-strong": "strong",
    "routed": None,       # honour the per-phase tier in the SOP spec
    "all-fast": "fast",
}


def run_config(name: str, override: str | None, scenarios_dir: str) -> dict:
    # The override is read at Settings load time, so it has to be in the
    # environment before the runner imports/constructs anything.
    if override:
        os.environ["TIER_OVERRIDE"] = override
    else:
        os.environ.pop("TIER_OVERRIDE", None)

    # Imported inside the function so each config picks up fresh settings.
    for mod in [m for m in list(sys.modules) if m.startswith(("app.", "evals."))]:
        del sys.modules[mod]
    from evals.report import summarize  # noqa: PLC0415
    from evals.runner import run_all  # noqa: PLC0415

    print(f"\n=== {name} (tier_override={override or 'none'}) ===", flush=True)
    results = run_all(scenarios_dir)
    summary = summarize(results)
    summary["config"] = name
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--configs", nargs="*", default=list(CONFIGS))
    parser.add_argument("--dir", default=str(BACKEND_DIR / "evals" / "scenarios"))
    parser.add_argument("--out", default=str(BACKEND_DIR / "evals" / "reports" / "cost_experiment.json"))
    args = parser.parse_args()

    summaries = [run_config(name, CONFIGS[name], args.dir) for name in args.configs]

    print("\n" + "=" * 92)
    print(f"{'config':<12} {'pass':>7} {'contain':>8} {'repair':>8} {'fallback':>9} {'cost':>9} {'$/scenario':>11}")
    print("-" * 92)
    for s in summaries:
        print(
            f"{s['config']:<12} "
            f"{s['passed']}/{s['total_scenarios']:<5} "
            f"{s['containment_rate']:>8.3f} "
            f"{s['guard_repair_rate']:>8.3f} "
            f"{s['guard_fallback_rate']:>9.3f} "
            f"${s['total_cost_usd']:>8.3f} "
            f"${s['avg_cost_usd_per_scenario']:>10.4f}"
        )
    print("=" * 92)

    baseline = next((s for s in summaries if s["config"] == "all-strong"), None)
    routed = next((s for s in summaries if s["config"] == "routed"), None)
    if baseline and routed and baseline["total_cost_usd"]:
        saved = 1 - routed["total_cost_usd"] / baseline["total_cost_usd"]
        print(f"\nrouted vs all-strong: {saved:+.1%} cost, "
              f"pass {routed['passed']}/{routed['total_scenarios']} vs {baseline['passed']}/{baseline['total_scenarios']}, "
              f"guard repair {routed['guard_repair_rate']:.3f} vs {baseline['guard_repair_rate']:.3f}")

    Path(args.out).write_text(json.dumps(summaries, indent=2))
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
