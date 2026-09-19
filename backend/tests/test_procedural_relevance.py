"""The fifth guard rule: does the reply advance the procedure?

Found by the hostile-model run. Scope (R3) was enforced only at the INPUT
boundary — an off-topic caller message is classified by PERCEIVE and answered
with a deterministic template. Nothing checked the other direction, so an
off-topic REPLY to an on-topic question sailed through: a banana bread recipe
was delivered to callers seventeen times while every safety-critical rule
held. R3 was, on the output side, enforced entirely by the model's manners.
"""
from __future__ import annotations

import pytest

from app.guards.output_guard import check_procedural_relevance
from app.sop.types import Phase, TurnPlan


def _plan(**facts) -> TurnPlan:
    return TurnPlan(phase=Phase.PROCESS_CASE, allowed_tools=(), visible_facts=facts)


def _flagged(reply, plan=None) -> bool:
    return bool(check_procedural_relevance(reply, plan or _plan()))


# --------------------------------------------------------------------------
# It must catch the thing it was written for
# --------------------------------------------------------------------------

def test_catches_an_off_topic_answer_to_an_on_topic_question():
    assert _flagged(
        "Sure, here's a recipe for banana bread: cream the butter and sugar, "
        "then fold in three mashed bananas."
    )


def test_catches_generic_prose_with_no_connection_to_the_call():
    assert _flagged(
        "The history of the printing press begins in fifteenth century Mainz, "
        "where movable type transformed the distribution of written work."
    )


# --------------------------------------------------------------------------
# It must NOT catch legitimate replies. These four are real strings from
# recorded runs; two of them were false positives on the first version of the
# rule and are the reason `let me` and the vocative pattern exist.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("reply", [
    "Perfect. Thanks for calling in, Margaret — take care!",
    "I understand the wait is frustrating — let me try requesting it again now.",
    "I'm sorry to hear about your mother. That sounds like a difficult situation "
    "to be dealing with right now.",
    "Could you confirm your date of birth for me?",
    "That summary is on its way to your email now.",
])
def test_does_not_flag_real_agent_replies(reply):
    assert not _flagged(reply)


def test_short_acknowledgements_are_not_substantive_enough_to_judge():
    """A short reply is not where an off-topic essay hides, and flagging
    filler would only buy repair round trips on something harmless."""
    assert not _flagged("Of course, no problem at all.")


def test_a_grounded_factual_reply_passes_on_this_turns_facts():
    """The impersonal, question-free, purely factual reply is the one case
    that depends on visible_facts being populated — which resolve() always
    does for a disclosing phase. Worth stating as a test so the dependency is
    explicit rather than incidental."""
    reply = ("The record shows this claim was denied because the review file didn't "
             "include two items: a pathology report and the treating provider's office note.")
    plan = _plan(claim={"denial_reason": "the review file did not include the pathology "
                                         "report and the treating provider office note"})
    assert not _flagged(reply, plan)


def test_a_question_always_counts_as_advancing():
    assert not _flagged("Would tomorrow morning work better for that callback?")


def test_facts_vocabulary_rescues_a_terse_factual_reply():
    """A reply with no pronouns and no question, but which states something
    straight out of this turn's facts, is doing its job."""
    reply = "The pathology report and the treating provider office note remain outstanding."
    assert _flagged(reply)                                   # no anchor on its own
    plan = _plan(claim={"documents_needed": ["pathology report", "office note"]})
    assert not _flagged(reply, plan)                          # grounded in this turn's facts


def test_rule_is_not_a_topic_blocklist():
    """The rule must not care WHAT the subject is — only whether the reply is
    doing the job. A banana bread sentence addressed to the caller as part of
    genuine small talk is the model's business, not the guard's; an unbounded
    list of forbidden subjects would be out of date the day it shipped."""
    assert not _flagged(
        "I hope you get to enjoy your baking later — for now, could you send "
        "me the pathology report?"
    )
