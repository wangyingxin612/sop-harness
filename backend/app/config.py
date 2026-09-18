"""Process-wide configuration, loaded once from the environment.

R10: "Setup must accept an API auth token for calling an AI model." We
support that two ways — an env var for local/Docker runs, and a per-session
token entered in the UI for the hosted demo (app/api/routes.py) — so a
reviewer can use either delivery path without editing files.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[2]  # sop-harness/
load_dotenv(_ROOT / ".env")

BACKEND_DIR = Path(__file__).resolve().parents[1]
FIXTURES_DIR = BACKEND_DIR / "fixtures"
SOPS_DIR = BACKEND_DIR / "sops"


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str | None
    model_strong: str
    model_fast: str
    demo_now: str
    llm_base_url: str | None = None
    # Forces every call to one tier regardless of what the TurnPlan asked
    # for. Used by the cost experiment (evals/cost_experiment.py) to measure
    # what per-phase routing actually buys; never set in normal operation.
    tier_override: str | None = None


def load_settings() -> Settings:
    return Settings(
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        model_strong=os.environ.get("MODEL_STRONG", "claude-sonnet-5"),
        model_fast=os.environ.get("MODEL_FAST", "claude-haiku-4-5-20251001"),
        demo_now=os.environ.get("DEMO_NOW", "2026-02-20"),
        llm_base_url=os.environ.get("LLM_BASE_URL"),
        tier_override=os.environ.get("TIER_OVERRIDE") or None,
    )
