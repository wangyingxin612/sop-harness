"""Server-side abandonment sweep (app/session/sweeper.py).

The behaviour under test is what happens when the BROWSER GOES AWAY. The idle
ladder lives in the client, so closing the tab takes the thing that was going
to close the session with it — and the session then shows as in-flight on the
operations board forever, never entering the containment denominator.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.session.sweeper import idle_seconds, last_caller_activity, sweep
from app.sop.disposition import classify
from app.sop.spec import load_spec
from app.sop.types import Phase, SessionState, Turn

SOPS = __import__("pathlib").Path(__file__).resolve().parents[1] / "sops"


def _spec(name: str):
    return load_spec(SOPS / f"{name}.yaml")


def _at(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _session(minutes_since_caller: float, phase=Phase.PROCESS_CASE) -> SessionState:
    st = SessionState(session_id="s", sop_name="insurance_claims", phase=phase)
    st.created_at = _at(minutes_since_caller + 5)
    st.transcript = [
        Turn(turn_index=0, role="caller", text="hello", ts=_at(minutes_since_caller)),
        # The agent replied AFTER the caller — this must not reset the clock.
        Turn(turn_index=1, role="agent", text="hi", ts=_at(minutes_since_caller - 0.1)),
    ]
    return st


class FakeStore:
    def __init__(self, sessions):
        self.sessions = sessions
        self.updated = []

    def list_sessions(self):
        return list(self.sessions)

    def get(self, sid):
        return self.sessions.get(sid)

    def update(self, state):
        self.updated.append(state.session_id)


def test_agent_replies_do_not_reset_the_idle_clock():
    """Otherwise a session would never go stale: the bot always speaks last."""
    st = _session(minutes_since_caller=20)
    last = last_caller_activity(st)
    assert last is not None
    assert idle_seconds(st) > 19 * 60


def test_session_past_the_ceiling_is_closed():
    st = _session(minutes_since_caller=20)          # insurance ceiling is 15 min
    st.session_id = "stale"
    store = FakeStore({"stale": st})
    assert sweep(store, _spec) == ["stale"]
    assert st.phase == Phase.CLOSED
    assert classify(st).code == "ABANDONED_AFTER_SILENCE"


def test_session_inside_the_ceiling_is_left_alone():
    st = _session(minutes_since_caller=5)
    st.session_id = "live"
    store = FakeStore({"live": st})
    assert sweep(store, _spec) == []
    assert st.phase == Phase.PROCESS_CASE


def test_closed_window_is_a_different_disposition_from_silence():
    """Same outcome class, different product problem: someone who closed the
    tab decided to leave; someone who went quiet may just be busy."""
    st = _session(minutes_since_caller=20)
    st.session_id = "gone"
    st.facts.window_closed = True
    sweep(FakeStore({"gone": st}), _spec)
    d = classify(st)
    assert d.code == "ABANDONED_WINDOW_CLOSED"
    assert d.outcome_class == "abandoned"
    assert d.review_flag is True


def test_window_close_alone_does_not_close_the_session():
    """pagehide also fires on a refresh. The evidence only becomes a verdict
    once the session actually stays silent past its ceiling."""
    st = _session(minutes_since_caller=2)
    st.session_id = "refreshed"
    st.facts.window_closed = True
    assert sweep(FakeStore({"refreshed": st}), _spec) == []
    assert st.phase == Phase.PROCESS_CASE


def test_already_terminal_sessions_are_not_touched():
    st = _session(minutes_since_caller=60, phase=Phase.HUMAN_HANDOFF)
    st.session_id = "handed_off"
    st.facts.escalation_reason = "caller_requested_human"
    store = FakeStore({"handed_off": st})
    assert sweep(store, _spec) == []
    assert classify(st).code == "TRANSFERRED_CALLER_REQUEST"


def test_never_spoken_sessions_are_not_swept():
    """Opening the page creates a session. That is not a call, and closing it
    as an abandonment would invent conversations that never happened."""
    st = SessionState(session_id="empty", sop_name="insurance_claims")
    st.created_at = _at(60)
    assert sweep(FakeStore({"empty": st}), _spec) == []


def test_ceiling_is_per_sop():
    """The bank SOP's ceiling is shorter — a verified banking session left
    open is a security exposure a claims chat is not."""
    st = _session(minutes_since_caller=10)
    st.session_id = "bank"
    st.sop_name = "bank_kyc"                         # 7 min ceiling
    assert sweep(FakeStore({"bank": st}), _spec) == ["bank"]

    st2 = _session(minutes_since_caller=10)
    st2.session_id = "claims"                        # 15 min ceiling
    assert sweep(FakeStore({"claims": st2}), _spec) == []


def test_unknown_sop_does_not_break_the_sweep():
    bad = _session(minutes_since_caller=60)
    bad.session_id = "bad"
    bad.sop_name = "no_such_sop"
    good = _session(minutes_since_caller=60)
    good.session_id = "good"
    store = FakeStore({"bad": bad, "good": good})
    assert sweep(store, _spec) == ["good"]
    assert bad.phase == Phase.PROCESS_CASE
