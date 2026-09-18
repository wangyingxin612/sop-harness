"""Tests for the output half of DECIDE (DESIGN.md §7.4, Appendix A, policy.py).

These check the TurnPlan produced for a given (already-transitioned) state:
what the model may know, may do, and is required/forbidden to say. This is
where "different phases get different freedom" (R2) becomes something you
can assert on, not just describe.
"""
from app.sop.machine import decide
from app.sop.policy import ACKNOWLEDGE_EMOTION, resolve
from app.sop.types import (
    CaseHint,
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
