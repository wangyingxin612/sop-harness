"""Core types for the SOP engine.

Design invariant (see DESIGN.md §4, §7.4): everything in this module is data.
No LLM calls happen anywhere near these types. `resolve()` in policy.py is a
pure function over these types, which is what makes the safety core testable
without a model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


# --------------------------------------------------------------------------
# Phases (DESIGN.md §7.1) — permission scopes, not conversation steps.
# --------------------------------------------------------------------------

class Phase(str, Enum):
    VERIFY_ID = "VERIFY_ID"
    RESOLVE_INTENT = "RESOLVE_INTENT"
    PROCESS_CASE = "PROCESS_CASE"
    POST_PROCESS = "POST_PROCESS"
    HUMAN_HANDOFF = "HUMAN_HANDOFF"
    ABUSE_TERMINATED = "ABUSE_TERMINATED"
    CLOSED = "CLOSED"


# Phases from which HUMAN_HANDOFF and ABUSE_TERMINATED are always reachable.
TERMINAL_PHASES = {Phase.HUMAN_HANDOFF, Phase.ABUSE_TERMINATED, Phase.CLOSED}

# The one-way gate (DESIGN.md §7.1): once left, VERIFY_ID is never re-entered
# within a session. A new verification requires a new session.
FORWARD_ONLY_FROM = {Phase.VERIFY_ID}


class CallerRole(str, Enum):
    UNKNOWN = "unknown"
    POLICYHOLDER = "policyholder"
    REPRESENTATIVE = "representative"


class VerificationStatus(str, Enum):
    UNVERIFIED = "unverified"
    AMBIGUOUS = "ambiguous"          # multiple candidate records match so far
    VERIFIED = "verified"
    LOCKED = "locked"                # too many mismatches; must transfer


class ConsentStatus(str, Enum):
    NOT_REQUESTED = "not_requested"
    PENDING = "pending"
    APPROVED = "approved"
    TIMED_OUT = "timed_out"
    DECLINED = "declined"


IDENTITY_FACTOR_TYPES = ("full_name", "dob", "phone", "email", "id_last4")


# Every reason a session can end, in one place — and every projection of it
# derived rather than maintained.
#
# This started as string literals scattered across four modules, with three
# separate hand-written tables translating between them: reason -> disposition
# code, reason -> handoff guidance, and reason -> terminal phase chosen inline
# at each call site. Keeping three tables in sync by hand is the same disease
# as the guard's contract vocabulary and as SEND_NOW: a value added on one
# side and forgotten on another produces no error, just an enforcement or a
# label that quietly stops working.
#
# So there is one authored representation — the reason — and the phase, the
# disposition and the handoff guidance are all read out of it. Adding a way
# for a session to end is now one entry, and a missing field is a TypeError
# at import rather than a silent default months later.


@dataclass(frozen=True)
class EndReason:
    id: str
    description: str
    # Which terminal phase this projects to. The phase is a PROJECTION of the
    # reason, never a parallel fact — see machine.end_session().
    phase: "Phase"
    # The disposition code, or None when it cannot be known from the reason
    # alone (an ordinary close depends on whether a case was worked and
    # whether the summary was sent — see disposition.classify()).
    disposition: str | None
    # What the receiving human is told, when this is a transfer.
    next_step: str | None = None
    # May a CLIENT assert this reason? A browser can say the caller gave up.
    # It does not get to declare an identity failure — that is a conclusion
    # only the server is entitled to reach.
    client_assertable: bool = False


def _end_reasons() -> dict[str, EndReason]:
    P = Phase
    return {r.id: r for r in (
        EndReason(
            "caller_requested_human", "the caller asked for a person",
            P.HUMAN_HANDOFF, "TRANSFERRED_CALLER_REQUEST",
            "Caller asked for a person directly — no persuasion needed, just continue "
            "where this left off.",
        ),
        EndReason(
            "identity_verification_failed", "identity could not be established",
            P.HUMAN_HANDOFF, "TRANSFERRED_IDENTITY_FAILED",
            "Identity could not be verified through the automated line. Re-verify manually "
            "with photo ID or account-specific knowledge before discussing any case.",
        ),
        EndReason(
            "repeated_prompt_injection_attempts", "repeated attempts to manipulate the agent",
            P.HUMAN_HANDOFF, "TRANSFERRED_INJECTION_ATTEMPTS",
            "Caller's messages repeatedly attempted to manipulate the automated system. "
            "Proceed with normal verification; no case data was ever exposed to the agent.",
        ),
        # ABUSE_TERMINATED, not HUMAN_HANDOFF. Writing the registry exposed a
        # contradiction that had been live the whole time: machine.py sent
        # this reason to ABUSE_TERMINATED while the disposition table mapped
        # it to TRANSFERRED_OFF_TOPIC — a code that could never fire, because
        # classify() branches on phase first. The SOP's own ABUSE_CLOSING
        # copy ("repeated off-topic requests mean the session needs to end
        # here") says termination is the intended behaviour, so the label was
        # what was wrong. It also mattered: TRANSFERRED counts in the
        # containment denominator and TERMINATED does not.
        EndReason(
            "repeated_off_topic_requests", "persistent out-of-scope requests",
            P.ABUSE_TERMINATED, "TERMINATED_ABUSE",
        ),
        EndReason(
            "agent_initiated_transfer", "the agent judged a person was needed",
            P.HUMAN_HANDOFF, "TRANSFERRED_AGENT_JUDGEMENT",
            "The automated agent judged this needed a person — see the emotional state and "
            "attempted paths below for why.",
        ),
        EndReason(
            "caller_inactive", "silence past the SOP's ceiling",
            P.CLOSED, "ABANDONED_AFTER_SILENCE", client_assertable=True,
        ),
        EndReason(
            "caller_window_closed", "the window was closed and never came back",
            P.CLOSED, "ABANDONED_WINDOW_CLOSED",
        ),
        EndReason(
            "caller_finished", "the caller said they were done",
            P.CLOSED, None, client_assertable=True,
        ),
        EndReason(
            "operator_closed", "closed from the operations side",
            P.CLOSED, None, client_assertable=True,
        ),
    )}


END_REASONS: dict[str, EndReason] = _end_reasons()

# Reasons a browser is allowed to assert via the close endpoint.
CLIENT_CLOSE_REASONS: frozenset[str] = frozenset(
    r.id for r in END_REASONS.values() if r.client_assertable
)


@dataclass(frozen=True)
class EndState:
    """How and when this session ended. The single authored fact; phase and
    disposition are read out of it."""
    reason: str
    at_turn: int

    @property
    def spec(self) -> EndReason:
        return END_REASONS[self.reason]


class ScopeRing(str, Enum):
    CORE = "core"
    ADJACENT = "adjacent"
    OUT = "out"


# --------------------------------------------------------------------------
# Memory: typed slots with provenance (DESIGN.md §7.3)
# --------------------------------------------------------------------------

@dataclass
class Slot:
    """One remembered fact, with enough provenance to audit and to support
    self-correction (`superseded_by`)."""

    value: Any
    verbatim_quote: str
    turn_index: int
    confidence: float = 1.0
    source: str = "extracted"        # "extracted" | "deterministic" | "tool"
    superseded_by: Optional["Slot"] = None
    recorded_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def current(self) -> "Slot":
        """Follow the supersede chain to the latest value."""
        s = self
        while s.superseded_by is not None:
            s = s.superseded_by
        return s


@dataclass
class CaseHint:
    case_type: Optional[str] = None
    status: Optional[str] = None
    time_ref: Optional[str] = None   # e.g. "January" — deliberately not resolved to a date here
    verbatim_quote: str = ""
    turn_index: int = 0


@dataclass
class Memory:
    """All cross-turn, cross-phase state that isn't a counter or a flag.

    Unconditional extraction (DESIGN.md §7.3) means every field here can be
    populated from *any* phase. That is the mechanism behind R8: a case hint
    stated during VERIFY_ID lands here and is read by RESOLVE_INTENT without
    any special-casing.
    """

    identity_slots: dict[str, Slot] = field(default_factory=dict)   # keyed by factor type
    case_hints: list[CaseHint] = field(default_factory=list)
    candidate_case_id: Optional[str] = None    # proposed by RESOLVE_INTENT, awaiting caller confirmation
    confirmed_case_id: Optional[str] = None    # confirmed; this is what PROCESS_CASE answers about
    active_case_id: Optional[str] = None       # may diverge from confirmed_case_id after an in-phase switch (§7.1)
    resolved_intent: Optional[str] = None
    intent_evidence_quote: str = ""
    intent_confidence: float = 0.0
    contact_change_requests: list[dict] = field(default_factory=list)  # §7.12 — never auto-applied
    anomalies: list[dict] = field(default_factory=list)               # contradicting post-gate info
    consent_events: list[dict] = field(default_factory=list)          # §7.2 action-gate evidence
    followup_notes: list[str] = field(default_factory=list)           # from create_followup tool calls

    def set_identity_factor(self, factor_type: str, slot: Slot) -> None:
        existing = self.identity_slots.get(factor_type)
        if existing is None:
            self.identity_slots[factor_type] = slot
        else:
            cur = existing.current()
            cur.superseded_by = slot

    def matched_factor_types(self) -> list[str]:
        return [k for k, s in self.identity_slots.items() if s.current().source == "matched"]


# --------------------------------------------------------------------------
# Session-level counters & flags (DESIGN.md §9.3 simplification — one flat
# record instead of several orthogonal "regions")
# --------------------------------------------------------------------------

@dataclass
class SessionFacts:
    # identity gate
    matched_factor_count: int = 0
    matched_factor_types: list[str] = field(default_factory=list)
    mismatch_count: int = 0
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    caller_role: CallerRole = CallerRole.UNKNOWN
    verified_party_id: Optional[str] = None
    candidate_party_ids: list[str] = field(default_factory=list)  # ambiguous-candidate tracking
    # True when a name matched by sound rather than exactly. Surfaced rather
    # than hidden so the inspector and the audit trail never overstate how
    # identity was established (app/identity/fuzzy.py).
    fuzzy_match_used: bool = False

    # representative / consent (DESIGN.md §7.10)
    representative_of_party_id: Optional[str] = None
    consent_status: ConsentStatus = ConsentStatus.NOT_REQUESTED
    consent_poll_count: int = 0

    # abuse / scope budget (DESIGN.md §7.9)
    off_topic_strikes: int = 0
    injection_flags: int = 0
    turns_used: int = 0
    tokens_used: int = 0
    cost_usd: float = 0.0

    # emotion (deterministic floor inputs — DESIGN.md §7.3)
    refusal_count: int = 0
    last_intensity: int = 0     # THIS turn's reading — what directive selection acts on (§7.4)
    peak_intensity: int = 0     # session-wide max — what the handoff packet reports (§7.6): a caller
                                 # who was furious two turns ago and has since gone quiet is still a
                                 # caller a human agent should be told was furious

    # last turn's scope classification, persisted so `resolve()` can stay a
    # single-argument function of `state` alone, matching DESIGN.md Appendix A
    last_scope: ScopeRing = ScopeRing.CORE

    # action gates
    pending_action: Optional["PendingAction"] = None
    email_sent: bool = False
    email_skipped: bool = False

    # set only when transition() routes to HUMAN_HANDOFF / ABUSE_TERMINATED —
    # the source of truth for "why", used by policy.py and the handoff packet
    # The single authored fact about how this session ended. Phase and
    # disposition are PROJECTIONS of it (see END_REASONS), never parallel
    # state that has to be kept in step.
    ended: Optional[EndState] = None
    # The browser told us the tab was closing (pagehide, sent with keepalive).
    # EVIDENCE, not proof: pagehide also fires on a refresh or a navigation.
    # It only becomes a conclusion once the session then stays silent past
    # its ceiling — see session/sweeper.py. Kept on facts rather than in the
    # event log so that disposition.classify() stays a pure function of
    # state, which is the property that makes the metric defensible.
    window_closed: bool = False
    # The caller has said, at least once, that they are finished.
    #
    # STICKY on purpose. This used to be read only off the current turn's
    # signals, so "that's it, thanks" was consumed to move into POST_PROCESS
    # and then forgotten. The caller was asked about the summary, answered,
    # and was then asked AGAIN whether they needed anything else — having
    # already said twice that they did not. Intent that the caller has
    # expressed does not stop being true because a phase boundary happened.
    wrap_up_signalled: bool = False

    def is_verified(self) -> bool:
        return self.verification_status == VerificationStatus.VERIFIED


@dataclass
class PendingAction:
    """The one true sub-state (DESIGN.md §9.3 / Appendix A). Represents an
    action gate awaiting explicit caller consent. Nothing else uses this —
    side-effecting tool calls execute synchronously once consent is given
    (DESIGN.md §6 / Appendix A "when a side-effecting tool call executes")."""

    action_type: str                 # "send_summary_email" | ...
    payload: dict = field(default_factory=dict)
    requested_at_turn: int = 0


# --------------------------------------------------------------------------
# Turn-level signals produced by PERCEIVE (DESIGN.md §7.3, §7.11)
# --------------------------------------------------------------------------

@dataclass
class TurnSignals:
    """Output of the blocking half of PERCEIVE, plus whatever the deferred
    half (riding on ACT's `memory_updates`, §7.3/§7.11) has contributed by
    the time DECIDE runs for a given turn. Every field has a safe default —
    DECIDE must be total over partial input (§7.3)."""

    # blocking
    scope: ScopeRing = ScopeRing.CORE
    escalation_request: bool = False
    identity_candidates: dict[str, str] = field(default_factory=dict)  # factor_type -> raw value
    injection_suspected: bool = False

    # deferred-but-may-have-arrived
    case_hint: Optional[CaseHint] = None
    intent: Optional[str] = None
    intent_confidence: float = 0.0
    intent_evidence_quote: str = ""
    negative_affect: bool = False
    refusal: bool = False
    confusion: bool = False
    intensity: int = 0               # 0..3, model signal only; DECIDE applies the floor
    contact_change_request: Optional[dict] = None

    # phase-gate specific (still "signals": derived from this turn's message,
    # not decisions — DECIDE turns them into decisions)
    claims_representative: bool = False
    representative_name: Optional[str] = None
    confirms_proposed_case: Optional[bool] = None   # True/False/None=unaddressed
    wrap_up_request: bool = False
    consent_response: Optional[bool] = None          # for the POST_PROCESS email action gate
    # The caller explicitly asked us to seek the policyholder's
    # authorisation. An instruction, not a judgement call — acted on
    # deterministically in transition(), like an explicit ask for a human.
    requests_consent: bool = False

    raw_message: str = ""
    turn_index: int = 0


# --------------------------------------------------------------------------
# TurnPlan — the sole output of DECIDE, the sole input (besides transcript)
# to ACT. (DESIGN.md §7.4, Appendix A)
# --------------------------------------------------------------------------

class Tier(str, Enum):
    FAST = "fast"
    STRONG = "strong"


class StreamPolicy(str, Enum):
    BUFFERED = "buffered"
    SENTENCE_GATED = "sentence_gated"


@dataclass(frozen=True)
class Directive:
    id: str
    text: str                        # instruction text compiled into the system prompt


@dataclass(frozen=True)
class TurnPlan:
    phase: Phase
    allowed_tools: tuple[str, ...]
    visible_facts: dict = field(default_factory=dict)
    directives: tuple[Directive, ...] = field(default_factory=tuple)
    required_elements: tuple[str, ...] = field(default_factory=tuple)
    forbidden_elements: tuple[str, ...] = field(default_factory=tuple)
    model_tier: Tier = Tier.STRONG
    stream_policy: StreamPolicy = StreamPolicy.SENTENCE_GATED
    route: str = "normal"                   # "normal" | "human_handoff" | "refuse" | "abuse_terminated"


# --------------------------------------------------------------------------
# Session state — everything the policy resolver reads.
# --------------------------------------------------------------------------

@dataclass
class Turn:
    turn_index: int
    role: str                        # "caller" | "agent"
    text: str
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    trace_event: Optional[dict] = None   # populated for agent turns; see obs/trace.py


@dataclass
class SessionState:
    session_id: str
    sop_name: str
    phase: Phase = Phase.VERIFY_ID
    memory: Memory = field(default_factory=Memory)
    facts: SessionFacts = field(default_factory=SessionFacts)
    transcript: list[Turn] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # Selects a fixture scenario from consent_scenarios.json — a demo/test
    # knob (DESIGN.md §7.10), not something a real caller controls.
    consent_scenario: str = "default"
    # Transient: this turn's raw caller message, set by transition() before
    # the caller's Turn is appended to `transcript` (that happens later in
    # the orchestrator). Exists so resolve() — called between the two — can
    # still see it, e.g. for the handoff packet (§7.6). Same pattern as
    # `facts.last_scope`: a turn-scoped signal persisted into state so
    # resolve() stays a function of `state` alone (Appendix A).
    current_raw_message: str = ""

    def next_turn_index(self) -> int:
        return len(self.transcript)
