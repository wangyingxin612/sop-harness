"""Load and index the policyholder fixture."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class PolicyholderRecord:
    party_id: str
    name: str
    policy_number: str
    dob: str
    id_type: str
    id_last4: str
    phone: str
    email: str
    name_aliases: tuple[str, ...] = field(default_factory=tuple)
    phone_aliases: tuple[str, ...] = field(default_factory=tuple)
    email_aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def all_names(self) -> tuple[str, ...]:
        return (self.name, *self.name_aliases)

    @property
    def all_phones(self) -> tuple[str, ...]:
        return (self.phone, *self.phone_aliases)

    @property
    def all_emails(self) -> tuple[str, ...]:
        return (self.email, *self.email_aliases)


def load_policyholders(path: str | Path) -> list[PolicyholderRecord]:
    data = json.loads(Path(path).read_text())
    records = []
    for row in data:
        records.append(
            PolicyholderRecord(
                party_id=row["party_id"],
                name=row["name"],
                policy_number=row["policy_number"],
                dob=row["dob"],
                id_type=row["id_type"],
                id_last4=row["id_last4"],
                phone=row["phone"],
                email=row["email"],
                name_aliases=tuple(row.get("name_aliases", [])),
                phone_aliases=tuple(row.get("phone_aliases", [])),
                email_aliases=tuple(row.get("email_aliases", [])),
            )
        )
    return records


def index_by_policy_number(records: list[PolicyholderRecord]) -> dict[str, PolicyholderRecord]:
    return {r.policy_number: r for r in records}


def index_by_party_id(records: list[PolicyholderRecord]) -> dict[str, PolicyholderRecord]:
    return {r.party_id: r for r in records}
