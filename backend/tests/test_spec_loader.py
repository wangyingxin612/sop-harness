from app.sop.types import Phase, StreamPolicy, Tier


def test_all_required_phases_present(spec):
    for phase in [
        Phase.VERIFY_ID,
        Phase.RESOLVE_INTENT,
        Phase.PROCESS_CASE,
        Phase.POST_PROCESS,
        Phase.HUMAN_HANDOFF,
        Phase.ABUSE_TERMINATED,
        Phase.CLOSED,
    ]:
        assert phase in spec.phases


def test_verify_id_is_strict_and_buffered(spec):
    p = spec.phase_spec(Phase.VERIFY_ID)
    assert p.freedom == "STRICT"
    assert p.stream == StreamPolicy.BUFFERED
    assert p.model_tier == Tier.FAST


def test_process_case_is_open_and_sentence_gated(spec):
    p = spec.phase_spec(Phase.PROCESS_CASE)
    assert p.freedom == "OPEN"
    assert p.stream == StreamPolicy.SENTENCE_GATED
    assert "get_case_detail" in p.tools


def test_identity_config_matches_matcher_constants(spec):
    assert spec.min_distinct_factors == 3
    assert spec.max_mismatches == 2
    assert set(spec.identity_factor_types) == {"full_name", "dob", "phone", "email", "id_last4"}


def test_directive_texts_cover_all_referenced_ids(spec):
    referenced = set()
    for phase_spec in spec.phases.values():
        referenced |= set(phase_spec.base_directives)
    missing = referenced - set(spec.directive_texts.keys())
    assert not missing, f"phase references directive ids with no text: {missing}"


def test_refusal_templates_nonempty(spec):
    assert len(spec.refusal_templates) >= 3


def test_always_available_tools_includes_transfer(spec):
    assert "transfer_to_human" in spec.always_available_tools
