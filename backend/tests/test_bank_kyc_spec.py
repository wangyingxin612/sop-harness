"""Tests for the spec layer's genericity (DESIGN.md §6, §9.2 P2).

`bank_kyc.yaml` loads through the exact same `load_spec()` as
`insurance_claims.yaml`, with no vertical-specific branches in the loader,
and produces structurally valid but *differently configured* behavior
(different freedom levels, tool lists, escalation thresholds, directive
text). That is the falsifiable claim this file checks: the spec.py/machine.py
CONTROL layer is vertical-agnostic. It does not claim the full runtime is —
see bank_kyc.yaml's own header comment and DESIGN.md §9.2 for the honestly-
scoped boundary (the DOMAIN ADAPTER — claim/application data — is not
pluggable yet).
"""
from pathlib import Path

import pytest

from app.sop.spec import load_spec
from app.sop.types import Phase, StreamPolicy, Tier

SOPS = Path(__file__).resolve().parents[1] / "sops"


@pytest.fixture(scope="module")
def bank_spec():
    return load_spec(SOPS / "bank_kyc.yaml")


def test_bank_spec_loads_through_the_same_loader_with_no_special_casing():
    """If spec.py needed a single `if sop_name == "insurance_claims"`
    branch, this test would still pass but the claim would be false — so
    the real check is structural (below), not just "it didn't crash"."""
    spec = load_spec(SOPS / "bank_kyc.yaml")
    assert spec.name == "bank_kyc"


def test_bank_spec_has_all_required_phases(bank_spec):
    for phase in [Phase.VERIFY_ID, Phase.RESOLVE_INTENT, Phase.PROCESS_CASE, Phase.POST_PROCESS]:
        assert phase in bank_spec.phases


def test_bank_spec_verify_id_is_strict_like_insurance(bank_spec):
    """Freedom level is a per-phase SPEC property (§6), not something
    hardcoded per vertical — both SOPs independently choose STRICT here."""
    assert bank_spec.phase_spec(Phase.VERIFY_ID).freedom == "STRICT"
    assert bank_spec.phase_spec(Phase.VERIFY_ID).stream == StreamPolicy.BUFFERED


def test_bank_spec_has_different_escalation_thresholds_than_insurance(spec, bank_spec):
    """Different verticals choosing different thresholds through the same
    field — proof the config varies, not the code."""
    assert spec.escalation.max_off_topic_strikes == 3
    assert bank_spec.escalation.max_off_topic_strikes == 2
    assert spec.escalation.max_off_topic_strikes != bank_spec.escalation.max_off_topic_strikes


def test_bank_spec_has_its_own_directive_vocabulary(bank_spec):
    assert "KYC_DISCLOSURE_LIMITS" in bank_spec.directive_texts
    assert "know-your-customer" in bank_spec.directive_texts["VERIFY_RATIONALE"].lower()


def test_bank_spec_refusal_templates_are_vertical_specific(spec, bank_spec):
    assert "insurance" in " ".join(spec.refusal_templates).lower()
    assert "account" in " ".join(bank_spec.refusal_templates).lower()
    assert set(spec.refusal_templates).isdisjoint(set(bank_spec.refusal_templates))


def test_bank_spec_directive_texts_cover_all_referenced_ids(bank_spec):
    """Same structural-integrity check as test_spec_loader.py's insurance
    version — proves the validation itself isn't insurance-specific either."""
    referenced = set()
    for phase_spec in bank_spec.phases.values():
        referenced |= set(phase_spec.base_directives)
    missing = referenced - set(bank_spec.directive_texts.keys())
    assert not missing, f"phase references directive ids with no text: {missing}"


def test_spec_loader_module_has_no_domain_import():
    """Static check backing the architectural claim: app/sop/spec.py must
    never import app.sop.domain (the insurance-specific claims/policyholder
    loader) — if it did, the loader itself would be insurance-coupled.
    Checked against actual import statements, not a bare substring scan —
    the module's own docstring legitimately *mentions*
    "insurance_claims.yaml" as an example, which isn't coupling."""
    import ast

    import app.sop.spec as spec_module

    source = Path(spec_module.__file__).read_text()
    tree = ast.parse(source)
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert not any(m.startswith("app.sop.domain") for m in imported_modules)
    assert not any(m.startswith("app.identity") for m in imported_modules)
