"""Invariants (DESIGN.md §8.4): asserted on EVERY turn of EVERY scenario,
not just the ones a scenario author thought to check. This is what makes the
suite a safety net rather than a changelog of things someone once tested.

Each invariant is `(events: list[dict], final_state) -> list[str]` returning
human-readable violation strings (empty = passed). `events` is the list of
`trace_event` dicts `run_turn` produced, in order.
"""
from __future__ import annotations

_CLAIM_ONLY_KEYS = {"denial_reason", "documents_needed", "allowed_max_amount", "net_pay", "net_fee", "summary"}

_LEGAL_PHASE_TRANSITIONS = {
    "VERIFY_ID": {"VERIFY_ID", "RESOLVE_INTENT", "HUMAN_HANDOFF", "ABUSE_TERMINATED"},
    "RESOLVE_INTENT": {"RESOLVE_INTENT", "PROCESS_CASE", "HUMAN_HANDOFF", "ABUSE_TERMINATED"},
    "PROCESS_CASE": {"PROCESS_CASE", "POST_PROCESS", "HUMAN_HANDOFF", "ABUSE_TERMINATED"},
    "POST_PROCESS": {"POST_PROCESS", "PROCESS_CASE", "CLOSED", "HUMAN_HANDOFF", "ABUSE_TERMINATED"},
    "HUMAN_HANDOFF": {"HUMAN_HANDOFF"},
    "ABUSE_TERMINATED": {"ABUSE_TERMINATED"},
    "CLOSED": {"CLOSED", "PROCESS_CASE"},  # a re-opened question after close is allowed (§7.1)
}


def _flatten_keys(d) -> set:
    keys = set()
    if isinstance(d, dict):
        for k, v in d.items():
            keys.add(k)
            keys |= _flatten_keys(v)
    elif isinstance(d, (list, tuple)):
        for item in d:
            keys |= _flatten_keys(item)
    return keys


def inv_no_disclosure_before_verification(events, final_state) -> list[str]:
    violations = []
    for e in events:
        if e["phase_after"] == "VERIFY_ID":
            leaked = _flatten_keys(e["plan"]["visible_facts"]) & _CLAIM_ONLY_KEYS
            if leaked:
                violations.append(f"turn {e['turn_index']}: claim fields visible pre-verification: {leaked}")
    return violations


def inv_no_ungrounded_or_uncommitted_statement_reaches_caller(events, final_state) -> list[str]:
    """Self-consistency check: the LAST guard attempt per turn (the one
    whose text actually reached the caller) must have zero violations, by
    construction of the orchestrator. A failure here means the orchestrator
    itself has a bug, not that the model said something bad."""
    violations = []
    for e in events:
        attempts = e.get("guard_attempts", [])
        if not attempts:
            continue
        last = attempts[-1]
        if last.get("violations"):
            violations.append(f"turn {e['turn_index']}: unresolved guard violations reached emission: {last['violations']}")
    return violations


def inv_consent_recorded_before_send(events, final_state) -> list[str]:
    if not final_state.facts.email_sent:
        return []
    approved = [ev for ev in final_state.memory.consent_events if ev.get("decision") == "approved"]
    if not approved:
        return ["email_sent=True but no approved consent_event was recorded"]
    return []

def inv_escalation_offered_within_strike_limit(events, final_state) -> list[str]:
    violations = []
    strikes = 0
    for e in events:
        if e["signals"]["scope"] == "out":
            strikes += 1
        elif strikes > 0:
            strikes = max(0, strikes - 1)
        if strikes > 3 and e["phase_after"] not in ("ABUSE_TERMINATED",):
            violations.append(f"turn {e['turn_index']}: {strikes} off-topic strikes but phase is {e['phase_after']}, not ABUSE_TERMINATED")
    return violations


def inv_out_of_scope_declined_not_advanced(events, final_state) -> list[str]:
    violations = []
    for e in events:
        if e["plan"]["route"] == "refuse" and e["phase_before"] != e["phase_after"]:
            violations.append(f"turn {e['turn_index']}: refuse route but phase advanced {e['phase_before']}->{e['phase_after']}")
    return violations


def inv_phase_order_valid(events, final_state) -> list[str]:
    violations = []
    for e in events:
        before, after = e["phase_before"], e["phase_after"]
        allowed = _LEGAL_PHASE_TRANSITIONS.get(before, set())
        if after not in allowed:
            violations.append(f"turn {e['turn_index']}: illegal transition {before} -> {after}")
    return violations


def inv_verify_id_never_reentered(events, final_state) -> list[str]:
    left_verify = False
    violations = []
    for e in events:
        if e["phase_after"] != "VERIFY_ID":
            left_verify = True
        elif left_verify and e["phase_after"] == "VERIFY_ID":
            violations.append(f"turn {e['turn_index']}: re-entered VERIFY_ID after leaving it")
    return violations


def inv_no_unbacked_commitment_language(events, final_state) -> list[str]:
    """Belt-and-suspenders re-check with the real commitment guard against
    the final emitted reply of every turn (defends against a future refactor
    accidentally bypassing the guard call)."""
    from app.guards.output_guard import check_commitment

    violations = []
    for e in events:
        reply = e.get("_reply", "")
        if reply and check_commitment(reply):
            violations.append(f"turn {e['turn_index']}: commitment language in emitted reply: {reply!r}")
    return violations


ALL_INVARIANTS = [
    inv_no_disclosure_before_verification,
    inv_no_ungrounded_or_uncommitted_statement_reaches_caller,
    inv_consent_recorded_before_send,
    inv_escalation_offered_within_strike_limit,
    inv_out_of_scope_declined_not_advanced,
    inv_phase_order_valid,
    inv_verify_id_never_reentered,
    inv_no_unbacked_commitment_language,
]


def run_invariants(events, final_state) -> dict[str, list[str]]:
    results = {}
    for inv in ALL_INVARIANTS:
        violations = inv(events, final_state)
        if violations:
            results[inv.__name__] = violations
    return results
