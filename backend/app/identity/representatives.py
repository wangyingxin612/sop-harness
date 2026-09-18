"""Third-party representative lookup (DESIGN.md §7.10).

A representative is verified by demonstrating knowledge of the POLICYHOLDER's
identity factors, not their own — the fixture has no representative PII, only
a stated relationship. Once a representative is matched, the identity matcher
is restricted to that one policyholder record rather than searched across the
whole book, which removes any ambiguity in the representative path by
construction.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.identity.normalize import normalize_name


@dataclass(frozen=True)
class RepresentativeRecord:
    rep_name: str
    relationship: str
    buyer_name: str
    buyer_party_id: str


def load_representatives(path: str | Path) -> list[RepresentativeRecord]:
    data = json.loads(Path(path).read_text())
    return [
        RepresentativeRecord(
            rep_name=row["rep_name"],
            relationship=row["relationship"],
            buyer_name=row["buyer_name"],
            buyer_party_id=row["buyer_party_id"],
        )
        for row in data
    ]


def find_representative(
    records: list[RepresentativeRecord], stated_rep_name: str
) -> RepresentativeRecord | None:
    if not stated_rep_name:
        return None
    target = normalize_name(stated_rep_name)
    for r in records:
        if normalize_name(r.rep_name) == target:
            return r
    return None
