"""Session (de)serialization (DESIGN.md §9.3, revised).

The original design chose an in-memory session store, reasoning that nothing
here needs relational queries. That reasoning was right about the database
and wrong about durability: deployed to Fly.io with two machines behind a
load balancer, a session created on one machine 404'd the moment a request
was routed to the other — and `auto_stop_machines` meant even a single
machine lost everything when it idled out. The observable symptom was a
conversation that simply died mid-sentence with no way back.

The fix keeps the interface (no database) but makes the store durable:
every mutation is written to a JSON file, and a lookup miss rehydrates from
disk. Cheap, dependency-free, and it converts "your conversation is gone"
into "your conversation is still here".

Explicit encoders/decoders rather than a generic dataclass round-trip,
because Slot carries a recursive `superseded_by` chain and several fields
are enums whose *values* are the wire format.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from app.sop.types import (
    CallerRole,
    CaseHint,
    ConsentStatus,
    Memory,
    PendingAction,
    Phase,
    ScopeRing,
    SessionFacts,
    SessionState,
    Slot,
    Turn,
    VerificationStatus,
)


def _slot_from_dict(d: dict | None) -> Slot | None:
    if d is None:
        return None
    return Slot(
        value=d["value"],
        verbatim_quote=d["verbatim_quote"],
        turn_index=d["turn_index"],
        confidence=d.get("confidence", 1.0),
        source=d.get("source", "extracted"),
        superseded_by=_slot_from_dict(d.get("superseded_by")),
        recorded_at=d.get("recorded_at", ""),
    )


def session_to_dict(state: SessionState) -> dict:
    return asdict(state)


def session_from_dict(d: dict) -> SessionState:
    facts_d = d["facts"]
    pending = facts_d.get("pending_action")
    facts = SessionFacts(
        **{
            **{k: v for k, v in facts_d.items()
               if k not in ("verification_status", "caller_role", "consent_status",
                            "last_scope", "pending_action")},
            "verification_status": VerificationStatus(facts_d["verification_status"]),
            "caller_role": CallerRole(facts_d["caller_role"]),
            "consent_status": ConsentStatus(facts_d["consent_status"]),
            "last_scope": ScopeRing(facts_d["last_scope"]),
            "pending_action": PendingAction(**pending) if pending else None,
        }
    )

    mem_d = d["memory"]
    memory = Memory(
        identity_slots={k: _slot_from_dict(v) for k, v in mem_d["identity_slots"].items()},
        case_hints=[CaseHint(**h) for h in mem_d["case_hints"]],
        candidate_case_id=mem_d.get("candidate_case_id"),
        confirmed_case_id=mem_d.get("confirmed_case_id"),
        active_case_id=mem_d.get("active_case_id"),
        resolved_intent=mem_d.get("resolved_intent"),
        intent_evidence_quote=mem_d.get("intent_evidence_quote", ""),
        intent_confidence=mem_d.get("intent_confidence", 0.0),
        contact_change_requests=mem_d.get("contact_change_requests", []),
        anomalies=mem_d.get("anomalies", []),
        consent_events=mem_d.get("consent_events", []),
        followup_notes=mem_d.get("followup_notes", []),
    )

    return SessionState(
        session_id=d["session_id"],
        sop_name=d["sop_name"],
        phase=Phase(d["phase"]),
        memory=memory,
        facts=facts,
        transcript=[Turn(**t) for t in d["transcript"]],
        created_at=d.get("created_at", ""),
        consent_scenario=d.get("consent_scenario", "default"),
        current_raw_message=d.get("current_raw_message", ""),
    )


def write_session(path: Path, state: SessionState) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(session_to_dict(state), default=str))
    tmp.replace(path)  # atomic: a crash mid-write can't leave a truncated session


def read_session(path: Path) -> SessionState | None:
    if not path.exists():
        return None
    try:
        return session_from_dict(json.loads(path.read_text()))
    except Exception:  # noqa: BLE001 — a corrupt file must not take the server down
        return None
