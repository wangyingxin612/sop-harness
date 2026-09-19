"""R9: empathy must be INSTRUCTED, not left to the model's manners.

Found by evals/attribution.py, not by a human reading transcripts — which is
the point of building that report. ACKNOWLEDGE_EMOTION never fired once across
the whole scenario suite, including `frustrated_caller_bonus`, whose entire
purpose is a caller saying "this is ridiculous". The empathy in those replies
was real and was the model being nice. An unguarded pass is hardest to see
precisely when the model is good at the thing.

Root cause: `signals.intensity` is a DEFERRED perception, so it describes the
PREVIOUS turn. Empathy is needed on the turn the person is upset.
"""
from __future__ import annotations

import pytest

from app.sop.machine import decide, deterministic_intensity_floor
from app.sop.types import Phase, ScopeRing, SessionState, TurnSignals


def _signals(msg: str, **kw) -> TurnSignals:
    base = dict(scope=ScopeRing.CORE, turn_index=0, raw_message=msg)
    base.update(kw)
    return TurnSignals(**base)


@pytest.mark.parametrize("msg", [
    "I already told you who I am. This is ridiculous.",
    "I'm sick of repeating myself to you people.",
    "How many times do I have to explain this?",
    "This is a complete waste of my time.",
    "Honestly this is absurd and I'm very frustrated.",
])
def test_frustration_raises_the_floor_on_the_same_turn(msg):
    assert deterministic_intensity_floor(msg) >= 2


@pytest.mark.parametrize("msg", [
    "I can't afford to pay this out of pocket.",
    "My mother is very ill and I'm trying to sort this out for her.",
    "I'm desperate, I don't know what else to do.",
])
def test_distress_also_raises_the_floor(msg):
    assert deterministic_intensity_floor(msg) >= 2


@pytest.mark.parametrize("msg", [
    "Margaret Chen, DOB 1985-03-15, SSN last four 4472.",
    "Why was the claim denied?",
    "Yes, that's the one.",
    "Could you send me the summary by email?",
    "",
])
def test_ordinary_messages_do_not_trip_it(msg):
    """False positives cost only a warmer reply, but a floor that fires on
    everything would make the empathy directive meaningless."""
    assert deterministic_intensity_floor(msg) == 0


def test_it_is_a_floor_and_never_lowers_the_models_reading(domain, spec):
    """The model may see distress the markers miss. This must never overrule
    it downward — that is what makes a conservative marker list safe here."""
    state = SessionState(session_id="s", sop_name="insurance_claims", phase=Phase.VERIFY_ID)
    state, _ = decide(state, _signals("Why was it denied?", intensity=3), domain, spec)
    assert state.facts.last_intensity == 3


def test_empathy_directive_fires_on_the_upset_turn(domain, spec):
    """The end-to-end property: the directive is in force on the SAME turn the
    caller expresses frustration, not the one after."""
    state = SessionState(session_id="s", sop_name="insurance_claims", phase=Phase.VERIFY_ID)
    state, plan = decide(
        state, _signals("I already told you who I am. This is ridiculous."), domain, spec
    )
    assert "ACKNOWLEDGE_EMOTION" in [d.id for d in plan.directives]


def test_a_calm_turn_gets_no_empathy_directive(domain, spec):
    state = SessionState(session_id="s", sop_name="insurance_claims", phase=Phase.VERIFY_ID)
    state, plan = decide(state, _signals("Margaret Chen, DOB 1985-03-15."), domain, spec)
    assert "ACKNOWLEDGE_EMOTION" not in [d.id for d in plan.directives]
