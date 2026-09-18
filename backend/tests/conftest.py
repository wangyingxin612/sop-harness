from datetime import date
from pathlib import Path

import pytest

from app.sop.domain import load_domain
from app.sop.spec import load_spec

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SOPS = Path(__file__).resolve().parents[1] / "sops"
DEMO_NOW = date(2026, 2, 20)  # keeps CL-2048's 2026-03-18 appeal deadline live


@pytest.fixture()
def spec():
    return load_spec(SOPS / "insurance_claims.yaml")


@pytest.fixture()
def domain():
    return load_domain(FIXTURES, now=DEMO_NOW)


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Point the session store and the client-event log at a temp directory
    for every test.

    Without this, API tests write real session files into `runs/` — so the
    operations board fills up with empty sessions nobody had, and the idle
    metrics count them. A test suite that quietly contaminates the product's
    own reporting is worse than one that does not run at all, because the
    numbers still look plausible.
    """
    from app.obs import idle_metrics
    from app.session.store import STORE

    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(STORE, "runs_dir", runs)
    monkeypatch.setattr(STORE, "_sessions", {})
    monkeypatch.setattr(STORE, "_api_keys", {})
    monkeypatch.setattr(idle_metrics, "EVENTS_PATH", runs / "client_events.jsonl")
    yield
