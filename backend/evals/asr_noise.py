"""ASR-noise transforms (DESIGN.md Appendix B, §9.2 P2).

`claim_schema.json`'s first line describes "the insurance **audio** agent
demo" — the real product is voice. That single word explains fixture details
that otherwise look arbitrary: why `name_aliases` and `email_aliases` exist,
why identity needs three factors rather than one, why `id_type` varies.

A full voice pipeline is out of scope, but the *robustness question* a voice
product raises is not: does the harness still work when the text it receives
has been through a speech recognizer? These transforms answer that cheaply —
they corrupt scenario inputs the way ASR actually does, so the existing
scenario suite can be re-run under noise with no new scenarios to maintain.

Everything here is deterministic (seeded) so a noisy run is reproducible and
a regression is attributable.
"""
from __future__ import annotations

import random
import re

_DIGIT_WORDS = {
    "0": "oh", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
}

_MONTHS = {
    "01": "January", "02": "February", "03": "March", "04": "April",
    "05": "May", "06": "June", "07": "July", "08": "August",
    "09": "September", "10": "October", "11": "November", "12": "December",
}

_ORDINALS = {
    1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth",
    7: "seventh", 8: "eighth", 9: "ninth", 10: "tenth", 11: "eleventh",
    12: "twelfth", 13: "thirteenth", 14: "fourteenth", 15: "fifteenth",
    16: "sixteenth", 17: "seventeenth", 18: "eighteenth", 19: "nineteenth",
    20: "twentieth", 21: "twenty-first", 22: "twenty-second", 23: "twenty-third",
    24: "twenty-fourth", 25: "twenty-fifth", 26: "twenty-sixth", 27: "twenty-seventh",
    28: "twenty-eighth", 29: "twenty-ninth", 30: "thirtieth", 31: "thirty-first",
}

# Real recognizer confusions on the fixture's own names. A recognizer doesn't
# produce random typos — it produces plausible-sounding alternatives, which is
# exactly what makes them dangerous for exact-match identity logic.
_HOMOPHONES = {
    "Margaret": "Margret",
    "Chen": "Chan",
    "Ya Wen": "Yaven",
    "Lopez": "Lopes",
    "Tian": "Tien",
}

_FILLERS = ["um", "uh", "you know", "let me see", "hold on"]


def spell_out_digits(text: str) -> str:
    """"4472" -> "four four seven two". The single most common way a spoken
    identity factor reaches a text pipeline."""
    def repl(m: re.Match) -> str:
        return " ".join(_DIGIT_WORDS[d] for d in m.group(0))

    return re.sub(r"\b\d{4}\b", repl, text)


def spoken_dates(text: str) -> str:
    """"1985-03-15" -> "March fifteenth, nineteen eighty-five"."""
    def repl(m: re.Match) -> str:
        y, mo, d = m.group(1), m.group(2), m.group(3)
        month = _MONTHS.get(mo, mo)
        day = _ORDINALS.get(int(d), d)
        year = f"nineteen {_year_words(int(y))}" if y.startswith("19") else y
        return f"{month} {day}, {year}"

    return re.sub(r"\b(\d{4})-(\d{2})-(\d{2})\b", repl, text)


def _year_words(year: int) -> str:
    tail = year % 100
    tens = {80: "eighty", 90: "ninety", 70: "seventy", 60: "sixty"}
    units = {0: "", 1: "-one", 2: "-two", 3: "-three", 4: "-four", 5: "-five",
             6: "-six", 7: "-seven", 8: "-eight", 9: "-nine"}
    return f"{tens.get(tail - tail % 10, str(tail // 10))}{units.get(tail % 10, '')}"


def spell_out_policy_numbers(text: str) -> str:
    """"POL-9921" -> "P O L nine nine two one"."""
    def repl(m: re.Match) -> str:
        letters = " ".join(m.group(1).upper())
        digits = " ".join(_DIGIT_WORDS[d] for d in m.group(2))
        return f"{letters} {digits}"

    return re.sub(r"\b([A-Za-z]{2,4})-(\d{3,6})\b", repl, text)


def confuse_homophones(text: str) -> str:
    for real, heard in _HOMOPHONES.items():
        text = re.sub(rf"\b{re.escape(real)}\b", heard, text)
    return text


def drop_punctuation(text: str) -> str:
    """Many recognizers emit little or no punctuation and no capitalisation."""
    return re.sub(r"[.,;:]", "", text).lower()


_DIGIT_WORD_SET = set(_DIGIT_WORDS.values()) | {"nineteen", "twenty"}


def add_disfluency(text: str, rng: random.Random) -> str:
    """Insert a filler at a plausible spot. Never inside a run of
    spelled-out digits: a speaker saying "P O L nine nine two one" doesn't
    pause mid-number, and inserting one there produces noise no recognizer
    would ever emit — which would make the test harder than reality rather
    than more like it."""
    words = text.split()
    if len(words) < 6:
        return text
    candidates = [
        i for i in range(2, len(words) - 1)
        if words[i - 1].strip(".,") not in _DIGIT_WORD_SET
        and words[i].strip(".,") not in _DIGIT_WORD_SET
    ]
    if not candidates:
        return text
    at = rng.choice(candidates)
    return " ".join([*words[:at], rng.choice(_FILLERS), *words[at:]])


# Profiles, ordered by how badly they mangle the input. `light` is what a good
# recognizer does on a clean line; `heavy` is a bad line on a mobile speaker.
# ORDER MATTERS. The structured transforms (dates, policy numbers) must run
# before the generic 4-digit spell-out, or that one eats "1985" out of a date
# and "9921" out of a policy number and the structured transforms find
# nothing left to convert — which silently made `heavy` identical to
# `moderate` on the first run of this module.
PROFILES: dict[str, list] = {
    "light": [confuse_homophones, drop_punctuation],
    "moderate": [spell_out_digits, confuse_homophones, drop_punctuation],
    "heavy": [
        spoken_dates,
        spell_out_policy_numbers,
        spell_out_digits,
        confuse_homophones,
        drop_punctuation,
    ],
}


def apply_noise(text: str, profile: str = "moderate", seed: int = 0) -> str:
    rng = random.Random(seed)
    out = text
    for fn in PROFILES[profile]:
        out = fn(out)
    if profile in ("moderate", "heavy"):
        out = add_disfluency(out, rng)
    return out
