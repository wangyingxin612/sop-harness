"""Static architectural claims — checked without running a conversation.

Attribution (evals/attribution.py) asks "did the mechanism fire during this
run?". That can only speak about code paths a scenario actually reached. A
whole class of defect lives underneath it: a mechanism that could never fire
at all, because two namespaces that must agree have drifted apart.

Every instance of that class found in this project has the same shape — TWO
HAND-MAINTAINED TABLES describing one concept, with a silent `.get()` between
them:

    policy.py declares required_elements as free text
    output_guard.py keys its matchers on the same free text
    check_contract() does `_REQUIRED_MATCHERS.get(element)` and skips on miss

    machine.py assigns escalation_reason as a string
    disposition.py maps that string to a code, defaulting on miss

    the client emits an event name
    idle_metrics.py filters against KNOWN_EVENTS, dropping on miss

In each case a typo, a rename, or a new value added on one side produces no
error, no warning, and no test failure — just an enforcement that quietly
stops happening. `SEND_NOW` never firing for an entire build is the same
disease at the directive level.

The cure is not more care. It is making absence impossible to express: a
closed vocabulary, and a loud failure when something falls outside it. These
claims are the check that the cure is still applied, and they run in
milliseconds with no model and no API key.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


@dataclass
class StaticClaim:
    claim_id: str
    description: str
    why_it_matters: str
    failures: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict:
        return {
            "claim_id": self.claim_id,
            "description": self.description,
            "why_it_matters": self.why_it_matters,
            "status": "ok" if self.ok else "drifted",
            "failures": self.failures,
        }


def _declared_contract_elements() -> set[str]:
    src = (BACKEND / "app/sop/policy.py").read_text()
    out: set[str] = set()
    for m in re.finditer(r"(required|forbidden)\s*=\s*\[(.*?)\]", src, re.S):
        out |= set(re.findall(r'"([^"]{4,})"', m.group(2)))
    out |= set(re.findall(r'(?:required|forbidden)\.append\("([^"]+)"\)', src))
    return out


def check_contract_vocabulary() -> StaticClaim:
    from app.guards.output_guard import CONTRACT_ELEMENTS

    c = StaticClaim(
        "contract_elements_are_a_closed_vocabulary",
        "Every required/forbidden element policy.py can emit is a known member of the "
        "guard's vocabulary, and each one either has a matcher or is explicitly delegated "
        "to another rule family.",
        "check_contract() used to skip unknown elements silently. 11 of 20 declared "
        "elements had no matcher and were never checked at all — including "
        "'an offer to request the policyholder's consent', a live R8 requirement.",
    )
    declared = _declared_contract_elements()
    unknown = sorted(declared - set(CONTRACT_ELEMENTS))
    for u in unknown:
        c.failures.append(f"policy.py emits {u!r}, which the guard has never heard of")
    return c


def check_end_reason_vocabulary() -> StaticClaim:
    from app.sop.disposition import unmapped_end_reasons
    from app.sop.types import END_REASONS

    c = StaticClaim(
        "end_reasons_have_exactly_one_home",
        "Every way a session can end is declared once, in types.END_REASONS, and every "
        "consumer resolves against it rather than keeping a private copy.",
        "Five overlapping representations of 'why did this end' (phase, escalation_reason, "
        "terminal_reason, route, disposition code) were kept in sync by hand. Needing a "
        "consistency test between internal representations is the smell.",
    )
    for r in sorted(unmapped_end_reasons()):
        c.failures.append(f"end reason {r!r} has no disposition")

    # Every termination goes through end_session(), which resolves against
    # the registry and raises on a miss. This scan is belt-and-braces: it
    # catches a literal that would only fail at runtime, on a branch a test
    # might not reach.
    call = re.compile(r"end_session\(\s*[\w.]+\s*,\s*\"([a-z_]+)\"")
    for path in (BACKEND / "app").rglob("*.py"):
        for lit in call.findall(path.read_text()):
            if lit not in END_REASONS:
                c.failures.append(f"{path.name} ends a session with {lit!r}, absent from END_REASONS")
    return c


def check_directives_are_backed() -> StaticClaim:
    """The claim that would have caught SEND_NOW on day one.

    A Directive is text in a system prompt. Emitting one guarantees nothing —
    which is exactly why a directive that stopped firing went unnoticed for
    the whole build while the model kept complying anyway. So every directive
    must declare which of the four enforcement mechanisms actually backs it,
    and 'nothing backs this' has to be a thing you can see.
    """
    from app.sop.policy import DIRECTIVE_BACKING, all_directive_ids

    c = StaticClaim(
        "every_directive_declares_its_backing",
        "Each directive states which mechanism enforces it: structural (the data or tool "
        "is absent), deterministic (code decides), guard (an output rule blocks it), or "
        "advisory (tone only — nothing enforces it, and that is admitted).",
        "SEND_NOW never fired once for an entire build. The email went out anyway because "
        "the model volunteered the tool call. A directive feels like enforcement and is "
        "not; making the backing explicit is what stops that feeling being load-bearing.",
    )
    for d in sorted(all_directive_ids()):
        if d not in DIRECTIVE_BACKING:
            c.failures.append(f"directive {d!r} declares no enforcement backing")
    for d in sorted(set(DIRECTIVE_BACKING) - all_directive_ids()):
        c.failures.append(f"DIRECTIVE_BACKING lists {d!r}, which no longer exists")
    return c


def check_client_event_vocabulary() -> StaticClaim:
    from app.obs.idle_metrics import KNOWN_EVENTS

    c = StaticClaim(
        "client_events_are_a_closed_vocabulary",
        "Every event name the frontend emits is known to the metrics sink.",
        "record_event() drops unknown names silently, so a renamed event becomes a metric "
        "that quietly reads zero forever — indistinguishable from 'this never happens'.",
    )
    emitted: set[str] = set()
    for path in (BACKEND.parent / "frontend/src").rglob("*.js*"):
        emitted |= set(re.findall(r'reportEvent\([^,]+,\s*"([a-z_]+)"', path.read_text()))
        emitted |= set(re.findall(r'onEventRef\.current\?\.\(\s*"([a-z_]+)"', path.read_text()))
        emitted |= set(re.findall(r'onEvent\w*\?\.\(\s*"([a-z_]+)"', path.read_text()))
    for e in sorted(emitted - KNOWN_EVENTS):
        c.failures.append(f"frontend emits {e!r}, which the metrics sink drops")
    return c


def check_phase_vocabulary_shared_with_ui() -> StaticClaim:
    from app.sop.types import Phase

    c = StaticClaim(
        "ui_phase_vocabulary_matches_the_engine",
        "Phase names hard-coded in the frontend correspond to real engine phases.",
        "The UI keys copy and layout off phase strings. A renamed phase leaves the "
        "interface silently rendering nothing for it.",
    )
    known = {p.value for p in Phase}
    for path in (BACKEND.parent / "frontend/src").rglob("*.js*"):
        text = path.read_text()
        for m in re.findall(r'"(VERIFY_ID|RESOLVE_INTENT|PROCESS_CASE|POST_PROCESS|[A-Z_]{5,})"', text):
            if m.isupper() and "_" in m and m not in known and m not in {
                "SELF_SERVED_SUMMARY_SENT", "SELF_SERVED_SUMMARY_DECLINED",
                "SELF_SERVED_NO_SUMMARY_DECISION", "CLOSED_BEFORE_CASE_WORK",
                "ABANDONED_AFTER_SILENCE", "ABANDONED_WINDOW_CLOSED",
                "TRANSFERRED_CALLER_REQUEST", "TRANSFERRED_IDENTITY_FAILED",
                "TRANSFERRED_CONSENT_UNAVAILABLE", "TRANSFERRED_AGENT_JUDGEMENT",
                "TRANSFERRED_OFF_TOPIC", "TRANSFERRED_INJECTION_ATTEMPTS",
                "TERMINATED_ABUSE", "IN_PROGRESS",
            }:
                c.failures.append(f"{path.name} references unknown phase-like name {m!r}")
    return c


ALL_CHECKS = (
    check_contract_vocabulary,
    check_end_reason_vocabulary,
    check_directives_are_backed,
    check_client_event_vocabulary,
    check_phase_vocabulary_shared_with_ui,
)


def analyse() -> dict:
    claims = []
    for fn in ALL_CHECKS:
        try:
            claims.append(fn().as_dict())
        except Exception as exc:  # noqa: BLE001
            claims.append({
                "claim_id": fn.__name__, "status": "error",
                "description": "", "why_it_matters": "",
                "failures": [f"{type(exc).__name__}: {exc}"],
            })
    drifted = [c for c in claims if c["status"] != "ok"]
    return {
        "claims": claims,
        "summary": {
            "claims_total": len(claims),
            "claims_ok": len(claims) - len(drifted),
            "drifted": [c["claim_id"] for c in drifted],
            "failures": sum(len(c["failures"]) for c in claims),
            "coherent": not drifted,
        },
    }


if __name__ == "__main__":
    import json
    print(json.dumps(analyse(), indent=2))
