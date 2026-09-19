"""Tests for the output guard (DESIGN.md §7.9) — entirely LLM-free, exactly
like the policy layer. We hand-craft candidate replies the way a model might
produce them (good and bad) and assert the guard's verdict.
"""
import pytest

from app.guards.output_guard import check_commitment, check_contract, check_disclosure, check_grounding
from app.sop.types import Phase, TurnPlan


def make_plan(**overrides) -> TurnPlan:
    base = dict(phase=Phase.VERIFY_ID, allowed_tools=(), visible_facts={}, required_elements=(), forbidden_elements=())
    base.update(overrides)
    return TurnPlan(**base)


class TestDisclosureGuard:
    def test_flags_unauthorized_dob_in_verify_id(self, domain):
        plan = make_plan(visible_facts={})
        reply = "I can confirm your date of birth is 1985-03-15."
        violations = check_disclosure(reply, domain, plan)
        assert violations

    def test_flags_unauthorized_denial_reason(self, domain):
        plan = make_plan(phase=Phase.VERIFY_ID, visible_facts={})
        reply = "Your claim was denied because the review file did not include the pathology report and the treating provider office note."
        violations = check_disclosure(reply, domain, plan)
        assert violations

    def test_no_violation_when_value_is_actually_visible(self, domain):
        plan = make_plan(
            phase=Phase.PROCESS_CASE,
            visible_facts={"claim": {"case_id": "CL-2048", "denial_reason": "the review file did not include the pathology report and the treating provider office note"}},
        )
        reply = "Your claim CL-2048 was denied because the review file did not include the pathology report and the treating provider office note."
        violations = check_disclosure(reply, domain, plan)
        assert violations == []

    def test_flags_reformatted_ssn_digits(self, domain):
        """DESIGN.md §7.11 ASR-noise note: a reformatted (spoken-style) leak
        should still be caught even if it doesn't literally match the fixture string."""
        plan = make_plan(visible_facts={})
        reply = "Just to confirm, that's four four seven two on file."
        violations = check_disclosure(reply, domain, plan)
        assert any("4472" in v for v in violations)

    def test_does_not_flag_trivial_zero_amount(self, domain):
        plan = make_plan(visible_facts={})
        reply = "There's nothing else I can share right now."
        assert check_disclosure(reply, domain, plan) == []


class TestGroundingGuard:
    def test_flags_ungrounded_amount(self, domain):
        plan = make_plan(phase=Phase.PROCESS_CASE, visible_facts={"claim": {"allowed_max_amount": "1450.00"}})
        reply = "The record shows you're entitled to $2,000."
        violations = check_grounding(reply, plan)
        assert violations

    def test_allows_grounded_amount(self, domain):
        plan = make_plan(phase=Phase.PROCESS_CASE, visible_facts={"claim": {"allowed_max_amount": "1450.00"}})
        reply = "On file, the allowed maximum is $1,450.00."
        assert check_grounding(reply, plan) == []

    def test_allows_grounded_duration(self, domain):
        plan = make_plan(phase=Phase.PROCESS_CASE, visible_facts={"claim": {"days_until_appeal_deadline": 26}})
        reply = "On file, you have 26 days left to appeal."
        assert check_grounding(reply, plan) == []

    def test_flags_ungrounded_duration(self, domain):
        plan = make_plan(phase=Phase.PROCESS_CASE, visible_facts={"claim": {"days_until_appeal_deadline": 26}})
        reply = "On file, you have 40 days left to appeal."
        assert check_grounding(reply, plan)

    def test_allows_grounded_iso_date(self, domain):
        plan = make_plan(phase=Phase.PROCESS_CASE, visible_facts={"claim": {"appeal_deadline": "2026-03-18"}})
        reply = "On file, the appeal deadline is 2026-03-18."
        assert check_grounding(reply, plan) == []


class TestCommitmentGuard:
    def test_flags_promissory_language(self):
        reply = "You will receive $1,450 once this is processed."
        violations = check_commitment(reply)
        assert any("promissory" in v for v in violations)

    def test_flags_unattributed_sensitive_figure(self):
        reply = "Your appeal deadline is 2026-03-18."
        violations = check_commitment(reply)
        assert any("unattributed" in v for v in violations)

    def test_allows_attributed_figure(self):
        reply = "On file, the appeal deadline is 2026-03-18."
        assert check_commitment(reply) == []

    def test_allows_attribution_in_preceding_sentence(self):
        reply = "Let me check the record for you. It shows the allowed maximum is $1,450.00."
        assert check_commitment(reply) == []

    def test_allows_reply_with_no_sensitive_numbers(self):
        reply = "I can help you with that once I verify a couple more details."
        assert check_commitment(reply) == []

    # --- "get": the generic-verb case, pinned in BOTH directions.
    #
    # History worth keeping in the test file itself: a live false positive
    # ("I'll need to get her consent") was first fixed by deleting `get`
    # from the verb list — which silently introduced a false NEGATIVE on
    # number-free promises like "you will get the full amount" (L1's
    # attribution check can't catch those; there's no figure to attribute).
    # The correct fix constrains `get`'s OBJECT rather than removing the
    # verb. These two test groups exist so neither direction can regress
    # again without a test going red.

    @pytest.mark.parametrize(
        "reply",
        [
            "You'll get $1,450 once this is processed.",
            "You will get the full amount.",
            "You'll get paid next week.",
            "We will get you your refund.",
            "We'll pay you $1,450.",
            "I guarantee you will be reimbursed.",
            "You will receive the payment.",
        ],
    )
    def test_flags_promises_including_generic_get_with_payment_object(self, reply):
        assert check_commitment(reply), f"should have been flagged: {reply!r}"

    @pytest.mark.parametrize(
        "reply",
        [
            "I will get her consent before we discuss details.",
            "I'll get that noted on the file for you.",
            "We'll get a representative on the line.",
            "I'll need to get the pathology report from your provider.",
        ],
    )
    def test_does_not_flag_get_with_a_non_payment_object(self, reply):
        assert check_commitment(reply) == [], f"false positive on: {reply!r}"

    def test_worked_example_no_commitment_and_no_disclosure(self, domain):
        """The brief's frustrated-caller example: the reply must not disclose
        or promise anything (no visible_facts exist pre-verification)."""
        plan = make_plan(phase=Phase.VERIFY_ID, visible_facts={})
        reply = (
            "I hear you, and I know repeating yourself is frustrating. I can't pull up claim details "
            "until I verify a couple more things — that protects your information. Could you give me "
            "your date of birth, or the last four digits of your SSN or national ID?"
        )
        assert check_disclosure(reply, domain, plan) == []
        assert check_commitment(reply) == []


class TestContractGuard:
    def test_forbidden_case_id_blocks(self):
        plan = make_plan(forbidden_elements=("any case id",))
        blocking, missing, _unver = check_contract("Your claim CL-2048 was denied.", plan)
        assert blocking

    def test_forbidden_amount_blocks(self):
        plan = make_plan(forbidden_elements=("any dollar amount",))
        blocking, missing, _unver = check_contract("The allowed max is $1,450.00.", plan)
        assert blocking

    # --- farewell / premature-close: was a prompt rule, now a guard rule.
    # "Skipped a required SOP step" is exactly the class of failure that
    # belongs in code rather than in prose (DESIGN.md §4.1). Live-observed:
    # the model said a warm goodbye from inside PROCESS_CASE and never
    # reached the mandatory POST_PROCESS summary offer (R6).

    @pytest.mark.parametrize(
        "reply",
        [
            "You're welcome, Margaret. Have a great day!",
            "Glad I could help — take care.",
            "Thanks for calling, and goodbye.",
            "That's everything then. Bye now.",
        ],
    )
    def test_farewell_blocked_when_phase_forbids_closing(self, reply):
        plan = make_plan(phase=Phase.PROCESS_CASE, forbidden_elements=("a farewell or sign-off",))
        blocking, _, _unver = check_contract(reply, plan)
        assert blocking, f"should have blocked a premature sign-off: {reply!r}"

    @pytest.mark.parametrize(
        "reply",
        [
            "Is there anything else about this claim I can help with?",
            "Once those are submitted, the claim goes back into review.",
            "Thanks for confirming that — let me pull it up.",
        ],
    )
    def test_ordinary_replies_are_not_mistaken_for_a_sign_off(self, reply):
        plan = make_plan(phase=Phase.PROCESS_CASE, forbidden_elements=("a farewell or sign-off",))
        blocking, _, _unver = check_contract(reply, plan)
        assert blocking == [], f"false positive on: {reply!r}"

    @pytest.mark.parametrize(
        "reply",
        [
            # Live regression: "thanks for calling" is a contact-centre
            # GREETING, and a sign-off is positionally final — both
            # constraints matter, and both were learned the hard way.
            "Hi David, thanks for calling about your mother's claim. Let me pull that up.",
            "Thank you for calling — I can help with that. What's your date of birth?",
            "Take care of those documents and send them when you can — what else can I check?",
        ],
    )
    def test_greetings_and_mid_reply_phrasing_are_not_sign_offs(self, reply):
        plan = make_plan(phase=Phase.PROCESS_CASE, forbidden_elements=("a farewell or sign-off",))
        blocking, _, _unver = check_contract(reply, plan)
        assert blocking == [], f"false positive on: {reply!r}"

    def test_farewell_allowed_where_the_phase_permits_closing(self):
        """POST_PROCESS/CLOSED don't carry the forbidden element, so the same
        sentence is fine there — the rule is phase-scoped, not global."""
        plan = make_plan(phase=Phase.POST_PROCESS, forbidden_elements=())
        blocking, _, _unver = check_contract("Thanks for calling, Margaret. Take care!", plan)
        assert blocking == []

    def test_no_forbidden_hit_when_absent(self):
        plan = make_plan(forbidden_elements=("any case id", "any dollar amount"))
        blocking, missing, _unver = check_contract("I can help you with that.", plan)
        assert blocking == []

    def test_missing_required_element_is_reported_not_blocking(self):
        plan = make_plan(required_elements=("an offered alternative identity factor",))
        blocking, missing, _unver = check_contract("I still need more information to verify you.", plan)
        assert blocking == []  # required-element misses never block (§7.11 append-only)
        assert "an offered alternative identity factor" in missing

    def test_present_required_element_not_reported_missing(self):
        plan = make_plan(required_elements=("an offered alternative identity factor",))
        blocking, missing, _unver = check_contract("Could you give me your date of birth instead?", plan)
        assert missing == []
