"""Wall-clock consent polling (app/api/main.py + machine.poll_pending_consent).

The behaviour under test is the one the original implementation claimed but
did not have: consent resolves on its own schedule, and the agent observes
the result. Previously it only advanced inside transition(), i.e. once per
caller turn — so a representative who asked for authorisation and then waited
quietly, which is the obvious thing to do, waited forever.
"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.session.store import STORE
from app.sop.types import ConsentStatus, Phase


@pytest.fixture
def client():
    return TestClient(app)


def _pending_session(client) -> str:
    sid = client.post("/api/sessions", json={"sop_name": "insurance_claims"}).json()["session_id"]
    st = STORE.get(sid)
    st.facts.consent_status = ConsentStatus.PENDING
    STORE.update(st)
    return sid


def test_consent_resolves_without_any_caller_turn(client):
    sid = _pending_session(client)
    # default scenario: ["pending", "approved"] -> second poll approves
    first = client.post(f"/api/sessions/{sid}/consent/poll").json()
    assert first["state"]["facts"]["consent_status"] == "pending"
    assert first["changed"] is False

    second = client.post(f"/api/sessions/{sid}/consent/poll").json()
    assert second["state"]["facts"]["consent_status"] == "approved"
    assert second["changed"] is True

    # and no caller message was ever sent
    assert STORE.get(sid).transcript == []


def test_timeout_scenario_reaches_timeout_by_waiting(client):
    sid = client.post(
        "/api/sessions", json={"sop_name": "insurance_claims", "consent_scenario": "timeout"}
    ).json()["session_id"]
    st = STORE.get(sid)
    st.facts.consent_status = ConsentStatus.PENDING
    STORE.update(st)

    for _ in range(5):
        client.post(f"/api/sessions/{sid}/consent/poll")
    assert STORE.get(sid).facts.consent_status == ConsentStatus.TIMED_OUT


def test_polling_a_settled_request_is_a_no_op(client):
    sid = _pending_session(client)
    client.post(f"/api/sessions/{sid}/consent/poll")
    client.post(f"/api/sessions/{sid}/consent/poll")   # -> approved
    before = STORE.get(sid).facts.consent_poll_count

    r = client.post(f"/api/sessions/{sid}/consent/poll").json()
    assert r["changed"] is False
    assert STORE.get(sid).facts.consent_poll_count == before


def test_poll_costs_nothing(client):
    """An observation of external state must not be billed to the caller —
    and must not involve the model at all."""
    sid = _pending_session(client)
    before = STORE.get(sid).facts.cost_usd
    client.post(f"/api/sessions/{sid}/consent/poll")
    after = STORE.get(sid)
    assert after.facts.cost_usd == before
    assert after.facts.tokens_used == 0


def test_client_is_told_how_long_the_wait_can_last(client):
    """An unbounded 'please wait' is the thing this strip exists to avoid."""
    sid = _pending_session(client)
    policy = client.get(f"/api/sessions/{sid}").json()["consent_policy"]
    assert policy["checks_before_timeout"] == 2
    assert policy["poll_interval_seconds"] > 0


def test_unknown_session_is_404_not_a_silent_ok(client):
    assert client.post("/api/sessions/nope/consent/poll").status_code == 404
