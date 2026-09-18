"""Normalization for identity factors.

Deliberately narrow scope (DESIGN.md §7.2, §9.3 simplification log): exact /
alias matching only, no fuzzy name matching in the core. Fuzzy tolerance for
ASR-style noise is a documented P2 extension (see EVAL.md, the ASR-noise
suite), not something we want silently loosening a security-relevant match
before it has been measured.
"""
from __future__ import annotations

import re
from datetime import datetime

from dateutil import parser as _dateutil_parser

_WORD_DIGITS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}

_ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11,
    "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15,
    "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
    "twentieth": 20, "twenty-first": 21, "twenty-second": 22,
    "twenty-third": 23, "twenty-fourth": 24, "twenty-fifth": 25,
    "twenty-sixth": 26, "twenty-seventh": 27, "twenty-eighth": 28,
    "twenty-ninth": 29, "thirtieth": 30, "thirty-first": 31,
}


def spoken_digits_to_numeral(raw: str) -> str:
    """'four four seven two' -> '4472'. Leaves already-numeric text alone.
    Cheap, deterministic ASR-noise tolerance (DESIGN.md Appendix B)."""
    if not raw:
        return raw
    tokens = re.findall(r"[a-zA-Z]+|\d+", raw.lower())
    out = []
    for tok in tokens:
        if tok in _WORD_DIGITS:
            out.append(_WORD_DIGITS[tok])
        elif tok.isdigit():
            out.append(tok)
    converted = "".join(out)
    # Fall back to the original if nothing looked like digits (e.g. a name).
    return converted if converted else raw


def normalize_digits_only(raw: str) -> str:
    if not raw:
        return ""
    spoken = spoken_digits_to_numeral(raw)
    digits = re.sub(r"\D", "", spoken)
    return digits


def normalize_phone(raw: str) -> str:
    """Return the last 10 digits (US numbering), dropping a leading country code."""
    digits = normalize_digits_only(raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits[-10:] if len(digits) >= 10 else digits


def normalize_id_last4(raw: str) -> str:
    digits = normalize_digits_only(raw)
    return digits[-4:] if len(digits) >= 4 else digits


def normalize_name(raw: str) -> str:
    if not raw:
        return ""
    s = re.sub(r"[^a-zA-Z\s'-]", "", raw.lower())
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_email(raw: str) -> str:
    return (raw or "").strip().lower()


def _replace_ordinal_words(s: str) -> str:
    pattern = r"\b(" + "|".join(re.escape(w) for w in _ORDINAL_WORDS) + r")\b"

    def repl(m: re.Match) -> str:
        return str(_ORDINAL_WORDS[m.group(0).lower()])

    return re.sub(pattern, repl, s, flags=re.IGNORECASE)


def normalize_dob(raw: str) -> str | None:
    """Best-effort parse to YYYY-MM-DD. Returns None if unparseable — callers
    must treat that as 'no match', never as a wildcard."""
    if not raw or not raw.strip():
        return None
    cleaned = _replace_ordinal_words(raw)
    try:
        dt = _dateutil_parser.parse(cleaned, fuzzy=True, default=datetime(1900, 1, 1))
    except (ValueError, OverflowError, TypeError):
        return None
    if dt.year == 1900 and "1900" not in cleaned:
        # default leaked through — the string had no real year, unparseable for our purposes
        return None
    return dt.strftime("%Y-%m-%d")
