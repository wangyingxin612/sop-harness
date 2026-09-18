"""Session storage (DESIGN.md §9.3, revised after a production failure —
see app/session/persistence.py for the full story).

Still no database: an in-memory dict backed by one JSON file per session,
plus the append-only JSONL audit trail (DESIGN.md §8.2). The dict is the
fast path; disk is what makes a restart, a redeploy, or a request landing
on a different machine survivable instead of fatal.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from app.session.persistence import read_session, write_session
from app.sop.types import SessionState

RUNS_DIR = Path(__file__).resolve().parents[2] / "runs"


class SessionStore:
    def __init__(self, runs_dir: Path = RUNS_DIR):
        self._sessions: dict[str, SessionState] = {}
        self._api_keys: dict[str, str] = {}   # per-session override (R10: UI-provided token)
        self._lock = threading.Lock()
        self.runs_dir = runs_dir
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def _state_path(self, session_id: str) -> Path:
        return self.runs_dir / f"{session_id}.state.json"

    def create(self, state: SessionState, api_key: str | None = None) -> None:
        with self._lock:
            self._sessions[state.session_id] = state
            if api_key:
                self._api_keys[state.session_id] = api_key
        write_session(self._state_path(state.session_id), state)

    def get(self, session_id: str) -> SessionState | None:
        with self._lock:
            state = self._sessions.get(session_id)
        if state is not None:
            return state
        # Miss: rehydrate from disk. This is the path that turns "your
        # conversation is gone" into "your conversation is still here" after
        # a restart or a request landing on a cold process.
        restored = read_session(self._state_path(session_id))
        if restored is not None:
            with self._lock:
                self._sessions[session_id] = restored
        return restored

    def update(self, state: SessionState) -> None:
        with self._lock:
            self._sessions[state.session_id] = state
        write_session(self._state_path(state.session_id), state)

    def api_key_for(self, session_id: str) -> str | None:
        return self._api_keys.get(session_id)

    def append_trace(self, session_id: str, trace_event: dict) -> None:
        path = self.runs_dir / f"{session_id}.jsonl"
        with path.open("a") as f:
            f.write(json.dumps(trace_event, default=str) + "\n")

    def list_sessions(self) -> list[str]:
        """Union of live and persisted sessions — used by the operations
        view, which must not show only whatever happens to be in memory."""
        with self._lock:
            live = set(self._sessions.keys())
        on_disk = {p.name[: -len(".state.json")] for p in self.runs_dir.glob("*.state.json")}
        return sorted(live | on_disk)


STORE = SessionStore()
