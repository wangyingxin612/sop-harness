"""Session storage (DESIGN.md §9.3 simplification: in-memory + append-only
JSONL behind an interface, not SQLite — nothing here needs relational
queries, and the interface keeps the door open at zero cost).

Also the audit trail (DESIGN.md §8.2): every trace_event is appended to a
per-session JSONL file as it happens, so a session's audit record exists
even if the process restarts mid-conversation.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from app.sop.types import SessionState

RUNS_DIR = Path(__file__).resolve().parents[2] / "runs"


class SessionStore:
    def __init__(self, runs_dir: Path = RUNS_DIR):
        self._sessions: dict[str, SessionState] = {}
        self._api_keys: dict[str, str] = {}   # per-session override (R10: UI-provided token)
        self._lock = threading.Lock()
        self.runs_dir = runs_dir
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def create(self, state: SessionState, api_key: str | None = None) -> None:
        with self._lock:
            self._sessions[state.session_id] = state
            if api_key:
                self._api_keys[state.session_id] = api_key

    def get(self, session_id: str) -> SessionState | None:
        with self._lock:
            return self._sessions.get(session_id)

    def update(self, state: SessionState) -> None:
        with self._lock:
            self._sessions[state.session_id] = state

    def api_key_for(self, session_id: str) -> str | None:
        return self._api_keys.get(session_id)

    def append_trace(self, session_id: str, trace_event: dict) -> None:
        path = self.runs_dir / f"{session_id}.jsonl"
        with path.open("a") as f:
            f.write(json.dumps(trace_event, default=str) + "\n")

    def list_sessions(self) -> list[str]:
        with self._lock:
            return list(self._sessions.keys())


STORE = SessionStore()
