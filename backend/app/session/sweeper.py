"""Server-side abandonment sweep.

THE GAP THIS CLOSES. The idle ladder (frontend/src/useIdleLadder.js) runs in
the browser, which is the right place for it — only the browser knows whether
the tab is visible or whether someone is mid-sentence. But it means that when
the caller simply CLOSES THE WINDOW, the thing that was going to close the
session goes with it. The session then sits in whatever phase it reached,
forever, showing on the operations board as in-flight. Every abandoned
conversation would quietly inflate the "live" column and never appear in the
containment denominator.

So the browser owns the polite part — checking in, warning, closing while
someone is watching — and the server owns the backstop: a session with no
caller activity past its SOP's hard ceiling is closed regardless of whether
anyone is still connected.

WHY LAZY AND NOT A SCHEDULER. The deployment scales to zero when idle
(fly.toml), so a background timer would be asleep exactly when sessions are
going stale. Sweeping on read means the books are balanced the moment anyone
looks at them, which is the only moment the answer is needed, and it costs
nothing when nobody is looking.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.sop.types import Phase, SessionState

TERMINAL = (Phase.CLOSED, Phase.HUMAN_HANDOFF, Phase.ABUSE_TERMINATED)


def _parse(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def last_caller_activity(state: SessionState) -> datetime | None:
    """When the CALLER last did something — not when the agent last spoke.

    The distinction matters for both the sweep and the duration metric: the
    agent's reply is our latency, not the caller's engagement, and a session
    whose clock restarted every time the bot said something would never go
    stale at all.
    """
    for turn in reversed(state.transcript):
        if turn.role == "caller":
            return _parse(turn.ts)
    return _parse(state.created_at)


def idle_seconds(state: SessionState, now: datetime | None = None) -> float | None:
    last = last_caller_activity(state)
    if last is None:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - last).total_seconds()


def sweep(store, get_spec, now: datetime | None = None) -> list[str]:
    """Close every session idle past its SOP's ceiling. Returns the ids closed.

    Deterministic and model-free, like every other close: deciding that a
    conversation is over is control-plane work (DESIGN.md §4.1).
    """
    closed = []
    now = now or datetime.now(timezone.utc)
    for sid in store.list_sessions():
        state = store.get(sid)
        if state is None or state.phase in TERMINAL or not state.transcript:
            continue
        try:
            ceiling = get_spec(state.sop_name).max_session_idle_seconds
        except Exception:
            continue          # an unknown SOP is not a reason to fail the sweep
        idle = idle_seconds(state, now)
        if idle is None or idle < ceiling:
            continue
        state.phase = Phase.CLOSED
        # The browser may have told us the window went away before the
        # silence started. That evidence is recorded on the session, and it
        # is what separates "walked away mid-conversation" from "closed the
        # tab and left" — two different product problems.
        state.facts.escalation_reason = (
            "caller_window_closed" if state.facts.window_closed else "caller_inactive"
        )
        store.update(state)
        closed.append(sid)
    return closed
