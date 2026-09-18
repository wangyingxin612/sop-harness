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
