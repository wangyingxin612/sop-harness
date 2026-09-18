"""Disposition codes — the one-line operational outcome of a session.

Every contact centre closes every contact with a disposition code. It is the
unit that operations reporting, QA sampling, workforce planning and the
invoice all key off, which is why it is the first thing a buyer's ops team
asks for and the first thing a demo that "just chats" cannot supply.

TWO DESIGN DECISIONS, both of which are the point of this file:

1. THE CODE IS DERIVED, NEVER ASSIGNED BY THE MODEL.
   The obvious implementation is to ask the model, at the end, "how would
   you categorise this call?" That produces a plausible label and an
   unusable statistic: the taxonomy drifts, the same session classifies two
   ways on two runs, and nobody can defend the containment number to a
   finance team. Disposition is a pure function of terminal state — the same
   control-plane/data-plane split as everywhere else (DESIGN.md §4.1). The
   model is not consulted and cannot be wrong about it.

2. THE CODE NEVER CLAIMS AN OUTCOME WE DID NOT OBSERVE.
   There is no `RESOLVED_SATISFIED`, because nothing in this system observes
   satisfaction. What we can observe is: did the caller reach the case-work
   phase, did the SOP run to its end, who ended it and why. Codes name those
   facts. A disposition taxonomy that quietly overclaims is worse than none,
   because it is the number a QA team stops checking.

The coarse `outcome_class` is what rolls up to CONTAINMENT — the metric
DESIGN.md §7.6 argues is the buyer's actual economics. Note that abandonment
is deliberately NOT counted as a failure to contain: a caller who walked away
did not defeat the automation, and folding the two together produces a
containment rate that moves for two unrelated reasons and therefore cannot be
acted on.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.sop.types import ConsentStatus, Phase, SessionState

CONTAINED = "contained"
TRANSFERRED = "transferred"
ABANDONED = "abandoned"
TERMINATED = "terminated"
IN_PROGRESS = "in_progress"


@dataclass(frozen=True)
class DispositionSpec:
    code: str
    outcome_class: str
    label: str            # what an ops dashboard shows
    routing_hint: str     # which human queue this belongs in, if any
    review_flag: bool     # should QA sample this one preferentially


# Ordered roughly best-to-worst so a dashboard can render them without
# re-sorting, and so the table reads as an outcome ladder.
DISPOSITIONS: dict[str, DispositionSpec] = {
    "SELF_SERVED_SUMMARY_SENT": DispositionSpec(
        "SELF_SERVED_SUMMARY_SENT", CONTAINED,
        "Handled end to end, summary emailed", "none", False),
    "SELF_SERVED_SUMMARY_DECLINED": DispositionSpec(
        "SELF_SERVED_SUMMARY_DECLINED", CONTAINED,
        "Handled end to end, summary declined", "none", False),
    "SELF_SERVED_NO_SUMMARY_DECISION": DispositionSpec(
        "SELF_SERVED_NO_SUMMARY_DECISION", CONTAINED,
        "Case worked, closed before the summary step", "none", True),
    "CLOSED_BEFORE_CASE_WORK": DispositionSpec(
        "CLOSED_BEFORE_CASE_WORK", CONTAINED,
        "Closed before any case was worked", "none", True),
    "ABANDONED_AFTER_SILENCE": DispositionSpec(
        "ABANDONED_AFTER_SILENCE", ABANDONED,
        "Caller went silent; closed by the idle ladder", "none", False),
    "TRANSFERRED_CALLER_REQUEST": DispositionSpec(
        "TRANSFERRED_CALLER_REQUEST", TRANSFERRED,
        "Caller asked for a person", "general", False),
    "TRANSFERRED_IDENTITY_FAILED": DispositionSpec(
        "TRANSFERRED_IDENTITY_FAILED", TRANSFERRED,
        "Identity could not be verified", "identity_desk", True),
    "TRANSFERRED_CONSENT_UNAVAILABLE": DispositionSpec(
        "TRANSFERRED_CONSENT_UNAVAILABLE", TRANSFERRED,
        "Third-party consent did not arrive in time", "authorisations", False),
    "TRANSFERRED_AGENT_JUDGEMENT": DispositionSpec(
        "TRANSFERRED_AGENT_JUDGEMENT", TRANSFERRED,
        "Automated agent judged a person was needed", "general", True),
    "TRANSFERRED_OFF_TOPIC": DispositionSpec(
        "TRANSFERRED_OFF_TOPIC", TRANSFERRED,
        "Persistent off-topic requests", "general", True),
    "TRANSFERRED_INJECTION_ATTEMPTS": DispositionSpec(
        "TRANSFERRED_INJECTION_ATTEMPTS", TRANSFERRED,
        "Repeated attempts to manipulate the agent", "trust_and_safety", True),
    "TERMINATED_ABUSE": DispositionSpec(
        "TERMINATED_ABUSE", TERMINATED,
        "Session ended after repeated abuse", "trust_and_safety", True),
    "IN_PROGRESS": DispositionSpec(
        "IN_PROGRESS", IN_PROGRESS,
        "Still in progress", "none", False),
}

# escalation_reason (set by machine.py / effects.py) -> disposition code.
# Kept as an explicit table rather than string munging so that adding an
# escalation reason without deciding its disposition is a visible omission
# (see `_UNMAPPED` below) instead of a silent fallthrough.
_REASON_TO_CODE = {
    "caller_requested_human": "TRANSFERRED_CALLER_REQUEST",
    "identity_verification_failed": "TRANSFERRED_IDENTITY_FAILED",
    "repeated_prompt_injection_attempts": "TRANSFERRED_INJECTION_ATTEMPTS",
    "repeated_off_topic_requests": "TRANSFERRED_OFF_TOPIC",
    "agent_initiated_transfer": "TRANSFERRED_AGENT_JUDGEMENT",
}

_UNMAPPED = "TRANSFERRED_AGENT_JUDGEMENT"


def phases_reached(state: SessionState) -> set[str]:
    """Which phases this session actually entered, read off the trace rather
    than inferred from the current phase — a session sitting in CLOSED tells
    you nothing about whether a case was ever worked."""
    reached = {Phase.VERIFY_ID.value}
    for turn in state.transcript:
        te = turn.trace_event
        if not te:
            continue
        for key in ("phase_before", "phase_after"):
            val = te.get(key)
            if val:
                reached.add(val)
    return reached


@dataclass(frozen=True)
class Disposition:
    code: str
    outcome_class: str
    label: str
    routing_hint: str
    review_flag: bool
    detail: str

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "outcome_class": self.outcome_class,
            "label": self.label,
            "routing_hint": self.routing_hint,
            "review_flag": self.review_flag,
            "detail": self.detail,
        }


def classify(state: SessionState) -> Disposition:
    facts = state.facts
    phase = state.phase

    if phase == Phase.ABUSE_TERMINATED:
        code = "TERMINATED_ABUSE"

    elif phase == Phase.HUMAN_HANDOFF:
        reason = facts.escalation_reason or ""
        code = _REASON_TO_CODE.get(reason, _UNMAPPED)
        # A transfer that happened while third-party consent was outstanding
        # belongs in the authorisations queue, not the general one, whatever
        # the proximate escalation reason was — the person who can unblock it
        # is not the person a general transfer reaches.
        if facts.consent_status == ConsentStatus.TIMED_OUT and code in (
            "TRANSFERRED_AGENT_JUDGEMENT",
            "TRANSFERRED_CALLER_REQUEST",
        ):
            code = "TRANSFERRED_CONSENT_UNAVAILABLE"

    elif phase == Phase.CLOSED:
        # Only silence is abandonment. A call closed because the caller said
        # they were done, or because an operator closed it, is an ordinary
        # ending and is classified by what actually happened in it.
        if facts.escalation_reason == "caller_inactive":
            code = "ABANDONED_AFTER_SILENCE"
        elif Phase.PROCESS_CASE.value not in phases_reached(state):
            code = "CLOSED_BEFORE_CASE_WORK"
        elif facts.email_sent:
            code = "SELF_SERVED_SUMMARY_SENT"
        elif facts.email_skipped:
            code = "SELF_SERVED_SUMMARY_DECLINED"
        else:
            code = "SELF_SERVED_NO_SUMMARY_DECISION"

    else:
        code = "IN_PROGRESS"

    spec = DISPOSITIONS[code]
    return Disposition(
        code=spec.code,
        outcome_class=spec.outcome_class,
        label=spec.label,
        routing_hint=spec.routing_hint,
        review_flag=spec.review_flag,
        detail=_detail(state, spec),
    )


def _detail(state: SessionState, spec: DispositionSpec) -> str:
    """One sentence a human reads before opening the transcript."""
    facts = state.facts
    bits = [f"{facts.turns_used} turn(s)", f"${facts.cost_usd:.4f}"]
    if facts.is_verified():
        bits.append(f"verified via {', '.join(facts.matched_factor_types) or 'n/a'}")
    else:
        bits.append(f"unverified ({facts.matched_factor_count}/3 factors)")
    if facts.peak_intensity >= 2:
        bits.append(f"peak caller intensity {facts.peak_intensity}/3")
    return f"{spec.label} — " + ", ".join(bits) + "."


def containment_rate(dispositions: list[str]) -> float | None:
    """contained / (contained + transferred).

    Abandonment and abuse termination are excluded from BOTH sides. A caller
    who walked away did not defeat the automation, and an abuse termination
    is a policy outcome we would not want to reduce; including either would
    make the rate move for reasons an operations team cannot act on.
    """
    counted = [
        DISPOSITIONS[c].outcome_class
        for c in dispositions
        if c in DISPOSITIONS and DISPOSITIONS[c].outcome_class in (CONTAINED, TRANSFERRED)
    ]
    if not counted:
        return None
    return round(counted.count(CONTAINED) / len(counted), 3)
