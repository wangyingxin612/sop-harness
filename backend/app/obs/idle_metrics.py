"""Idle-policy instrumentation.

The honest position on "how long should the bot wait?" is that nobody can
answer it from an armchair — not me, and not a best-practices blog post.
Every published number I could find is either about voice (where a two-second
silence is already awkward) or is someone's product intuition with a
confident tone. The only defensible move is to ship a number that is
*reasoned* rather than guessed, and then instrument it so the next number is
*measured*.

Two rates make the current setting falsifiable, and they pull in opposite
directions — which is the point, because a single metric would just push the
timer to one extreme:

  nudge_false_positive_rate
      of the nudges we sent, how many were answered almost immediately
      afterwards? A caller who replies within FALSE_POSITIVE_WINDOW of being
      asked "still there?" was there the whole time — they were typing, or
      thinking, and we interrupted. High rate => we are impatient.

  abandoned_without_close_rate
      of sessions that went quiet, how many never reached a terminal phase?
      These are the conversations that end with no conclusion in the audit
      trail, which is the failure the ladder exists to prevent. High rate
      => we are too patient, or the ladder is not firing at all.

Tuning means moving RESPONSE_EFFORT_SECONDS until both are acceptable, not
until the demo feels nice.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

EVENTS_PATH = Path(__file__).resolve().parents[2] / "runs" / "client_events.jsonl"

# A reply arriving within this long of a nudge means the caller was already
# engaged. Chosen to be comfortably longer than "read the nudge and type a
# short answer" but shorter than "came back from another task" — a person who
# had genuinely left does not compose a reply in eight seconds.
FALSE_POSITIVE_WINDOW_MS = 8_000

KNOWN_EVENTS = {
    "idle_nudge_shown",
    "idle_nudge_recovered",
    "idle_expired",
    "idle_ceiling_reached",
    "session_window_closed",
}

_LOCK = Lock()


def record_event(session_id: str, name: str, payload: dict | None = None) -> None:
    if name not in KNOWN_EVENTS:
        return
    row = {
        "ts": time.time(),
        "session_id": session_id,
        "event": name,
        "payload": payload or {},
    }
    with _LOCK:
        EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with EVENTS_PATH.open("a") as f:
            f.write(json.dumps(row) + "\n")


def _read_events() -> list[dict]:
    if not EVENTS_PATH.exists():
        return []
    rows = []
    for line in EVENTS_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue          # a torn last line from a crash is not a reason to 500
    return rows


@dataclass
class IdleReport:
    nudges_shown: int = 0
    nudges_recovered: int = 0
    nudges_false_positive: int = 0
    expirations: int = 0
    ceiling_hits: int = 0
    sessions_seen: int = 0
    sessions_quiet: int = 0
    sessions_abandoned_without_close: int = 0
    per_effort: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "nudges_shown": self.nudges_shown,
            "nudges_recovered": self.nudges_recovered,
            "nudges_false_positive": self.nudges_false_positive,
            "nudge_false_positive_rate": _rate(self.nudges_false_positive, self.nudges_shown),
            "nudge_recovery_rate": _rate(self.nudges_recovered, self.nudges_shown),
            "expirations": self.expirations,
            "ceiling_hits": self.ceiling_hits,
            "sessions_seen": self.sessions_seen,
            "sessions_quiet": self.sessions_quiet,
            "sessions_abandoned_without_close": self.sessions_abandoned_without_close,
            "abandoned_without_close_rate": _rate(
                self.sessions_abandoned_without_close, self.sessions_quiet
            ),
            "false_positive_window_ms": FALSE_POSITIVE_WINDOW_MS,
            # Broken out by effort level because the aggregate hides the
            # thing worth knowing: if `quick` phases are the only ones with a
            # high false-positive rate, the fix is to raise one number, not
            # to make the whole product more patient.
            "per_effort": self.per_effort,
            "interpretation": {
                "nudge_false_positive_rate": (
                    "callers who answered within "
                    f"{FALSE_POSITIVE_WINDOW_MS // 1000}s of being nudged — they were "
                    "there all along. Rising => the budget is too short."
                ),
                "abandoned_without_close_rate": (
                    "quiet sessions that never reached a terminal phase — no "
                    "conclusion in the audit trail. Rising => the ladder is too "
                    "slow or not firing."
                ),
            },
        }


def _rate(num: int, den: int) -> float | None:
    # None, not 0.0: "we have no data" and "the rate is zero" are different
    # claims, and a dashboard that renders the first as the second is how a
    # team concludes a setting is fine when it has never been exercised.
    if den == 0:
        return None
    return round(num / den, 3)


def build_report(terminal_by_session: dict[str, bool] | None = None) -> dict:
    """`terminal_by_session` maps session_id -> did it reach a terminal phase,
    supplied by the API from the session store (this module deliberately does
    not import the store: metrics read state, they never own it)."""
    events = _read_events()
    terminal_by_session = terminal_by_session or {}

    rep = IdleReport()
    sessions_with_quiet: set[str] = set()
    all_sessions: set[str] = set(terminal_by_session)
    # A recovery has to be charged to the effort level of the NUDGE it is
    # answering, not to whatever phase the session is in by the time it
    # arrives. Replying to "still there?" can itself advance the phase, so
    # reading the effort off the recovery event would systematically credit
    # the wrong bucket — and the per-effort breakdown exists precisely to
    # tell us which bucket is mistuned.
    nudge_effort: dict[str, str] = {}

    for e in events:
        sid = e.get("session_id", "")
        all_sessions.add(sid)
        name, payload = e["event"], e.get("payload", {})
        if name == "idle_nudge_recovered":
            effort = nudge_effort.get(sid, "unknown")
        else:
            effort = payload.get("response_effort") or "unknown"
        bucket = rep.per_effort.setdefault(
            effort, {"shown": 0, "false_positive": 0, "expired": 0}
        )

        if name == "idle_nudge_shown":
            nudge_effort[sid] = effort
            rep.nudges_shown += 1
            bucket["shown"] += 1
            sessions_with_quiet.add(sid)
        elif name == "idle_nudge_recovered":
            rep.nudges_recovered += 1
            if payload.get("ms_after_nudge", 10**9) <= FALSE_POSITIVE_WINDOW_MS:
                rep.nudges_false_positive += 1
                bucket["false_positive"] += 1
        elif name == "idle_expired":
            rep.expirations += 1
            bucket["expired"] += 1
            sessions_with_quiet.add(sid)
        elif name == "idle_ceiling_reached":
            rep.ceiling_hits += 1
            sessions_with_quiet.add(sid)

    rep.sessions_seen = len(all_sessions)
    rep.sessions_quiet = len(sessions_with_quiet)
    rep.sessions_abandoned_without_close = sum(
        1 for sid in sessions_with_quiet if not terminal_by_session.get(sid, False)
    )
    for effort, b in rep.per_effort.items():
        b["false_positive_rate"] = _rate(b["false_positive"], b["shown"])
    return rep.as_dict()
