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
    verification_attempts: int = 0
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    caller_role: CallerRole = CallerRole.UNKNOWN
    verified_party_id: Optional[str] = None
    candidate_party_ids: list[str] = field(default_factory=list)  # ambiguous-candidate tracking

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
    repeated_request_count: int = 0
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
    escalation_reason: Optional[str] = None

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
    terminal_reason: Optional[str] = None   # set when this turn ends in handoff/termination
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
