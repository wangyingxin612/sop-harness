"""Tests for the deterministic identity matcher (DESIGN.md §7.2).

These run with no LLM and no network — they are the demonstration that the
safety-critical core does not depend on a model.
"""
from pathlib import Path

import pytest

from app.identity.matcher import (
    MAX_MISMATCHES,
    MIN_DISTINCT_FACTORS,
    MatchState,
    apply_factor,
    apply_factors,
    lookup_candidates_by_policy_number,
)
from app.identity.records import PolicyholderRecord, load_policyholders

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture(scope="module")
def records():
    return load_policyholders(FIXTURES / "policyholders.json")


def test_margaret_chen_verifies_with_name_dob_ssn4(records):
    """The brief's worked example: name + policy + DOB + SSN4 in one utterance."""
    state = MatchState()
    state = apply_factors(
        state,
        records,
        {"full_name": "Margaret Chen", "dob": "1985-03-15", "id_last4": "4472"},
    )
    assert state.is_verified
    assert state.verified_party_id == "P9"
    assert state.mismatch_count == 0
    assert set(state.matched_factor_types) == {"full_name", "dob", "id_last4"}


def test_two_factors_is_not_enough(records):
    state = MatchState()
    state = apply_factors(records=records, state=state, factors={"full_name": "Margaret Chen", "dob": "1985-03-15"})
    assert not state.is_verified
    assert state.factors_still_needed == 1


def test_policy_number_is_not_an_identity_factor(records):
    """DESIGN.md §7.2: policy_number locates a record but proves nothing."""
    state = MatchState()
    # Even if we tried to feed it through apply_factor, it isn't a supported
    # factor type; the matcher's factor set simply doesn't include it.
    from app.identity.matcher import IDENTITY_FACTOR_TYPES

    assert "policy_number" not in IDENTITY_FACTOR_TYPES

    candidates = lookup_candidates_by_policy_number(records, "POL-9921")
    assert [c.party_id for c in candidates] == ["P9"]
    # Locating a record does not verify it:
    state2 = MatchState()
    assert not state2.is_verified


def test_name_alias_matches(records):
    """Ya Wen Li / Yaven Li — DESIGN.md §7.2 alias requirement."""
    state = MatchState()
    state = apply_factor(state, records, "full_name", "Yaven Li")
    assert "full_name" in state.matched_factor_types
    assert state.candidate_party_ids == ["P13"]


def test_email_alias_matches(records):
    state = MatchState()
    state = apply_factor(state, records, "email", "yawen.li@example.com")
    assert state.candidate_party_ids == ["P13"]


def test_national_id_type_matches_like_ssn(records):
    """id_type has two values in the fixture; the matcher must not hard-code SSN."""
    state = MatchState()
    state = apply_factors(
        state, records, {"full_name": "Ma Tian", "dob": "1964-09-10", "id_last4": "6688"}
    )
    assert state.is_verified
    assert state.verified_party_id == "P12"


def test_phone_normalization_handles_punctuation(records):
    state = MatchState()
    state = apply_factor(state, records, "phone", "(650) 521-2836")
    assert state.candidate_party_ids == ["P9"]


def test_phone_normalization_handles_country_code(records):
    state = MatchState()
    state = apply_factor(state, records, "phone", "1-650-521-2836")
    assert state.candidate_party_ids == ["P9"]


def test_spoken_digits_for_id_last4(records):
    """DESIGN.md Appendix B: ASR-style 'four four seven two' should normalize."""
    state = MatchState()
    state = apply_factor(state, records, "id_last4", "four four seven two")
    assert state.candidate_party_ids == ["P9"]


def test_one_mismatch_does_not_lock_but_is_recorded(records):
    state = MatchState()
    state = apply_factor(state, records, "full_name", "Margaret Chen")
    state = apply_factor(state, records, "dob", "1999-01-01")  # wrong DOB
    assert state.mismatch_count == 1
    assert not state.is_locked
    # The correct candidate is still there (name matched):
    assert state.candidate_party_ids == ["P9"]


def test_two_mismatches_locks(records):
    state = MatchState()
    state = apply_factor(state, records, "dob", "1999-01-01")
    state = apply_factor(state, records, "phone", "555-000-0000")
    assert state.mismatch_count == MAX_MISMATCHES
    assert state.is_locked


def test_locked_state_ignores_further_factors(records):
    state = MatchState()
    state = apply_factor(state, records, "dob", "1999-01-01")
    state = apply_factor(state, records, "phone", "555-000-0000")
    assert state.is_locked
    before = state
    after = apply_factor(state, records, "full_name", "Margaret Chen")
    assert after is before  # frozen once locked


def test_partial_then_recovery_still_verifies(records):
    """One bad factor, then enough good ones to still reach 3 matches."""
    state = MatchState()
    state = apply_factor(state, records, "phone", "000-000-0000")  # mismatch #1
    state = apply_factor(state, records, "full_name", "Margaret Chen")
    state = apply_factor(state, records, "dob", "1985-03-15")
    state = apply_factor(state, records, "id_last4", "4472")
    assert state.mismatch_count == 1
    assert not state.is_locked
    assert state.is_verified
    assert state.verified_party_id == "P9"


def test_conflicting_matched_factors_count_as_mismatch(records):
    """Name matches P9, but a *matching-someone-else* phone should not widen
    the candidate set — it's a mismatch relative to who we've narrowed to."""
    state = MatchState()
    state = apply_factor(state, records, "full_name", "Margaret Chen")
    assert state.candidate_party_ids == ["P9"]
    state = apply_factor(state, records, "phone", "+16503882920")  # Ava Lopez's phone
    assert state.mismatch_count == 1
    assert state.candidate_party_ids == ["P9"]  # unchanged, not widened to include P7


def test_ambiguous_candidates_require_more_factors():
    """Synthetic duplicate-name scenario, independent of the fixture, to
    exercise the ambiguous-candidate branch explicitly."""
    dup_records = [
        PolicyholderRecord(
            party_id="X1", name="Jordan Lee", policy_number="POL-A", dob="1990-01-01",
            id_type="ssn_last4", id_last4="1111", phone="+15550001111", email="a@example.com",
        ),
        PolicyholderRecord(
            party_id="X2", name="Jordan Lee", policy_number="POL-B", dob="1985-05-05",
            id_type="ssn_last4", id_last4="2222", phone="+15550002222", email="b@example.com",
        ),
    ]
    state = MatchState()
    state = apply_factor(state, dup_records, "full_name", "Jordan Lee")
    assert state.is_ambiguous
    assert set(state.candidate_party_ids) == {"X1", "X2"}
    assert not state.is_verified

    # A DOB narrows it to one:
    state = apply_factor(state, dup_records, "dob", "1990-01-01")
    assert state.candidate_party_ids == ["X1"]
    assert not state.is_ambiguous

    # A third factor completes verification:
    state = apply_factor(state, dup_records, "id_last4", "1111")
    assert state.is_verified
    assert state.verified_party_id == "X1"


def test_unknown_person_never_verifies_and_eventually_locks(records):
    state = MatchState()
    state = apply_factor(state, records, "full_name", "Nobody Real")
    state = apply_factor(state, records, "dob", "2000-01-01")
    assert state.mismatch_count == 2
    assert state.is_locked
    assert not state.is_verified


def test_matched_factor_type_not_double_counted_if_restated(records):
    """Restating the same correct factor twice must not let two restatements
    of one factor substitute for two distinct factors."""
    state = MatchState()
    state = apply_factor(state, records, "full_name", "Margaret Chen")
    state = apply_factor(state, records, "full_name", "Margaret Chen")
    assert state.matched_factor_types == ["full_name"]
    assert not state.is_verified


class TestAsrRobustness:
    """Motivated by evidence, not theory: the ASR-noise suite
    (evals/asr_noise.py) showed the agent locking out and transferring a
    legitimate caller on turn one, because heavy speech-recognizer output
    turned "Margaret Chen, DOB 1985-03-15" into "margret chan, march
    fifteenth nineteen eighty-five".

    The pair of tests that matter are the two directions: a real caller must
    get through, and an impostor must NOT — because the fix loosened an
    identity matcher, and a loosening is only defensible if the property it
    was protecting still holds."""

    ASR_HEAVY = {
        "full_name": "margret chan",
        "dob": "march fifteenth nineteen eighty-five",
        "id_last4": "four four seven two",
    }

    def test_legitimate_caller_verifies_through_heavy_asr_noise(self, records):
        state = apply_factors(MatchState(), records, self.ASR_HEAVY)
        assert state.is_verified
        assert state.verified_party_id == "P9"
        assert state.mismatch_count == 0

    def test_the_phonetic_match_is_recorded_not_hidden(self, records):
        """A compliance reviewer must be able to see that the name matched by
        sound rather than exactly — otherwise the audit trail overstates how
        identity was established."""
        from app.identity.matcher import MatchTier, phonetic_match_used

        state = apply_factors(MatchState(), records, self.ASR_HEAVY)
        assert phonetic_match_used(state)
        name_app = next(h for h in state.history if h.factor_type == "full_name")
        assert name_app.tier == MatchTier.PHONETIC

    def test_impostor_with_a_sound_alike_name_still_locks_out(self, records):
        state = apply_factors(
            MatchState(),
            records,
            {"full_name": "margret chan", "dob": "january first two thousand", "id_last4": "1111"},
        )
        assert not state.is_verified
        assert state.is_locked

    def test_phonetic_fallback_applies_to_names_only(self, records):
        """The high-entropy factors must stay exact. A near-miss SSN is a
        mismatch, not a 'close enough'."""
        from app.identity.matcher import MatchTier, records_matching_factor

        for factor, near_miss in [("id_last4", "4473"), ("dob", "1985-03-16"), ("phone", "650-521-2837")]:
            matches, tier = records_matching_factor(records, factor, near_miss)
            assert matches == [], f"{factor} should not have matched {near_miss!r}"
            assert tier == MatchTier.EXACT

    def test_exact_match_still_wins_and_is_labelled_exact(self, records):
        from app.identity.matcher import MatchTier, records_matching_factor

        matches, tier = records_matching_factor(records, "full_name", "Margaret Chen")
        assert [m.party_id for m in matches] == ["P9"]
        assert tier == MatchTier.EXACT

    def test_spoken_dates_parse_across_common_phrasings(self):
        from app.identity.normalize import normalize_dob

        for raw in [
            "March fifteenth, nineteen eighty-five",
            "march fifteenth nineteen eighty-five",
            "March 15th of eighty-five",
            "1985-03-15",
            "03/15/1985",
        ]:
            assert normalize_dob(raw) == "1985-03-15", raw
