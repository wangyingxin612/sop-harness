"""Side-effect handlers for tool calls (DESIGN.md Appendix A: executed
synchronously, after the guard, before the turn is emitted — except
`request_consent`, which polls asynchronously by design).

Every handler mutates the `state` it is given directly. By the time these
run, `state` is the orchestrator's own working copy for this turn (already
produced by `machine.transition`), so mutating in place here is safe and
matches Appendix A's timing note.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.sop.domain import DomainContext
from app.sop.summary import build_summary_draft
from app.sop.types import ConsentStatus, Phase, SessionState


@dataclass
class ToolEffectResult:
    tool_name: str
    tool_use_id: str
    output: dict          # returned to the model as a tool_result, if the call continues
    summary_for_trace: str


def load_consent_scenarios(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())


def handle_transfer_to_human(state: SessionState, tool_input: dict, tool_use_id: str) -> ToolEffectResult:
    reason = tool_input.get("reason", "caller requested or agent-initiated transfer")
    if state.phase not in (Phase.HUMAN_HANDOFF,):
        state.phase = Phase.HUMAN_HANDOFF
        state.facts.escalation_reason = "agent_initiated_transfer"
    return ToolEffectResult(
        tool_name="transfer_to_human",
        tool_use_id=tool_use_id,
        output={"status": "transferring", "reason": reason},
        summary_for_trace=f"transfer_to_human({reason!r})",
    )


def handle_create_followup(state: SessionState, tool_input: dict, tool_use_id: str) -> ToolEffectResult:
    note = tool_input.get("note", "").strip()
    if note:
        state.memory.followup_notes.append(note)
    return ToolEffectResult(
        tool_name="create_followup",
        tool_use_id=tool_use_id,
        output={"status": "recorded"},
        summary_for_trace=f"create_followup({note!r})",
    )


def handle_request_consent(
    state: SessionState, tool_input: dict, tool_use_id: str, consent_scenarios: dict
) -> ToolEffectResult:
    """INITIATES an asynchronous consent request (DESIGN.md §7.10).

    Deliberately does not poll: once a request is open, the state machine
    advances it once per turn on its own (machine.poll_pending_consent),
    because a real async approval resolves on its own schedule rather than
    when an agent decides to look. The model's only decision here is
    *whether to ask*, which is a genuine judgement call; *how often to check*
    is not, and used to be a source of eval flakiness when it was.

    Re-calling this while a request is already open is a no-op on status —
    it just reports the current state, which is what a caller asking "has it
    come through?" should get.
    """
    facts = state.facts
    scenario = consent_scenarios.get(state.consent_scenario, consent_scenarios["default"])
    sequence = scenario["status_sequence"]

    if facts.consent_status in (ConsentStatus.NOT_REQUESTED, ConsentStatus.DECLINED):
        facts.consent_poll_count += 1
        idx = min(facts.consent_poll_count - 1, len(sequence) - 1)
        if sequence[idx] == "approved":
            facts.consent_status = ConsentStatus.APPROVED
        elif facts.consent_poll_count >= len(sequence):
            facts.consent_status = ConsentStatus.TIMED_OUT
        else:
            facts.consent_status = ConsentStatus.PENDING
        action = "opened"
    else:
        action = "already open"

    return ToolEffectResult(
        tool_name="request_consent",
        tool_use_id=tool_use_id,
        output={"status": facts.consent_status.value, "poll_count": facts.consent_poll_count},
        summary_for_trace=f"request_consent() [{action}] -> {facts.consent_status.value}",
    )


def handle_send_summary_email(
    state: SessionState, tool_input: dict, tool_use_id: str, domain: DomainContext
) -> ToolEffectResult:
    facts = state.facts
    memory = state.memory
    just_approved = bool(memory.consent_events) and memory.consent_events[-1].get("decision") == "approved"
    if not just_approved and not facts.email_sent:
        # Defensive: the directive layer only offers this path when consent
        # was just given, but a guard/handler-level check costs nothing and
        # means a malformed tool call can never send without consent.
        return ToolEffectResult(
            tool_name="send_summary_email",
            tool_use_id=tool_use_id,
            output={"status": "blocked", "reason": "no recorded consent for this turn"},
            summary_for_trace="send_summary_email() blocked: no consent event",
        )
    draft = build_summary_draft(state, domain)
    facts.email_sent = True
    return ToolEffectResult(
        tool_name="send_summary_email",
        tool_use_id=tool_use_id,
        output={"status": "sent", "to": draft.recipient_email, "summary": draft.as_dict()},
        summary_for_trace=f"send_summary_email() -> sent to {draft.recipient_email}",
    )


def execute_tool_call(
    name: str, tool_input: dict, tool_use_id: str, state: SessionState, domain: DomainContext, consent_scenarios: dict
) -> ToolEffectResult | None:
    if name == "transfer_to_human":
        return handle_transfer_to_human(state, tool_input, tool_use_id)
    if name == "create_followup":
        return handle_create_followup(state, tool_input, tool_use_id)
    if name == "request_consent":
        return handle_request_consent(state, tool_input, tool_use_id, consent_scenarios)
    if name == "send_summary_email":
        return handle_send_summary_email(state, tool_input, tool_use_id, domain)
    return None  # record_signals is handled separately (deferred perception, not a side effect)
