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
from app.identity.phonetic import names_sound_alike


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
    """Exact first, then phonetic — the same treatment policyholder names
    get (app/identity/phonetic.py), for the same reason and with the same
    safety argument.

    Consistency here isn't cosmetic: the ASR-noise run turned "David Chen"
    into "david chan", the lookup missed, and the caller was silently
    downgraded to a policyholder — losing the entire representative flow.
    Adding phonetic tolerance to one name table and not the other was simply
    an oversight.

    Matching here establishes only WHO the caller claims to be acting for.
    It grants nothing on its own: the policyholder's own factors still have
    to be verified, and an unmatched claim of representation carries neither
    privilege nor penalty (see tests/test_state_machine.py)."""
    if not stated_rep_name:
        return None
    target = normalize_name(stated_rep_name)
    for r in records:
        if normalize_name(r.rep_name) == target:
            return r
    for r in records:
        if names_sound_alike(target, r.rep_name):
            return r
    return None
