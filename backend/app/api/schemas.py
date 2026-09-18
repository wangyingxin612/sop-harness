"""JSON-safe serialization for the Inspector and audit endpoints. Kept
separate from the dataclasses themselves so app/sop/* has zero web
dependency (DESIGN.md §5.1: the engine doesn't know an API exists)."""
from __future__ import annotations

from app.sop.types import SessionState, Slot


def _slot_to_dict(slot: Slot) -> dict:
    cur = slot.current()
    history = []
    s = slot
    while True:
        history.append({"value": s.value, "verbatim_quote": s.verbatim_quote, "turn_index": s.turn_index})
        if s.superseded_by is None:
            break
        s = s.superseded_by
    return {
        "value": cur.value,
        "verbatim_quote": cur.verbatim_quote,
        "turn_index": cur.turn_index,
        "confidence": cur.confidence,
        "source": cur.source,
        "superseded": len(history) > 1,
        "history": history,
    }


def serialize_state(state: SessionState) -> dict:
    facts = state.facts
    memory = state.memory
    return {
        "session_id": state.session_id,
        "sop_name": state.sop_name,
        "phase": state.phase.value,
        "consent_scenario": state.consent_scenario,
        "facts": {
            "verification_status": facts.verification_status.value,
            "matched_factor_count": facts.matched_factor_count,
            "matched_factor_types": facts.matched_factor_types,
            "mismatch_count": facts.mismatch_count,
            "caller_role": facts.caller_role.value,
            "verified_party_id": facts.verified_party_id,
            "representative_of_party_id": facts.representative_of_party_id,
            "consent_status": facts.consent_status.value,
            "consent_poll_count": facts.consent_poll_count,
            "off_topic_strikes": facts.off_topic_strikes,
            "injection_flags": facts.injection_flags,
            "turns_used": facts.turns_used,
            "tokens_used": facts.tokens_used,
            "cost_usd": round(facts.cost_usd, 5),
            "last_intensity": facts.last_intensity,
            "peak_intensity": facts.peak_intensity,
            "last_scope": facts.last_scope.value,
            "refusal_count": facts.refusal_count,
            "pending_action": facts.pending_action.action_type if facts.pending_action else None,
            "email_sent": facts.email_sent,
            "email_skipped": facts.email_skipped,
            "escalation_reason": facts.escalation_reason,
        },
        "memory": {
            "identity_slots": {k: _slot_to_dict(v) for k, v in memory.identity_slots.items()},
            "case_hints": [
                {"case_type": h.case_type, "status": h.status, "time_ref": h.time_ref, "turn_index": h.turn_index}
                for h in memory.case_hints
            ],
            "candidate_case_id": memory.candidate_case_id,
            "confirmed_case_id": memory.confirmed_case_id,
            "active_case_id": memory.active_case_id,
            "resolved_intent": memory.resolved_intent,
            "intent_confidence": memory.intent_confidence,
            "intent_evidence_quote": memory.intent_evidence_quote,
            "consent_events": memory.consent_events,
            "followup_notes": memory.followup_notes,
            "anomalies": memory.anomalies,
            "contact_change_requests": memory.contact_change_requests,
        },
        "transcript": [
            {
                "turn_index": t.turn_index,
                "role": t.role,
                "text": t.text,
                "ts": t.ts,
                "trace_event": t.trace_event,
            }
            for t in state.transcript
        ],
    }
