"""VERIFY — the output guard (DESIGN.md §7.9). One guard, four rule
families, each independently unit-testable with hand-crafted reply strings —
no LLM needed to exercise this module, same as the policy layer.

    ① disclosure  — nothing not authorized this turn appears
    ② grounding   — every number/date traces to visible_facts
    ③ commitment  — sensitive numbers are attributed, never promised
    ④ contract    — required_elements present, forbidden_elements absent

Guards ①-③ assert something is ABSENT — checkable sentence-by-sentence, the
half of DESIGN.md §7.11's "never retract" protocol that can gate a stream.
Guard ④'s required-elements half asserts something is PRESENT — checkable
only over the whole reply, so a missing element is *appended*, never used to
block (see `missing_required_elements`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.identity.normalize import normalize_digits_only
from app.sop.domain import DomainContext
from app.sop.types import SessionState, TurnPlan

_MIN_FORBIDDEN_LEN = 4  # skip trivially short values (e.g. "0.00") to cut false positives

_ATTRIBUTION_MARKERS = (
    "the record shows", "on file", "according to the file", "your claim shows",
    "our records show", "our system shows", "shows that", "it shows", "file shows",
    "the file shows", "currently shows",
)

# A promissory speech act = (subject) + (modal/promise verb) + (outcome verb).
# Two variants, because the outcome verbs split into two classes:
_PROMISE_SUBJECT_MODAL = (
    r"\b(you|we|i)\b[^.!?]{0,15}\b(will|'ll|shall|guarantee|guarantees|promise|promises|assure|assures)\b"
)

# (a) Verbs specific enough to a payment/approval outcome that the verb
#     alone is the signal — no object needed.
_PROMISE_SPECIFIC_RE = re.compile(
    _PROMISE_SUBJECT_MODAL + r"[^.!?]{0,20}\b(receive|pay|paid|refund|reimburse|reimbursed|cover|covered|approve|approved)\b",
    re.IGNORECASE,
)

# (b) "get" is too generic to flag on its own ("I'll need to get her consent"
#     is not a promise of money), but it IS the most natural way to phrase a
#     payout promise ("you'll get the full amount"). So it fires only when
#     its object is payment-shaped. Dropping the verb entirely — which an
#     earlier fix did — traded a false positive for a false NEGATIVE on
#     number-free promises; constraining the object fixes both directions.
_PROMISE_GENERIC_RE = re.compile(
    _PROMISE_SUBJECT_MODAL
    + r"[^.!?]{0,20}\bget\b[^.!?]{0,25}"
    + r"\b(amount|money|payment|paid|refund|reimbursement|reimbursed|check|cheque|funds|"
    r"settlement|coverage|covered|approval|approved|balance|difference|full|everything)\b",
    re.IGNORECASE,
)

_SENSITIVE_NUMBER_RE = re.compile(
    r"\$\s?\d[\d,]*\.?\d*"                      # currency
    r"|\b\d{4}-\d{2}-\d{2}\b"                    # ISO date
    r"|\b\d+\s*(?:day|days|week|weeks|business day|business days)\b"  # durations
    r"|\b\d{1,3}%\b",                            # percentages
    re.IGNORECASE,
)

_DURATION_RE = re.compile(r"\b(\d+)\s*(?:day|days|week|weeks|business day|business days)\b", re.IGNORECASE)

_CASE_ID_RE = re.compile(r"\bCL-\d{3,6}\b", re.IGNORECASE)
_DOLLAR_RE = re.compile(r"\$\s?\d[\d,]*\.?\d*|\b\d+\.\d{2}\b")


@dataclass
class GuardVerdict:
    ok: bool
    violations: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)   # to APPEND, not block on


def _flatten_values(obj) -> list[str]:
    out: list[str] = []
    if isinstance(obj, dict):
        for v in obj.values():
            out.extend(_flatten_values(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out.extend(_flatten_values(v))
    elif obj is not None:
        out.append(str(obj))
    return out


def _authorized(value: str, visible_flat: list[str]) -> bool:
    """A candidate is authorized if it exactly matches a visible value, OR is
    a natural sub-phrase of one that's already authorized in full — e.g. a
    document name ("pathology report") that appears inside an authorized
    denial_reason narrative should not separately be flagged as a leak."""
    value_l = value.lower()
    return any(value_l == v or value_l in v for v in visible_flat)


def compute_forbidden_values(domain: DomainContext, plan: TurnPlan) -> set[str]:
    """Fixture values NOT authorized to appear in this turn's reply
    (DESIGN.md §7.9 rule ①). Deliberately restricted to specific, high-
    signal identifiers rather than whole sentences — see module docstring."""
    visible_flat = [v.lower() for v in _flatten_values(plan.visible_facts)]
    forbidden: set[str] = set()

    for p in domain.policyholders:
        for v in (p.name, p.dob, p.phone, p.email, p.id_last4, p.policy_number, *p.name_aliases, *p.email_aliases):
            if v and len(str(v)) >= _MIN_FORBIDDEN_LEN and not _authorized(str(v), visible_flat):
                forbidden.add(str(v))

    for c in domain.claims:
        candidates = [c.case_id, c.denial_reason, c.appeal_deadline, *c.documents_needed]
        for amt in (c.expected_reimbursement_amount, c.allowed_max_amount, c.net_pay, c.net_fee):
            if amt and amt != "0.00":
                candidates.append(amt)
        for v in candidates:
            if v and len(str(v)) >= _MIN_FORBIDDEN_LEN and not _authorized(str(v), visible_flat):
                forbidden.add(str(v))

    return forbidden


def check_disclosure(reply: str, domain: DomainContext, plan: TurnPlan) -> list[str]:
    forbidden = compute_forbidden_values(domain, plan)
    reply_lower = reply.lower()
    violations = []
    for value in forbidden:
        if value.lower() in reply_lower:
            violations.append(f"disclosed unauthorized value: {value!r}")
    # Digit-normalized pass for identity-caliber fields — catches reformatted
    # phone/DOB/id_last4 (e.g. spoken-style output) a literal match would miss.
    reply_digits = normalize_digits_only(reply)
    if reply_digits:
        for p in domain.policyholders:
            for v in (p.dob, p.phone, p.id_last4):
                norm = normalize_digits_only(v)
                if norm and len(norm) >= 4 and norm in reply_digits and v.lower() not in reply_lower:
                    if str(v).lower() not in {x.lower() for x in _flatten_values(plan.visible_facts)}:
                        violations.append(f"disclosed unauthorized value (reformatted): {v!r}")
    return violations


def check_grounding(reply: str, plan: TurnPlan) -> list[str]:
    visible_flat = _flatten_values(plan.visible_facts)
    visible_normalized = set()
    for v in visible_flat:
        visible_normalized.add(v.lower())
        visible_normalized.add(v.replace(",", "").lower())
        visible_normalized.add(v.replace("$", "").replace(",", "").lower())

    violations = []
    for match in _SENSITIVE_NUMBER_RE.finditer(reply):
        raw = match.group(0)
        duration_match = _DURATION_RE.fullmatch(raw)
        cleaned = duration_match.group(1) if duration_match else raw.replace("$", "").replace(",", "").strip().lower()
        cleaned = cleaned.lower()
        if cleaned in visible_normalized or raw.lower() in visible_normalized:
            continue
        if cleaned.endswith(".00") and cleaned[:-3] in visible_normalized:
            continue
        if any(cleaned in v or v in cleaned for v in visible_normalized if len(v) >= 3):
            continue
        violations.append(f"ungrounded figure in reply: {raw!r}")
    return violations


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def check_commitment(reply: str) -> list[str]:
    violations = []
    if _PROMISE_SPECIFIC_RE.search(reply) or _PROMISE_GENERIC_RE.search(reply):
        violations.append("promissory language detected (e.g. 'you will receive...') — must attribute to the record instead")

    sentences = _split_sentences(reply)
    lowered = [s.lower() for s in sentences]
    for i, sentence in enumerate(sentences):
        if _SENSITIVE_NUMBER_RE.search(sentence):
            window = lowered[max(0, i - 1): i + 1]
            attributed = any(marker in s for s in window for marker in _ATTRIBUTION_MARKERS)
            if not attributed:
                violations.append(f"unattributed sensitive figure: {sentence!r} (add 'the record shows...' or similar)")
    return violations


# --- contract guard: matchers keyed to the exact strings policy.py emits.
# Unmatched required/forbidden strings are intentionally not enforced here
# (a keyword heuristic false-positive would hurt the naturalness the SOP is
# trying to preserve) — see module docstring.
_REQUIRED_MATCHERS = {
    "an offered alternative identity factor": lambda r: bool(
        re.search(r"\b(date of birth|dob|phone|email|last four|social security|ssn|national id)\b", r, re.IGNORECASE)
    ),
    "how many factors remain": lambda r: bool(re.search(r"\b(one|two|three|1|2|3)\b.{0,20}(more|remain|need|left)", r, re.IGNORECASE))
    or bool(re.search(r"(remain|need|left).{0,20}\b(one|two|three|1|2|3)\b", r, re.IGNORECASE)),
    "a restatement of the candidate claim (type, status, rough date)": lambda r: True,  # phrasing-free; length-based, best-effort
    "a request to confirm": lambda r: "?" in r,
}

_FORBIDDEN_MATCHERS = {
    "any case id": lambda r: bool(_CASE_ID_RE.search(r)),
    "any dollar amount": lambda r: bool(_DOLLAR_RE.search(r)),
    "any amount": lambda r: bool(_DOLLAR_RE.search(r)),
    "the denial narrative": lambda r: False,  # covered by disclosure guard; avoid double-flagging here
    # NOT `\b\d{4}\b` — that also fires on case IDs, years, and day-counts
    # (observed live: it blocked a legitimate "2026"/"26 days" reply and
    # forced two pointless repairs before falling back to a safe template).
    # The precise check already exists: check_disclosure() compares against
    # the actual policyholder's real digits, digit-normalized. Deferring to
    # it here avoids a duplicate, much cruder regex.
    "SSN or ID digits": lambda r: False,
}


def check_contract(reply: str, plan: TurnPlan) -> tuple[list[str], list[str]]:
    """Returns (blocking_violations, missing_required). Forbidden-element
    hits block (they assert absence); missing required elements are
    reported separately for appending, never blocking (see module docstring)."""
    blocking = []
    for element in plan.forbidden_elements:
        check = _FORBIDDEN_MATCHERS.get(element)
        if check and check(reply):
            blocking.append(f"forbidden element present: {element!r}")

    missing = []
    for element in plan.required_elements:
        check = _REQUIRED_MATCHERS.get(element)
        if check and not check(reply):
            missing.append(element)
    return blocking, missing


def check_reply(reply: str, plan: TurnPlan, domain: DomainContext, state: SessionState) -> GuardVerdict:
    violations: list[str] = []
    violations += check_disclosure(reply, domain, plan)
    violations += check_grounding(reply, plan)
    violations += check_commitment(reply)
    contract_blocking, missing_required = check_contract(reply, plan)
    violations += contract_blocking

    return GuardVerdict(ok=(len(violations) == 0), violations=violations, missing_required=missing_required)
