"""Tool schemas exposed to ACT, and the dispatch table for executing them.

Only genuinely side-effecting tools are here (DESIGN.md §6's pseudo-tools —
get_case_detail etc. — were simplified out of the runtime tool-use loop for
this build; see PROGRESS.md and sops/insurance_claims.yaml's comment).
Appendix A: side-effecting tools execute synchronously, inside VERIFY, after
the reply text has passed the guard and before the turn is emitted — except
`request_consent`, which is deliberately asynchronous.
"""
from __future__ import annotations

TOOL_SCHEMAS: dict[str, dict] = {
    "transfer_to_human": {
        "name": "transfer_to_human",
        "description": (
            "Connect the caller to a human representative. Call this when the caller explicitly asks "
            "for a human, or when you cannot resolve their need within policy."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Brief reason for the transfer, for the human agent."}
            },
            "required": ["reason"],
        },
    },
    "request_consent": {
        "name": "request_consent",
        "description": (
            "Request the policyholder's consent for a representative to discuss their claim in full "
            "detail. Starts an asynchronous approval check."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    "create_followup": {
        "name": "create_followup",
        "description": (
            "Attach a note to the claim file for a human reviewer — use this when the caller has a "
            "question or a piece of information that needs manual follow-up but doesn't require an "
            "immediate transfer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"note": {"type": "string", "description": "What should be followed up on."}},
            "required": ["note"],
        },
    },
    "send_summary_email": {
        "name": "send_summary_email",
        "description": (
            "Send the conversation summary to the caller's email on file. Only call this after the "
            "caller has clearly agreed to receive it."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
}

# The one non-side-effecting tool: deferred perception riding on ACT's
# output (DESIGN.md §7.3/§7.11). Optional — the model calls it only when it
# has something to record.
RECORD_SIGNALS_SCHEMA = {
    "name": "record_signals",
    "description": (
        "Record information relevant to LATER steps of this call, or emotional/behavioral signals from "
        "THIS message. Call this alongside your reply whenever any of these fields apply; omit fields "
        "that don't. This is bookkeeping — it is never shown to the caller."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "case_type": {"type": "string", "description": "e.g. healthcare, auto, dental — only if mentioned."},
            "case_status_hint": {"type": "string", "description": "e.g. denied, open, closed — only if mentioned."},
            "case_time_ref": {"type": "string", "description": "A month name if the caller referenced one, e.g. 'January'."},
            "intent": {
                "type": "string",
                "enum": [
                    "document_submission", "next_steps", "denial_question", "general_claim_question",
                    "status_inquiry", "appeal_request", "payment_question", "escalation_request", "social",
                ],
            },
            "intent_confidence": {"type": "number"},
            "intent_evidence_quote": {"type": "string", "description": "The exact phrase that supports the intent."},
            "negative_affect": {"type": "boolean"},
            "refusal": {"type": "boolean", "description": "Caller is refusing to comply with a request (e.g. won't give ID info)."},
            "confusion": {"type": "boolean"},
            "intensity": {"type": "integer", "minimum": 0, "maximum": 3},
            "contact_change_requested": {"type": "boolean", "description": "Caller asked to change phone/email on file."},
            "contact_change_detail": {"type": "string"},
            "internal_note": {
                "type": "string",
                "description": "One short sentence on why you replied the way you did — for audit/debugging, never shown to the caller.",
            },
        },
    },
}


def tools_for_names(names: tuple[str, ...]) -> list[dict]:
    schemas = [TOOL_SCHEMAS[n] for n in names if n in TOOL_SCHEMAS]
    schemas.append(RECORD_SIGNALS_SCHEMA)
    return schemas
