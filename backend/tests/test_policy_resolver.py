"""Tests for the output half of DECIDE (DESIGN.md §7.4, Appendix A, policy.py).

These check the TurnPlan produced for a given (already-transitioned) state:
what the model may know, may do, and is required/forbidden to say. This is
where "different phases get different freedom" (R2) becomes something you
can assert on, not just describe.
"""
from app.sop.machine import decide
from app.sop.policy import ACKNOWLEDGE_EMOTION, resolve
from app.sop.types import (
    CallerRole,
    CaseHint,
    ConsentStatus,
    PendingAction,
    Phase,
    ScopeRing,
    SessionState,
    Tier,
    TurnSignals,
)

CLAIM_LEAK_KEYS = {"denial_reason", "documents_needed", "case_id", "allowed_max_amount", "net_pay"}


def make_state(**kwargs) -> SessionState:
    return SessionState(session_id="test", sop_name="insurance_claims", **kwargs)


def signals(**overrides) -> TurnSignals:
    base = dict(scope=ScopeRing.CORE, turn_index=0, raw_message="")
    base.update(overrides)
    return TurnSignals(**base)


def _flatten_keys(d: dict) -> set:
    keys = set()
    for k, v in d.items():
        keys.add(k)
        if isinstance(v, dict):
            keys |= _flatten_keys(v)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    keys |= _flatten_keys(item)
    return keys


class TestDisclosureScoping:
    def test_verify_id_visible_facts_never_leak_claim_data(self, domain, spec):
        """Structural safety guarantee (DESIGN.md §5.3): even under a
        partial-verification state, nothing from claims.json can appear."""
        state = make_state()
        state, plan = decide(state, signals(identity_candidates={"full_name": "Margaret Chen"}), domain, spec)
        assert plan.phase == Phase.VERIFY_ID
        leaked = _flatten_keys(plan.visible_facts) & CLAIM_LEAK_KEYS
        assert not leaked, f"claim fields leaked into VERIFY_ID visible_facts: {leaked}"
        assert "any claim fact" in plan.forbidden_elements

    def test_resolve_intent_visible_facts_are_index_only(self, domain, spec):
        state = make_state()
        msg = "denied healthcare claim from January"
        state, plan = decide(
            state,
            signals(
                identity_candidates={"full_name": "Margaret Chen", "dob": "1985-03-15", "id_last4": "4472"},
                case_hint=CaseHint(case_type="healthcare", status="denied", time_ref="January", verbatim_quote=msg),
                raw_message=msg,
            ),
            domain,
            spec,
        )
        assert plan.phase == Phase.RESOLVE_INTENT
        flat = _flatten_keys(plan.visible_facts)
        assert "denial_reason" not in flat
        assert "documents_needed" not in flat
        assert "allowed_max_amount" not in flat
        assert "case_id" in flat  # index fields ARE visible

    def test_process_case_visible_facts_include_full_claim_and_derived_fields(self, domain, spec):
        state = make_state()
        msg = "denied healthcare claim from January"
        state, plan = decide(
            state,
            signals(
                identity_candidates={"full_name": "Margaret Chen", "dob": "1985-03-15", "id_last4": "4472"},
                case_hint=CaseHint(case_type="healthcare", status="denied", time_ref="January", verbatim_quote=msg),
                raw_message=msg,
            ),
            domain,
            spec,
        )
        state, plan = decide(state, signals(confirms_proposed_case=True, turn_index=1), domain, spec)
        assert plan.phase == Phase.PROCESS_CASE
        claim = plan.visible_facts["claim"]
        assert claim["denial_reason"] is not None
        assert claim["unpaid_balance"] == "1450.00"
        assert claim["days_until_appeal_deadline"] == 26
        assert claim["appeal_deadline_passed"] is False
        assert "guidance" in plan.visible_facts


class TestFreedomLevel:
    def test_verify_id_is_strict_with_minimal_tools(self, domain, spec):
        state = make_state()
        state, plan = decide(state, signals(), domain, spec)
        assert plan.phase == Phase.VERIFY_ID
        assert "send_summary_email" not in plan.allowed_tools
        assert "get_case_detail" not in plan.allowed_tools

    def test_process_case_is_open_with_wide_tools(self, domain, spec):
        state = make_state()
        msg = "denied healthcare claim from January"
        state, plan = decide(
            state,
            signals(
                identity_candidates={"full_name": "Margaret Chen", "dob": "1985-03-15", "id_last4": "4472"},
                case_hint=CaseHint(case_type="healthcare", status="denied", time_ref="January", verbatim_quote=msg),
                raw_message=msg,
            ),
            domain,
            spec,
        )
        state, plan = decide(state, signals(confirms_proposed_case=True, turn_index=1), domain, spec)
        # get_case_detail/get_document_guidance are pseudo-tools simplified to
        # direct visible_facts injection (see sops/insurance_claims.yaml); the
        # tool actually registered here is the genuine side effect.
        assert "create_followup" in plan.allowed_tools
        assert "claim" in plan.visible_facts
        assert "guidance" in plan.visible_facts

    def test_transfer_withheld_from_the_model_on_the_first_verify_turn(self, domain, spec):
        """DESIGN.md §7.8 — "know when to stop persuading" implies persuading
        first. Structural, not a prompt rule: the model cannot call a tool it
        was never shown, so it has to try the ladder before it can transfer."""
        state = make_state()
        state, plan = decide(state, signals(), domain, spec)
        assert plan.phase == Phase.VERIFY_ID
        assert "transfer_to_human" not in plan.allowed_tools

    def test_transfer_becomes_available_once_the_conversation_has_had_a_chance(self, domain, spec):
        state = make_state()
        state, _ = decide(state, signals(), domain, spec)
        state, plan = decide(state, signals(turn_index=1), domain, spec)
        assert "transfer_to_human" in plan.allowed_tools

    def test_transfer_available_immediately_on_a_hard_signal(self, domain, spec):
        """A mismatch is a real signal something is wrong — the ladder
        requirement shouldn't trap a caller who can't verify."""
        state = make_state()
        state, plan = decide(state, signals(identity_candidates={"dob": "1999-01-01"}), domain, spec)
        assert state.facts.mismatch_count == 1
        assert "transfer_to_human" in plan.allowed_tools

    def test_explicit_caller_request_never_depends_on_tool_availability(self, domain, spec):
        """The caller-sovereignty path is deterministic and runs before ACT,
        so withholding the tool cannot strand a caller who asks for a human
        on turn one (DESIGN.md §7.4 precedence rule 1)."""
        state = make_state()
        state, plan = decide(state, signals(escalation_request=True), domain, spec)
        assert plan.phase == Phase.HUMAN_HANDOFF
        assert plan.route == "human_handoff"

    def test_transfer_available_in_later_phases(self, domain, spec):
        state = make_state(phase=Phase.PROCESS_CASE)
        state.facts.verified_party_id = "P9"
        state, plan = decide(state, signals(), domain, spec)
        assert "transfer_to_human" in plan.allowed_tools

    def test_terminal_phase_has_no_tools(self, domain, spec):
        state = make_state()
        state, plan = decide(state, signals(escalation_request=True), domain, spec)
        assert plan.phase == Phase.HUMAN_HANDOFF
        assert plan.allowed_tools == ()


class TestEmpathyContract:
    def test_upset_caller_prepends_acknowledge_never_replaces_task(self, domain, spec):
        """DESIGN.md §7.4/§7.8: empathy PREPENDS, never replaces the phase's
        own directive — the agent must stay warm AND keep moving."""
        state = make_state()
        state, plan = decide(state, signals(refusal=True, intensity=2), domain, spec)
        assert plan.directives[0].id == ACKNOWLEDGE_EMOTION.id
        directive_ids = [d.id for d in plan.directives]
        assert "VERIFY_RATIONALE" in directive_ids  # the phase's own task directive still present
        assert "an offered alternative identity factor" in plan.required_elements
        assert "how many factors remain" in plan.required_elements

    def test_calm_caller_gets_no_empathy_directive(self, domain, spec):
        state = make_state()
        state, plan = decide(state, signals(), domain, spec)
        directive_ids = [d.id for d in plan.directives]
        assert ACKNOWLEDGE_EMOTION.id not in directive_ids

    def test_worked_example_frustrated_caller_stays_gated(self, domain, spec):
        """The brief's bonus scenario: 'I already told you who I am. This is
        ridiculous. Just tell me why my claim was denied.' Must acknowledge,
        explain, offer alternatives — and NOT disclose or skip verification."""
        state = make_state()
        state, plan = decide(
            state,
            signals(
                refusal=True,
                intensity=2,
                identity_candidates={"full_name": "Margaret Chen"},  # only one factor offered
                raw_message="I already told you who I am. This is ridiculous. Just tell me why my claim was denied.",
            ),
            domain,
            spec,
        )
        assert plan.phase == Phase.VERIFY_ID  # not skipped
        assert "any claim fact" in plan.forbidden_elements
        assert plan.directives[0].id == ACKNOWLEDGE_EMOTION.id


class TestScopeRefusal:
    def test_out_of_scope_routes_to_refuse_with_templated_directive(self, domain, spec):
        state = make_state()
        state, plan = decide(state, signals(scope=ScopeRing.OUT), domain, spec)
        assert plan.route == "refuse"
        assert plan.phase == Phase.VERIFY_ID  # unchanged
        assert len(plan.directives) == 1
        assert plan.directives[0].id == "REFUSAL_TEMPLATE"
        assert plan.model_tier == Tier.FAST

    def test_refusal_template_is_always_one_of_the_spec_set(self, domain, spec):
        """While still under the strike threshold (route stays 'refuse'),
        the directive text must always be one of the spec's exact templates.
        Once strikes exceed the threshold the route correctly becomes
        'abuse_terminated' instead — covered separately in test_state_machine.py."""
        state = make_state()
        seen = set()
        for i in range(3):  # max_off_topic_strikes = 3; stay under it
            state.facts.off_topic_strikes = i
            _, plan = decide(state, signals(scope=ScopeRing.OUT, turn_index=i), domain, spec)
            assert plan.route == "refuse"
            seen.add(plan.directives[0].text)
        for text in seen:
            assert any(t in text for t in spec.refusal_templates)


class TestModelTierRouting:
    def test_verify_id_defaults_to_fast_tier(self, domain, spec):
        state = make_state()
        state, plan = decide(state, signals(), domain, spec)
        assert plan.model_tier == Tier.FAST

    def test_verify_id_escalates_to_strong_when_caller_is_upset(self, domain, spec):
        """DESIGN.md §7.4: quality of empathetic phrasing matters enough to
        justify the tier bump, even in an otherwise fast-tier phase."""
        state = make_state()
        state, plan = decide(state, signals(refusal=True, intensity=3), domain, spec)
        assert plan.model_tier == Tier.STRONG


class TestRepresentativeDisclosure:
    def test_representative_gets_scope_note_in_process_case(self, domain, spec):
        state = make_state()
        msg = "denied healthcare claim from January"
        state, plan = decide(
            state,
            signals(
                claims_representative=True,
                representative_name="David Chen",
                identity_candidates={"full_name": "Margaret Chen", "dob": "1985-03-15", "id_last4": "4472"},
                case_hint=CaseHint(case_type="healthcare", status="denied", time_ref="January", verbatim_quote=msg),
                raw_message=msg,
            ),
            domain,
            spec,
        )
        state, plan = decide(state, signals(confirms_proposed_case=True, turn_index=1), domain, spec)
        directive_ids = [d.id for d in plan.directives]
        assert "REPRESENTATIVE_SCOPE_NOTE" in directive_ids


def test_resolve_is_a_pure_function_of_state(domain, spec):
    """Calling resolve() twice on the same state must give the same plan —
    no hidden clock/random/global mutation (DESIGN.md Appendix A)."""
    state = make_state()
    state, _ = decide(state, signals(identity_candidates={"full_name": "Margaret Chen"}), domain, spec)
    plan_a = resolve(state, domain, spec)
    plan_b = resolve(state, domain, spec)
    assert plan_a == plan_b


class TestPostProcessCloseOut:
    """POST_PROCESS has exactly ONE entry: a wrap-up signal. Everything about
    the phase follows from that, including which branches can exist.

    These tests used to construct a POST_PROCESS state with
    `wrap_up_signalled` false, and asserted on directives for it. That state
    is unreachable — which is why the coverage report showed four directives
    that had never fired in any run. They were not untested, they were dead,
    and the tests were keeping them looking alive.
    """

    def _post_process_state(self, domain, spec, *, sent=False, skipped=False):
        state = make_state(phase=Phase.POST_PROCESS)
        state.facts.verified_party_id = "P9"
        state.memory.confirmed_case_id = "CL-2048"
        state.facts.wrap_up_signalled = True     # true by construction, see below
        state.facts.email_sent = sent
        state.facts.email_skipped = skipped
        return state

    def test_post_process_is_only_reachable_via_a_wrap_up_signal(self, domain, spec):
        """The invariant the phase's whole shape rests on. If another entry is
        ever added, four deleted branches become reachable again and this
        fails — which is the point of asserting it rather than assuming it."""
        state = make_state(phase=Phase.PROCESS_CASE)
        state.facts.verified_party_id = "P9"
        state.memory.confirmed_case_id = "CL-2048"

        stayed, _ = decide(state, signals(raw_message="what about the deadline?"), domain, spec)
        assert stayed.phase == Phase.PROCESS_CASE

        moved, _ = decide(state, signals(wrap_up_request=True, raw_message="that's all"), domain, spec)
        assert moved.phase == Phase.POST_PROCESS
        assert moved.facts.wrap_up_signalled is True

    def test_summary_is_still_offered_before_any_decision(self, domain, spec):
        state = self._post_process_state(domain, spec)
        state, plan = decide(state, signals(), domain, spec)
        assert "OFFER_SUMMARY" in [d.id for d in plan.directives]

    def test_approving_the_send_fires_send_and_close(self, domain, spec):
        """The regression that matters: the send directive must fire on the
        turn the caller agrees, not be left to the model's initiative."""
        state = self._post_process_state(domain, spec)
        state.facts.pending_action = PendingAction(
            action_type="send_summary_email", requested_at_turn=0
        )
        state, plan = decide(
            state,
            signals(consent_response=True, turn_index=state.next_turn_index(),
                    raw_message="yes please, send it"),
            domain, spec,
        )
        ids = [d.id for d in plan.directives]
        assert "SEND_AND_CLOSE" in ids
        assert "send_summary_email" in plan.allowed_tools
        # ...and it does not also ask whether they need anything else, because
        # they already said they did not.
        text = next(d for d in plan.directives if d.id == "SEND_AND_CLOSE").text.lower()
        assert "do not ask whether there's anything else" in text

    def test_the_offer_is_dropped_on_the_turn_the_caller_says_yes(self, domain, spec):
        """Otherwise the same turn carries "offer a summary" and "send it and
        close" — two contradictory instructions, resolved by whichever the
        model weighted more."""
        state = self._post_process_state(domain, spec)
        state.facts.pending_action = PendingAction(
            action_type="send_summary_email", requested_at_turn=0
        )
        _, plan = decide(
            state,
            signals(consent_response=True, turn_index=state.next_turn_index(),
                    raw_message="yes please"),
            domain, spec,
        )
        ids = [d.id for d in plan.directives]
        assert "SEND_AND_CLOSE" in ids
        assert "OFFER_SUMMARY" not in ids
        assert "CONSENT_REQUIRED" not in ids

    def test_phase_settles_to_closed_once_the_email_actually_sends(self, domain, spec):
        """settle_phase() re-checks the terminal guard after tool effects --
        the send happens during ACT, long after transition() ran."""
        from app.sop.machine import settle_phase

        state = self._post_process_state(domain, spec)
        assert state.phase == Phase.POST_PROCESS
        state.facts.email_sent = True
        settle_phase(state)
        assert state.phase == Phase.CLOSED

    def test_conversation_can_actually_reach_closed(self, domain, spec):
        state = self._post_process_state(domain, spec, sent=True)
        state, plan = decide(
            state, signals(wrap_up_request=True, raw_message="no that's all, thanks"), domain, spec
        )
        assert plan.phase == Phase.CLOSED


class TestPostProcessCanStillAnswer:
    """A caller who says "that's all, thanks" and then thinks of one more
    question must get a real answer.

    Found in a live run: asked for the appeal deadline right after wrapping
    up, the agent said it did not have the deadline in front of it. It had
    been reading that exact field one turn earlier — POST_PROCESS was
    assembling a strict subset of the case data, so the caller lost access by
    being polite. Entitlement had not changed; only the phase name had.
    """

    def _state(self):
        state = make_state(phase=Phase.POST_PROCESS)
        state.facts.verified_party_id = "P9"
        state.memory.confirmed_case_id = "CL-2048"
        state.facts.wrap_up_signalled = True
        return state

    def test_post_process_sees_the_same_case_data_as_process_case(self, domain, spec):
        wrap = self._state()
        _, post_plan = decide(wrap, signals(), domain, spec)

        working = make_state(phase=Phase.PROCESS_CASE)
        working.facts.verified_party_id = "P9"
        working.memory.confirmed_case_id = "CL-2048"
        _, process_plan = decide(working, signals(), domain, spec)

        post_claim = post_plan.visible_facts.get("claim") or {}
        process_claim = process_plan.visible_facts.get("claim") or {}
        assert post_claim == process_claim
        assert post_claim.get("appeal_deadline")          # the field that was missing

    def test_answers_there_are_still_grounded_and_non_committal(self, domain, spec):
        """More data means the same guards have to apply — otherwise this
        fix would trade a bad answer for an unguarded one."""
        _, plan = decide(self._state(), signals(), domain, spec)
        ids = [d.id for d in plan.directives]
        assert "GROUNDING_ONLY" in ids
        assert "NO_COMMITMENT" in ids

    def test_an_unconsented_representative_still_gets_reduced_scope(self, domain, spec):
        """The one entitlement difference that DOES survive into POST_PROCESS."""
        state = self._state()
        state.facts.caller_role = CallerRole.REPRESENTATIVE
        state.facts.consent_status = ConsentStatus.PENDING
        _, plan = decide(state, signals(), domain, spec)
        claim = plan.visible_facts.get("claim") or {}
        assert "denial_reason" not in claim
        assert plan.visible_facts.get("disclosure_note")


class TestConsentIsGrounded:
    """Regression for a fabricated-state bug: in the `timeout` scenario the
    agent told a representative that consent had been APPROVED when it never
    was. Root cause was that consent status wasn't in visible_facts at all —
    the model had nothing to check itself against, and the grounding guard
    had nothing to catch it with. A fact the agent is expected to state must
    be a fact the agent was given."""

    def _rep_in_process_case(self, domain, spec, status):
        from app.sop.types import CallerRole, ConsentStatus

        state = make_state(phase=Phase.PROCESS_CASE)
        state.facts.caller_role = CallerRole.REPRESENTATIVE
        state.facts.verified_party_id = "P9"
        state.facts.representative_of_party_id = "P9"
        state.memory.confirmed_case_id = "CL-2048"
        state.facts.consent_status = ConsentStatus(status)
        return state

    def test_pending_consent_is_visible_and_says_it_is_not_approved(self, domain, spec):
        state = self._rep_in_process_case(domain, spec, "pending")
        state, plan = decide(state, signals(), domain, spec)
        consent = plan.visible_facts["consent"]
        assert consent["status"] == "pending"
        assert "NOT been approved" in consent["meaning"]

    def test_timed_out_consent_is_visible_and_says_it_is_not_approved(self, domain, spec):
        state = self._rep_in_process_case(domain, spec, "timed_out")
        state, plan = decide(state, signals(), domain, spec)
        assert plan.visible_facts["consent"]["status"] == "timed_out"
        assert "NOT been approved" in plan.visible_facts["consent"]["meaning"]

    def test_approved_consent_unlocks_full_detail(self, domain, spec):
        state = self._rep_in_process_case(domain, spec, "approved")
        state, plan = decide(state, signals(), domain, spec)
        assert plan.visible_facts["consent"]["status"] == "approved"
        assert plan.visible_facts["claim"]["denial_reason"] is not None   # reduced scope lifted

    def test_pending_consent_still_withholds_the_narrative(self, domain, spec):
        state = self._rep_in_process_case(domain, spec, "pending")
        state, plan = decide(state, signals(), domain, spec)
        assert "denial_reason" not in plan.visible_facts["claim"]
        assert "allowed_max_amount" not in plan.visible_facts["claim"]

    def test_direct_policyholder_gets_no_consent_block(self, domain, spec):
        """Consent is a representative concept — showing it to a policyholder
        would be noise, and noise in visible_facts is something the model can
        misread."""
        state = make_state(phase=Phase.PROCESS_CASE)
        state.facts.verified_party_id = "P9"
        state.memory.confirmed_case_id = "CL-2048"
        state, plan = decide(state, signals(), domain, spec)
        assert "consent" not in plan.visible_facts
