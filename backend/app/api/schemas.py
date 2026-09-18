"""JSON-safe serialization for the Inspector and audit endpoints. Kept
separate from the dataclasses themselves so app/sop/* has zero web
dependency (DESIGN.md §5.1: the engine doesn't know an API exists)."""
from __future__ import annotations

from app.sop.disposition import classify
from app.sop.domain import DomainContext
from app.sop.spec import SopSpec
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


def _idle_policy(state: SessionState, spec: SopSpec | None) -> dict | None:
    """The silence budget, published so the CLIENT can run the timer.

    The server could run it instead, but the two facts that matter most —
    is this tab even in front of the person, and are they mid-sentence —
    only exist in the browser. So the server owns the POLICY (how long a
    given question is worth waiting for) and the client owns the CLOCK.
    """
    if spec is None:
        return None
    phase_spec = spec.phase_spec(state.phase)
    if phase_spec is None:
        return None
    return {
        "response_effort": phase_spec.response_effort,
        "base_seconds": phase_spec.idle_base_seconds,
        "max_session_idle_seconds": spec.max_session_idle_seconds,
    }


def _consent_policy(state: SessionState, domain: DomainContext | None) -> dict | None:
    """How the pending authorisation will resolve, published so the client can
    show a real wait instead of an indefinite one.

    `checks_before_timeout` is the honest framing of the fixture's status
    sequence: a caller waiting on someone else's approval deserves to know
    there is an end to the waiting, and the agent has to be able to say what
    happens when it arrives.
    """
    if domain is None:
        return None
    scenario = domain.consent_scenarios.get(
        state.consent_scenario, domain.consent_scenarios.get("default", {})
    )
    sequence = scenario.get("status_sequence", [])
    if not sequence:
        return None
    return {
        "poll_interval_seconds": scenario.get("poll_interval_seconds", 8),
        "checks_before_timeout": len(sequence),
        "checks_done": state.facts.consent_poll_count,
    }


def _identity_policy(spec: SopSpec | None) -> dict | None:
    """The gate's thresholds, published rather than hardcoded in the UI.

    The Inspector was printing "Two locks the session" as a literal, which
    quietly made a spec value part of the frontend. A different SOP with a
    different `max_mismatches` would have had the interface confidently
    stating the wrong number — the same class of mistake as any other place
    where configuration leaks into code.
    """
    if spec is None:
        return None
    return {
        "min_distinct_factors": spec.min_distinct_factors,
        "max_mismatches": spec.max_mismatches,
        "factor_types": list(spec.identity_factor_types),
    }


def serialize_state(
    state: SessionState,
    spec: SopSpec | None = None,
    domain: DomainContext | None = None,
) -> dict:
    facts = state.facts
    memory = state.memory
    return {
        "idle_policy": _idle_policy(state, spec),
        "disposition": classify(state).as_dict(),
        "identity_policy": _identity_policy(spec),
        "consent_policy": _consent_policy(state, domain),
        "session_id": state.session_id,
        "sop_name": state.sop_name,
        "phase": state.phase.value,
        "consent_scenario": state.consent_scenario,
        "facts": {
            "verification_status": facts.verification_status.value,
            "matched_factor_count": facts.matched_factor_count,
            "matched_factor_types": facts.matched_factor_types,
            "mismatch_count": facts.mismatch_count,
            "phonetic_match_used": facts.phonetic_match_used,
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
                {
                    "case_type": h.case_type,
                    "status": h.status,
                    "time_ref": h.time_ref,
                    "verbatim_quote": h.verbatim_quote,
                }
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
