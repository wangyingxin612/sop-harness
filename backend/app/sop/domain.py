"""Claims + document-guideline knowledge, loaded once and treated as
read-only data. This is the "domain adapter" from DESIGN.md's architecture
diagram (§5.1) — the one part of the system with insurance-specific shape.
The engine (spec loader, gates, policy resolver) never imports insurance
vocabulary directly; it reads `DomainContext` through a generic interface.

Derived facts (DESIGN.md §7.4 "the model does no arithmetic") are computed
here, once, at load time — not by the model at generation time.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from app.identity.records import PolicyholderRecord, load_policyholders
from app.identity.representatives import RepresentativeRecord, load_representatives


@dataclass(frozen=True)
class ClaimRecord:
    case_id: str
    party_id: str
    case_type: str
    created_at: str
    status: str
    summary: str
    expected_reimbursement_amount: str
    allowed_max_amount: str
    net_pay: str
    net_fee: str
    denial_reason: str | None = None
    documents_needed: tuple[str, ...] = field(default_factory=tuple)
    appeal_deadline: str | None = None

    def created_year(self) -> int:
        return int(self.created_at[:4])

    def created_month(self) -> int:
        return int(self.created_at[5:7])


_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


def month_from_time_ref(time_ref: str | None) -> int | None:
    if not time_ref:
        return None
    return _MONTH_NAMES.get(time_ref.strip().lower())


@dataclass(frozen=True)
class ClaimView:
    """A ClaimRecord plus every derived value the model is allowed to state,
    pre-computed so generation never has to do arithmetic (DESIGN.md §7.4)."""

    record: ClaimRecord
    unpaid_balance: str
    days_until_appeal_deadline: int | None
    appeal_deadline_passed: bool | None

    @property
    def case_id(self) -> str:
        return self.record.case_id


def _to_cents(amount: str) -> int:
    return round(float(amount) * 100)


def build_claim_view(record: ClaimRecord, now: date) -> ClaimView:
    unpaid_cents = _to_cents(record.allowed_max_amount) - _to_cents(record.net_pay)
    unpaid_balance = f"{unpaid_cents / 100:.2f}"

    days_left: int | None = None
    passed: bool | None = None
    if record.appeal_deadline:
        deadline = datetime.strptime(record.appeal_deadline, "%Y-%m-%d").date()
        days_left = (deadline - now).days
        passed = days_left < 0

    return ClaimView(
        record=record,
        unpaid_balance=unpaid_balance,
        days_until_appeal_deadline=days_left,
        appeal_deadline_passed=passed,
    )


def load_claims(path: str | Path) -> list[ClaimRecord]:
    data = json.loads(Path(path).read_text())
    out = []
    for row in data:
        out.append(
            ClaimRecord(
                case_id=row["case_id"],
                party_id=row["party_id"],
                case_type=row["case_type"],
                created_at=row["created_at"],
                status=row["status"],
                summary=row["summary"],
                expected_reimbursement_amount=row["expected_reimbursement_amount"],
                allowed_max_amount=row["allowed_max_amount"],
                net_pay=row["net_pay"],
                net_fee=row["net_fee"],
                denial_reason=row.get("denial_reason"),
                documents_needed=tuple(row.get("documents_needed", [])),
                appeal_deadline=row.get("appeal_deadline"),
            )
        )
    return out


@dataclass
class GuidanceEntry:
    topic: str
    text: str
    source: str        # "claim_followup_guidance" | "document_guidance" | "document_alternative_guidance" | ...


class GuidelineKB:
    """Wraps required_document_guideline.json. Matching is deterministic
    keyword/hint matching, not an LLM call — see DESIGN.md §7.4/§7.6: this
    is the data source behind the "alternative ladder," the single highest-
    value section in the fixture set for containment."""

    def __init__(self, raw: dict):
        self._raw = raw

    @classmethod
    def load(cls, path: str | Path) -> "GuidelineKB":
        return cls(json.loads(Path(path).read_text()))

    def document_guidance(self, document_name: str) -> str | None:
        entry = self._raw.get("document_guidance", {}).get(document_name)
        return entry["en"] if entry else None

    def document_alternative_guidance(self, document_name: str) -> str:
        alts = self._raw.get("document_alternative_guidance", {})
        entry = alts.get(document_name) or alts.get("default")
        return entry["en"] if entry else ""

    def case_type_guidance(self, case_type: str) -> str | None:
        entry = self._raw.get("case_type_guidance", {}).get(case_type)
        return entry["en"] if entry else None

    def default_guidance(self) -> str:
        return self._raw.get("default_guidance", {}).get("en", "")

    def followup_settings(self) -> dict:
        return self._raw.get("claim_followup_settings", {})

    def followup_fallback(self) -> str:
        return self._raw.get("claim_followup_fallback", {}).get("en", "")

    def match_followup(
        self, message: str, intent: str | None, has_documents_needed: bool
    ) -> list[GuidanceEntry]:
        """Deterministic matching against claim_followup_guidance: filter by
        intent_hints and requires_documents, then rank matches whose
        `match_any` keyword list appears in the caller's message above
        entries that only match on intent."""
        msg = (message or "").lower()
        candidates = self._raw.get("claim_followup_guidance", [])
        keyword_hits: list[GuidanceEntry] = []
        intent_only_hits: list[GuidanceEntry] = []

        for entry in candidates:
            if entry.get("requires_documents") and not has_documents_needed:
                continue
            intent_hints = entry.get("intent_hints", [])
            if intent and intent not in intent_hints:
                continue
            match_any = entry.get("match_any", [])
            text = entry["en"]
            topic = entry["topic"]
            if match_any and any(kw in msg for kw in match_any):
                keyword_hits.append(GuidanceEntry(topic=topic, text=text, source="claim_followup_guidance"))
            elif not match_any:
                intent_only_hits.append(GuidanceEntry(topic=topic, text=text, source="claim_followup_guidance"))

        return keyword_hits + intent_only_hits

    def format_entry(self, entry_template: str, claim: ClaimRecord) -> str:
        documents = ", ".join(claim.documents_needed) if claim.documents_needed else ""
        settings = self.followup_settings()
        avg_time = settings.get("average_processing_time_after_submission", {}).get("en", "")
        return entry_template.format(
            case_id=claim.case_id,
            documents=documents,
            average_processing_time_after_submission=avg_time,
        )


@dataclass
class DomainContext:
    """Everything the policy resolver and tool layer need, loaded once and
    treated as immutable for the lifetime of the process (or refreshed
    wholesale between requests in a real backend)."""

    claims: list[ClaimRecord]
    guideline_kb: GuidelineKB
    policyholders: list[PolicyholderRecord]
    representatives: list[RepresentativeRecord]
    now: date

    def policyholder_by_party_id(self, party_id: str) -> PolicyholderRecord | None:
        for p in self.policyholders:
            if p.party_id == party_id:
                return p
        return None

    def claims_for_party(self, party_id: str) -> list[ClaimRecord]:
        return [c for c in self.claims if c.party_id == party_id]

    def claim_by_id(self, case_id: str) -> ClaimRecord | None:
        for c in self.claims:
            if c.case_id == case_id:
                return c
        return None

    def view(self, case_id: str) -> ClaimView | None:
        record = self.claim_by_id(case_id)
        return build_claim_view(record, self.now) if record else None


def load_domain(fixtures_dir: str | Path, now: date) -> DomainContext:
    fixtures_dir = Path(fixtures_dir)
    return DomainContext(
        claims=load_claims(fixtures_dir / "claims.json"),
        guideline_kb=GuidelineKB.load(fixtures_dir / "required_document_guideline.json"),
        policyholders=load_policyholders(fixtures_dir / "policyholders.json"),
        representatives=load_representatives(fixtures_dir / "representatives.json"),
        now=now,
    )
