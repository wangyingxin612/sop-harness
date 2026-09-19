"""VERIFY — the output guard (DESIGN.md §7.9). One guard, five rule
families, each independently unit-testable with hand-crafted reply strings —
no LLM needed to exercise this module, same as the policy layer.

    ① disclosure  — nothing not authorized this turn appears
    ② grounding   — every number/date traces to visible_facts
    ③ commitment  — sensitive numbers are attributed, never promised
    ④ contract    — required_elements present, forbidden_elements absent
    ⑤ relevance   — the reply advances the procedure at all

    ⑤ was added after the hostile-model run (evals/hostile_model.py) showed
    that scope was enforced only on the INPUT side: an off-topic reply to an
    on-topic question had nothing checking it, and a banana bread recipe
    reached callers seventeen times while every safety rule held.

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

# Attribution is a PATTERN, not a list of literals. A fixed-string list
# missed "the record ALSO shows ..." — one adverb away from a phrasing that
# is obviously attributed — and burned a repair round-trip on a perfectly
# good sentence. Allowing an optional adverb and a family of reporting verbs
# covers the natural variants without loosening what the rule means.
_ATTRIBUTION_RE = re.compile(
    r"\b("
    r"(the |your |our )?(record|records|file|claim|system|account)s?\s+"
    r"(also\s+|currently\s+|now\s+|still\s+)?"
    r"(shows?|show|indicates?|lists?|reflects?|says?|has)"
    r"|on file"
    r"|according to (the|our|your)"
    r"|it shows"
    r")\b",
    re.IGNORECASE,
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

# Closing the call is a PHASE, not a sentence the model gets to write whenever
# it senses the conversation is over (DESIGN.md §7.7: the summary offer is
# mandatory, R6). Observed live: the model said a warm goodbye from inside
# PROCESS_CASE and skipped POST_PROCESS entirely. This was first patched with
# a prompt rule; it belongs in the guard, because "skipped a required SOP
# step" is exactly the class of thing that should be enforced in code rather
# than requested in prose (§4.1).
_FAREWELL_RE = re.compile(
    # Deliberately EXCLUDES "thanks/thank you for calling": in a contact
    # centre that is a standard GREETING ("thanks for calling about your
    # mother's claim"), not a sign-off. Including it false-positived on an
    # opening line in live testing — the same mistake as putting the generic
    # verb "get" in the promissory list. Signal kept, context constrained.
    r"\b(have a (great|good|nice|lovely) (day|one|rest of your)"
    # "take care" alone is a sign-off; "take care OF those documents" is
    # an instruction. One lookahead separates them.
    r"|take care(?!\s+of)"
    r"|good\s?bye|bye now|bye for now"
    r"|we'?re all set here"
    r"|that'?s everything then)\b",
    re.IGNORECASE,
)


def _looks_like_signoff(reply: str) -> bool:
    """A sign-off is positionally final, so only the tail of the reply is
    examined. "Take care of those documents and call us back" mid-reply is
    not the model closing the call; the same words as the last thing said
    are."""
    sentences = _split_sentences(reply)
    tail = " ".join(sentences[-2:]) if sentences else reply
    return bool(_FAREWELL_RE.search(tail))


@dataclass
class GuardVerdict:
    ok: bool
    violations: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)   # to APPEND, not block on
    # Contract elements this turn asked for that nothing deterministically
    # checked. Not a failure — an admission, surfaced instead of swallowed.
    unverified: list[str] = field(default_factory=list)


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
            attributed = any(_ATTRIBUTION_RE.search(s) for s in window)
            if not attributed:
                violations.append(f"unattributed sensitive figure: {sentence!r} (add 'the record shows...' or similar)")
    return violations


# --- contract guard -------------------------------------------------------
#
# CONTRACT_ELEMENTS is a CLOSED VOCABULARY. Every string policy.py can put in
# required_elements or forbidden_elements must appear here, and each one must
# say how it is enforced. There is no fall-through.
#
# It used to be two loose dicts with a `.get()` between them and policy.py
# writing free text on the other side. An audit found 11 of 20 declared
# elements had no matcher at all — including "an offer to request the
# policyholder's consent", a live R8 requirement, and "sending to any email
# address other than the one on file", which meant nothing would have caught
# the agent offering to mail a summary somewhere new. They were not failing;
# they were never running. Same disease as SEND_NOW.
#
# Three dispositions, and an element must pick one:
#   a callable   — this rule checks it here
#   DELEGATED(x) — deliberately enforced by rule family x instead; checking it
#                  twice would double-flag and cost pointless repair rounds
#   UNCHECKABLE(why) — no deterministic test exists. Admitted in the open so
#                  it shows up in the guard verdict as unverified rather than
#                  masquerading as a check that passed.

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")

_CONSENT_OFFER_RE = re.compile(
    r"\b(consent|authoris|authoriz|permission|approval)\b", re.IGNORECASE
)

# Phrasings that confirm or deny a SPECIFIC identity field — the probing
# channel an impostor uses to enumerate what is on file ("so the DOB is
# wrong?"). R2: verification must never leak field-level feedback.
_FIELD_CONFIRMATION_RE = re.compile(
    r"\b(that (one )?(does not|doesn'?t|did not|didn'?t) match"
    r"|(is|was) (in)?correct"
    r"|(we|i) (do not|don'?t|couldn'?t|could not) (have|find) (a |that )?(record|match)"
    r"|no (record|match) (for|of|on) (that|your)"
    r"|(the|your) (date of birth|dob|phone|email|name|ssn|social) (is|was|does not|doesn'?t)"
    r")\b",
    re.IGNORECASE,
)


class _Delegated:
    def __init__(self, to: str):
        self.to = to


class _Uncheckable:
    def __init__(self, why: str):
        self.why = why


def DELEGATED(to: str) -> _Delegated:  # noqa: N802 - reads as a declaration
    return _Delegated(to)


def UNCHECKABLE(why: str) -> _Uncheckable:  # noqa: N802
    return _Uncheckable(why)


def _sends_to_foreign_address(reply: str, plan: TurnPlan) -> bool:
    """An email address in the reply that is not the one on file.

    R8's send-to-file-address-only rule had no enforcement whatsoever. The
    directive asked the model not to, and that was the entire mechanism."""
    on_file = {
        str(v).lower()
        for v in _flatten_values(plan.visible_facts)
        if "@" in str(v)
    }
    return any(addr.lower() not in on_file for addr in _EMAIL_RE.findall(reply))


CONTRACT_ELEMENTS: dict[str, object] = {
    # ---- required (assert PRESENT; appended when missing, never blocking) --
    "an offered alternative identity factor": lambda r, p: bool(
        re.search(r"\b(date of birth|dob|phone|email|last four|social security|ssn|national id)\b", r, re.IGNORECASE)
    ),
    "how many factors remain": lambda r, p: bool(
        re.search(r"\b(one|two|three|1|2|3)\b.{0,20}(more|remain|need|left)", r, re.IGNORECASE)
    ) or bool(
        re.search(r"(remain|need|left).{0,20}\b(one|two|three|1|2|3)\b", r, re.IGNORECASE)
    ),
    "a request to confirm": lambda r, p: "?" in r,
    "an offer to request the policyholder's consent for full detail, if not already declined":
        lambda r, p: bool(_CONSENT_OFFER_RE.search(r)),
    "a restatement of the candidate claim (type, status, rough date)": UNCHECKABLE(
        "no keyword test distinguishes a good restatement from a bad one; a heuristic "
        "here would cost naturalness for no safety gain"
    ),

    # ---- forbidden (assert ABSENT; blocking) -------------------------------
    "any case id": lambda r, p: bool(_CASE_ID_RE.search(r)),
    "any dollar amount": lambda r, p: bool(_DOLLAR_RE.search(r)),
    "any amount": lambda r, p: bool(_DOLLAR_RE.search(r)),
    "a farewell or sign-off": lambda r, p: _looks_like_signoff(r),
    "sending to any email address other than the one on file": _sends_to_foreign_address,
    "confirmation that a record was found or not found for a specific field":
        lambda r, p: bool(_FIELD_CONFIRMATION_RE.search(r)),

    # ---- deliberately enforced elsewhere -----------------------------------
    # check_disclosure compares against the real fixture values, digit-
    # normalized. A second, cruder regex here would double-flag and burn
    # repair rounds — an earlier `\b\d{4}\b` did exactly that, blocking a
    # legitimate "2026" and "26 days".
    "SSN or ID digits": DELEGATED("check_disclosure"),
    "the denial narrative": DELEGATED("check_disclosure"),
    "denial reason": DELEGATED("check_disclosure"),
    "documents needed": DELEGATED("check_disclosure"),
    "any claim fact": DELEGATED("check_disclosure"),
    "any claim status": DELEGATED("check_disclosure"),
    "any claim amount": DELEGATED("check_disclosure + check_grounding"),
    "any amount or date not present in the claim data provided this turn":
        DELEGATED("check_grounding"),
    # On the refuse route the reply IS the template — policy.resolve() serves
    # it verbatim, so there is no generated text to police.
    "anything other than the refusal": DELEGATED("route=refuse serves template copy"),
    "any answer to the off-topic question": DELEGATED("route=refuse serves template copy"),
}


def check_contract(reply: str, plan: TurnPlan) -> tuple[list[str], list[str], list[str]]:
    """Returns (blocking_violations, missing_required, unverified).

    Forbidden-element hits block — they assert absence. Missing required
    elements are appended rather than blocking (see module docstring).

    `unverified` is the third return value and the reason this function was
    rewritten: an element the vocabulary does not recognise, or one honestly
    marked UNCHECKABLE, is now REPORTED. Previously it was skipped in silence,
    so a renamed element looked exactly like a passing check. Loud enough to
    see in the trace and the Inspector, quiet enough not to break a live call
    over a naming mistake — and evals/architecture.py makes shipping one
    impossible in the first place.
    """
    blocking: list[str] = []
    missing: list[str] = []
    unverified: list[str] = []

    def resolve(element: str):
        if element not in CONTRACT_ELEMENTS:
            unverified.append(f"{element!r} is not in the guard's contract vocabulary")
            return None
        spec = CONTRACT_ELEMENTS[element]
        if isinstance(spec, _Delegated):
            return None                     # checked by another rule family
        if isinstance(spec, _Uncheckable):
            unverified.append(f"{element!r} is not deterministically checkable: {spec.why}")
            return None
        return spec

    for element in plan.forbidden_elements:
        check = resolve(element)
        if check and check(reply, plan):
            blocking.append(f"forbidden element present: {element!r}")

    for element in plan.required_elements:
        check = resolve(element)
        if check and not check(reply, plan):
            missing.append(element)
    return blocking, missing, unverified


# Words that show the reply is addressed to the caller and doing the job,
# rather than being prose that happens to have been generated. Second person
# is the cheapest reliable signal: an agent working someone's case cannot
# help but say "you" or "your".
_ADDRESSES_CALLER_RE = re.compile(
    r"\b(you|your|you're|you'll|you've|we|we'll|we're|i|i'll|i'm|i've|me|my|let's|let me)\b",
    re.I,
)

# A vocative — "Thanks for calling in, Margaret" — is addressing the caller as
# directly as "you" is. Validating the rule against 283 recorded replies
# surfaced exactly two false positives, and this was one of them; the other
# was "let me", now in the pattern above. Both were found by measuring rather
# than by imagining what the rule might catch.
_VOCATIVE_RE = re.compile(r",\s+[A-Z][a-z]{2,}\b")


def _facts_vocabulary(plan: TurnPlan) -> set[str]:
    """Distinctive tokens from what the model was actually given this turn."""
    vocab: set[str] = set()

    def walk(v, depth=0):
        if depth > 6:
            return
        if isinstance(v, dict):
            for item in v.values():
                walk(item, depth + 1)
        elif isinstance(v, list):
            for item in v:
                walk(item, depth + 1)
        elif isinstance(v, str):
            for tok in re.findall(r"[A-Za-z][\w'-]{3,}", v):
                vocab.add(tok.lower())
    walk(plan.visible_facts)
    return vocab


def check_procedural_relevance(reply: str, plan: TurnPlan) -> list[str]:
    """Does this reply advance the procedure at all?

    WHY THIS EXISTS. Scope (R3) was enforced only at the INPUT boundary: an
    off-topic caller message is classified by PERCEIVE and answered with a
    template, deterministically. Nothing checked the other direction — an
    off-topic REPLY to a perfectly on-topic question. The hostile-model run
    (evals/hostile_model.py) walked straight through that gap: a banana bread
    recipe was delivered to callers seventeen times while every
    safety-critical rule held. R3 was, on the output side, being enforced
    entirely by the model's good manners.

    This is deliberately NOT a topic blocklist — a list of forbidden subjects
    is unbounded and always out of date. It is the positive form of the same
    requirement, taken from the SOP's own KEEP_THE_CALL_MOVING directive:
    every reply must ask something, address the caller, or say something
    traceable to the facts of their case. Prose about anything else has none
    of those.

    Conservative on purpose — it fires only when ALL anchors are absent, so
    an empathy line ("I'm sorry to hear about your mother") passes on the
    second person alone, and a one-word acknowledgement is too short to be
    the whole reply anyway. Full output-scope classification needs a model
    call per turn; this is the deterministic floor under it, and DESIGN.md
    §11 records the remaining gap rather than implying it is closed.
    """
    text = reply.strip()
    # Below this length a reply cannot be a substantive off-topic ANSWER, and
    # flagging short filler ("Of course, no problem at all.") would only buy
    # repair round trips on something harmless. Short replies that genuinely
    # break the SOP — a bare "Thanks, goodbye!" skipping the summary offer —
    # are caught by the contract and farewell rules, which is where they
    # belong. Each rule does one job.
    if not text or len(text.split()) < 10:
        return []
    if "?" in text:
        return []
    if _ADDRESSES_CALLER_RE.search(text) or _VOCATIVE_RE.search(text):
        return []
    vocab = _facts_vocabulary(plan)
    if vocab:
        tokens = {t.lower() for t in re.findall(r"[A-Za-z][\w'-]{3,}", text)}
        if tokens & vocab:
            return []
    return [
        "reply does not advance the procedure: it asks nothing, addresses no one, "
        "and states nothing traceable to this caller's case"
    ]


def check_reply(reply: str, plan: TurnPlan, domain: DomainContext, state: SessionState) -> GuardVerdict:
    violations: list[str] = []
    violations += check_disclosure(reply, domain, plan)
    violations += check_grounding(reply, plan)
    violations += check_commitment(reply)
    violations += check_procedural_relevance(reply, plan)
    contract_blocking, missing_required, unverified = check_contract(reply, plan)
    violations += contract_blocking

    return GuardVerdict(
        ok=(len(violations) == 0),
        violations=violations,
        missing_required=missing_required,
        unverified=unverified,
    )
