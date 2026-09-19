"""Typo-tolerant name matching (DESIGN.md §7.2, Appendix B).

WHY THIS IS SAFE, stated up front because loosening an identity matcher
deserves an explicit argument rather than a convenience:

  1. It applies to `full_name` ONLY. Date of birth, phone, email and the ID
     last-four stay exact — those are the high-entropy factors.
  2. A name is low-entropy to begin with. It is printed on the envelope, in
     the phone book, and on every piece of mail the caller has ever
     received. It was never the thing keeping an impostor out.
  3. Verification requires three DISTINCT factor types, and the name can be
     at most one of them — so any fuzzy match is always accompanied by two
     exact matches on high-entropy factors. That is strictly stronger than
     what a human agent does when they accept a spelling.
  4. The match tier is recorded (`exact` vs `fuzzy`), so the audit trail and
     the Inspector show honestly how identity was established. A compliance
     reviewer sees "the name matched approximately" rather than inferring it.

WHY EDIT DISTANCE AND NOT PHONETICS. An earlier version of this file was a
hand-rolled phonetic keyer — consonant digraphs, vowel folding, the usual
Soundex-adjacent machinery — built to survive a speech recognizer mangling
"Margaret Chen" into "margret chan".

This product is a CHAT. Nothing here has ever been spoken. The failure mode
that actually exists is a typo, a doubled letter, a transposition, a missing
accent, an autocorrect — and those are edit-distance errors, not phonetic
ones. Building phonetic matching for a text channel was solving a problem
the product does not have, and it made the identity path harder to reason
about than the thing it protected against. Same mechanism, same safety
argument, correct failure model, a third of the code.

Bounded deliberately: one edit for a short name, two for a long one. Enough
for a fat-fingered key, not enough to walk from one name in the fixture set
to another.
"""
from __future__ import annotations

import re

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _key(name: str) -> str:
    """Case, punctuation and spacing folded away. This alone resolves the
    fixture's 'Ya Wen Li' / 'Yaven Li' pair down to a single edit."""
    return _NON_ALNUM.sub("", (name or "").lower())


def _within(a: str, b: str, limit: int) -> bool:
    """True when `a` and `b` differ by at most `limit` edits.

    Classic Levenshtein with a band: we only ever ask about small limits, so
    the O(n*m) table is a few hundred cells on the longest realistic name.
    """
    if abs(len(a) - len(b)) > limit:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(
                prev[j] + 1,            # deletion
                cur[j - 1] + 1,         # insertion
                prev[j - 1] + (ca != cb),  # substitution
            ))
        # Nothing in this row is close enough, and rows only grow.
        if min(cur) > limit:
            return False
        prev = cur
    return prev[-1] <= limit


def edit_budget(key: str) -> int:
    """One edit for a short name, two once there is enough length that a
    single slip does not meaningfully reduce the space of candidates."""
    return 2 if len(key) >= 10 else 1


def names_match_loosely(stated: str, on_record: str) -> bool:
    """Would a careful human accept these as the same name typed twice?"""
    a, b = _key(stated), _key(on_record)
    if not a or not b:
        return False
    if a == b:
        return True
    return _within(a, b, edit_budget(max(a, b, key=len)))
