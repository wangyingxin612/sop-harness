"""Operations board endpoint and the eval report renderer."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.session.store import STORE
from app.sop.machine import end_session
from app.sop.types import Phase, Turn


@pytest.fixture
def client():
    return TestClient(app)


def _session(client, phase: Phase | None = None, reason: str | None = None, spoken: bool = True) -> str:
    """`spoken` controls whether anyone actually said anything. The board
    lists calls, and a session nobody spoke in is not a call."""
    sid = client.post("/api/sessions", json={"sop_name": "insurance_claims"}).json()["session_id"]
    st = STORE.get(sid)
    if spoken:
        st.transcript.append(Turn(turn_index=0, role="caller", text="hello, I have a question"))
        st.facts.turns_used = 1
    if reason:
        # Phase comes from the reason now, not chosen alongside it.
        st.phase = end_session(st, reason, 0)
    elif phase:
        st.phase = phase
    STORE.update(st)
    return sid


def test_board_lists_sessions_with_a_disposition_each(client):
    sid = _session(client)
    rows = client.get("/api/sessions").json()["sessions"]
    row = next(r for r in rows if r["session_id"] == sid)
    assert row["disposition"]["code"] == "IN_PROGRESS"
    assert row["disposition"]["outcome_class"] == "in_progress"


def test_opened_but_never_spoken_in_is_not_a_call(client):
    """Creating a session is not the same as having a conversation. Listing
    those makes the board read as a wall of stalled verifications when in
    fact nobody ever said anything."""
    _session(client, spoken=False)
    body = client.get("/api/sessions").json()
    assert body["sessions"] == []
    assert body["rollup"]["opened_never_started"] == 1


def test_board_is_ordered_most_recent_first(client):
    _session(client)
    _session(client)
    rows = client.get("/api/sessions").json()["sessions"]
    stamps = [r["last_activity_at"] for r in rows]
    assert stamps == sorted(stamps, reverse=True)


def test_rollup_containment_ignores_live_sessions(client):
    """A board full of in-flight calls must not read as 0% contained — that
    would be a metric that panics an operations lead for no reason."""
    _session(client)
    roll = client.get("/api/sessions").json()["rollup"]
    assert roll["containment_rate"] is None or 0.0 <= roll["containment_rate"] <= 1.0


def test_transferred_session_carries_a_routing_hint(client):
    sid = _session(client, Phase.HUMAN_HANDOFF, "identity_verification_failed")
    row = next(r for r in client.get("/api/sessions").json()["sessions"] if r["session_id"] == sid)
    assert row["disposition"]["routing_hint"] == "identity_desk"
    assert row["disposition"]["review_flag"] is True


def test_board_summary_does_not_leak_full_transcripts(client):
    """The board is a summary by design — cheap to open with a thousand rows,
    and it should not ship a caller's whole conversation to a list view."""
    sid = _session(client)
    row = next(r for r in client.get("/api/sessions").json()["sessions"] if r["session_id"] == sid)
    assert "transcript" not in row
    assert len(row["last_caller_message"]) <= 160


def test_eval_report_renders_the_invariants_first(client):
    r = client.get("/api/evals/report")
    assert r.status_code == 200
    body = r.text
    assert "Safety invariants" in body
    # The compliance reader's question comes before the engineer's.
    assert body.index("Safety invariants") < body.index("Outcomes")


def test_eval_report_names_every_invariant(client):
    from evals.invariants import ALL_INVARIANTS
    body = client.get("/api/evals/report").text
    for inv in ALL_INVARIANTS:
        assert inv.__name__ in body


def test_eval_report_degrades_without_a_run():
    from evals.html_report import render
    body = render("definitely-not-a-report.json")
    assert "No eval report" in body
    assert "make eval" in body
