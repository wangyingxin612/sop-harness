"""Chat-noise transforms — how real people actually type (DESIGN.md §9.2).

This file replaced an ASR-noise suite that corrupted inputs the way a speech
recognizer does: spelled-out digits, homophone names, dropped punctuation.
That was careful work aimed at the wrong product. The fixture's schema
mentions an audio demo, and the harness may well serve one later, but the
assignment and the delivered interface are a CHAT. Nothing a caller sends has
ever been spoken, so a suite testing recovery from mishearing was measuring
robustness against a failure mode that could not occur.

What does occur, constantly, in a support chat:

  - typos and transpositions        "Margart" / "Chne"
  - doubled or dropped characters   "Margarett" / "Margret"
  - missing capitals and apostrophes "margaret chen", "whats"
  - phone-keyboard autocorrect      "denied" -> "denies"
  - shorthand and abbreviation      "dob", "ssn", "pls", "thx"
  - punctuation collapse            no full stops, run-on clauses
  - a stray keystroke               "1985-03-155"

These are edit-distance errors, which is why identity tolerance is now edit
distance too (app/identity/fuzzy.py) rather than a phonetic keyer. The noise
model and the defence now describe the same failure.

Everything is deterministic (seeded), so a noisy run is reproducible and a
regression is attributable to a specific corruption.
"""
from __future__ import annotations

import random
import re

# Keys physically adjacent on QWERTY — the substitution a real slip makes.
_NEIGHBOURS = {
    "a": "sq", "b": "vn", "c": "xv", "d": "sf", "e": "wr", "f": "dg",
    "g": "fh", "h": "gj", "i": "uo", "j": "hk", "k": "jl", "l": "k",
    "m": "n", "n": "bm", "o": "ip", "p": "o", "q": "wa", "r": "et",
    "s": "ad", "t": "ry", "u": "yi", "v": "cb", "w": "qe", "x": "zc",
    "y": "tu", "z": "x",
}

_ABBREVIATIONS = [
    (r"\bdate of birth\b", "dob"),
    (r"\bsocial security number\b", "ssn"),
    (r"\bplease\b", "pls"),
    (r"\bthanks\b", "thx"),
    (r"\byou\b", "u"),
    (r"\bare\b", "r"),
]


def _rng(text: str, seed: int) -> random.Random:
    return random.Random(f"{seed}:{text}")


def drop_capitals(text: str, seed: int) -> str:
    """Most support chat arrives entirely lower case."""
    return text.lower()


def drop_punctuation(text: str, seed: int) -> str:
    """Commas and full stops are the first casualty of typing in a hurry.
    Question marks survive — people still ask questions."""
    return re.sub(r"[.,;]", "", text)


def drop_apostrophes(text: str, seed: int) -> str:
    return text.replace("'", "").replace("’", "")


def abbreviate(text: str, seed: int) -> str:
    out = text
    for pattern, short in _ABBREVIATIONS:
        out = re.sub(pattern, short, out, flags=re.IGNORECASE)
    return out


def _corrupt_word(word: str, rng: random.Random) -> str:
    """One edit, chosen the way a keyboard actually fails."""
    if len(word) < 4 or not word.isalpha():
        return word
    kind = rng.choice(("transpose", "double", "drop", "neighbour"))
    i = rng.randrange(1, len(word) - 1)
    if kind == "transpose":
        return word[:i] + word[i + 1] + word[i] + word[i + 2:]
    if kind == "double":
        return word[:i] + word[i] * 2 + word[i:]
    if kind == "drop":
        return word[:i] + word[i + 1:]
    sub = _NEIGHBOURS.get(word[i].lower())
    return word[:i] + (rng.choice(sub) if sub else word[i]) + word[i + 1:]


def _typo_at_rate(rate: float):
    def transform(text: str, seed: int) -> str:
        rng = _rng(text, seed)
        return " ".join(
            _corrupt_word(w, rng) if rng.random() < rate else w
            for w in text.split(" ")
        )
    return transform


def stray_digit(text: str, seed: int) -> str:
    """A doubled keystroke inside a number. Deliberately nasty: identity
    tolerance is names-only, so this SHOULD fail to match — the question
    under test is whether the harness degrades politely or locks the caller
    out (DESIGN.md §7.2)."""
    rng = _rng(text, seed)
    digits = list(re.finditer(r"\d{4,}", text))
    if not digits:
        return text
    m = rng.choice(digits)
    return text[: m.end()] + m.group(0)[-1] + text[m.end():]


PROFILES: dict[str, list] = {
    # Ordered so structural transforms run before character-level ones —
    # abbreviating after introducing typos would mostly miss its patterns.
    "light": [drop_capitals, drop_punctuation],
    "moderate": [drop_capitals, drop_punctuation, drop_apostrophes, abbreviate, _typo_at_rate(0.12)],
    "heavy": [drop_capitals, drop_punctuation, drop_apostrophes, abbreviate, _typo_at_rate(0.28), stray_digit],
}


def apply_noise(text: str, profile: str | None, seed: int = 0) -> str:
    if not profile or profile not in PROFILES:
        return text
    out = text
    for fn in PROFILES[profile]:
        out = fn(out, seed)
    return out
