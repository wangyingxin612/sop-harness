"""Phonetic name keys, for matching names that arrived through a speech
recognizer (DESIGN.md §7.2, Appendix B).

WHY THIS IS SAFE, stated up front because loosening an identity matcher
deserves an explicit argument rather than a convenience:

  1. It applies to `full_name` ONLY. Date of birth, phone, email and the ID
     last-four stay exact — those are the high-entropy factors.
  2. A name is low-entropy to begin with. It is printed on the envelope, in
     the phone book, and on every piece of mail the caller has ever
     received. It was never the thing keeping an impostor out.
  3. Verification requires three DISTINCT factor types, and name can be at
     most one of them — so any phonetic match is always accompanied by two
     exact matches on high-entropy factors. That is strictly stronger than
     what a human agent does when they accept a pronunciation.
  4. The match tier is recorded (`exact` vs `phonetic`), so the audit trail
     and the inspector show honestly how identity was established. A
     compliance reviewer can see "the name matched phonetically" rather than
     having to infer it.

Motivated by evidence, not theory: the ASR-noise suite (evals/asr_noise.py)
showed the agent locking out and transferring a legitimate caller on turn
one, because "Margaret Chen" reached it as "margret chan".

Hand-rolled rather than pulling in a phonetics library — the rules that
matter for this are few, and a dependency whose behavior we can't see is a
poor trade for an identity path.
"""
from __future__ import annotations

import re

_VOWELS = set("aeiouy")

# Consonant groups that a recognizer routinely swaps. Mapped to a single
# representative so both spellings produce the same key.
_DIGRAPHS = [
    ("sch", "sk"),
    ("ph", "f"),
    ("ck", "k"),
    ("ch", "x"),
    ("sh", "x"),
    ("th", "0"),
    ("gh", "g"),
    ("kn", "n"),
    ("wr", "r"),
]

# Single letters that sound alike (Soundex's insight, kept minimal).
_EQUIVALENT = str.maketrans({"z": "s", "c": "k", "q": "k", "v": "f", "j": "g"})


def phonetic_key(name: str) -> str:
    """A coarse sound-alike key. Same key == plausibly the same spoken name."""
    s = re.sub(r"[^a-z\s]", "", (name or "").lower())
    parts = []
    for word in s.split():
        parts.append(_word_key(word))
    return " ".join(p for p in parts if p)


def _word_key(word: str) -> str:
    if not word:
        return ""
    for src, dst in _DIGRAPHS:
        word = word.replace(src, dst)
    word = word.translate(_EQUIVALENT)
    first, rest = word[0], word[1:]
    rest = "".join(ch for ch in rest if ch not in _VOWELS)
    key = first + rest
    # Collapse doubled consonants ("Emmett" / "Emet").
    return re.sub(r"(.)\1+", r"\1", key)


def names_sound_alike(a: str, b: str) -> bool:
    """Compares both the word-preserving key and a space-collapsed one,
    because recognizers split and join words freely ("Ya Wen" / "Yawen").

    Note the division of labour with the fixture's `name_aliases`: curated
    aliases cover the specific transcriptions someone has already seen and
    written down (the fixture lists "Yaven Li" for "Ya Wen Li"); this covers
    the long tail nobody enumerated. Keeping the phonetic rules conservative
    and letting the curated list handle known oddities is deliberate — the
    alternative, loosening the rules until every known case passes, would
    over-match names nobody has checked."""
    ka, kb = phonetic_key(a), phonetic_key(b)
    if not ka or not kb:
        return False
    return ka == kb or ka.replace(" ", "") == kb.replace(" ", "")
