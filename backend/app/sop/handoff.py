"""The handoff packet (DESIGN.md §7.6): "no transfer is ever empty-handed."

Every transition to HUMAN_HANDOFF carries this alongside the closing reply,
so a human agent (or, in this demo, the Inspector panel) can see exactly
what the caller does not have to repeat: verified identity and how, the
case in question, their actual question, what's already been tried, their
emotional state and why, and a recommended next step. This is what turns a
transfer from a failure into a high-quality handover (DESIGN.md §7.6 — the
buyer's economics run on average handle time, and a warm handoff is what
moves that number).
"""
from __future__ import annotations

from dataclasses import dataclass

from app.sop.disposition import classify
from app.sop.domain import DomainContext
from app.sop.types import SessionState


@dataclass
class HandoffPacket:
    reason: str
    verified: bool
    verification_method: list[str]
    caller_role: str
    case_id: str | None
    case_summary: str | None
    resolved_intent: str | None
    intent_evidence_quote: str
    caller_last_message: str
    attempted_paths: list[str]
    emotional_state: str
    off_topic_strikes: int
    consent_status: str
    recommended_next_step: str
    disposition: dict

    def as_dict(self) -> dict:
        return {
            "reason": self.reason,
            "verified": self.verified,
            "verification_method": self.verification_method,
            "caller_role": self.caller_role,
            "case_id": self.case_id,
            "case_summary": self.case_summary,
            "resolved_intent": self.resolved_intent,
            "intent_evidence_quote": self.intent_evidence_quote,
            "caller_last_message": self.caller_last_message,
            "attempted_paths": self.attempted_paths,
            "emotional_state": self.emotional_state,
            "off_topic_strikes": self.off_topic_strikes,
            "consent_status": self.consent_status,
            "recommended_next_step": self.recommended_next_step,
            # The receiving human needs the routing hint before they need the
            # narrative: which queue this belongs in decides whether they are
            # the right person to be reading the rest of this at all.
            "disposition": self.disposition,
        }


def build_handoff_packet(state: SessionState, domain: DomainContext) -> HandoffPacket:
    facts = state.facts
    memory = state.memory

    active_id = memory.active_case_id or memory.confirmed_case_id or memory.candidate_case_id
    view = domain.view(active_id) if active_id else None

    attempted = []
    if facts.matched_factor_count > 0:
        attempted.append(f"identity: {facts.matched_factor_count}/3 factors matched before handoff")
    if memory.case_hints:
        attempted.append("case identified from caller-stated hints" if memory.confirmed_case_id else "case not yet disambiguated")
    if memory.followup_notes:
        attempted.append(f"{len(memory.followup_notes)} follow-up note(s) already on file")
    if facts.consent_poll_count > 0:
        attempted.append(f"consent requested {facts.consent_poll_count}x, status={facts.consent_status.value}")

    # This turn's message (transition() stashes it — see SessionState.current_raw_message —
    # because it isn't appended to `transcript` until after resolve() runs).
    last_caller_msg = state.current_raw_message
    if not last_caller_msg:
        for t in reversed(state.transcript):
            if t.role == "caller":
                last_caller_msg = t.text
                break

    # Peak, not last-turn: a caller who was furious two turns ago and has
    # since gone quiet is still a caller a human agent should be told was
    # furious (DESIGN.md §7.6 / see SessionFacts.peak_intensity).
    emotional_state = "calm"
    if facts.peak_intensity >= 3:
        emotional_state = "escalated / abusive language"
    elif facts.peak_intensity == 2:
        emotional_state = "explicit dissatisfaction"
    elif facts.peak_intensity == 1:
        emotional_state = "mild frustration / impatience"

    # Guidance comes from the end-reason registry (types.END_REASONS), not
    # from a table maintained here. This was the third hand-written map keyed
    # on the same reason space; a reason added elsewhere silently got generic
    # advice, which is the worst kind of wrong for a human picking up a call.
    ended = facts.ended
    reason = ended.reason if ended else "unspecified"
    next_step = ended.spec.next_step if ended else None
    return HandoffPacket(
        reason=reason,
        verified=facts.is_verified(),
        verification_method=list(facts.matched_factor_types),
        caller_role=facts.caller_role.value,
        case_id=view.case_id if view else None,
        case_summary=view.record.summary if view else None,
        resolved_intent=memory.resolved_intent,
        intent_evidence_quote=memory.intent_evidence_quote,
        caller_last_message=last_caller_msg,
        attempted_paths=attempted,
        emotional_state=emotional_state,
        off_topic_strikes=facts.off_topic_strikes,
        consent_status=facts.consent_status.value,
        recommended_next_step=next_step
        or "Review the transcript and confirm the caller's need before proceeding.",
        disposition=classify(state).as_dict(),
    )
