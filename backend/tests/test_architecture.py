"""The static architectural claims, run as ordinary unit tests.

evals/architecture.py exists because every namespace-drift bug in this
project had the same shape: two hand-maintained tables describing one
concept, with a silent `.get()` between them. A value renamed on one side
produced no error and no test failure — just an enforcement that quietly
stopped happening.

These run in milliseconds with no model, so there is no reason for them to
live only in the eval pipeline.
"""
from __future__ import annotations

import pytest

from evals.architecture import ALL_CHECKS, analyse


@pytest.mark.parametrize("check", ALL_CHECKS, ids=lambda c: c.__name__)
def test_static_claim_holds(check):
    claim = check()
    assert claim.ok, f"{claim.claim_id}: {claim.failures}"


def test_the_whole_architecture_is_coherent():
    assert analyse()["summary"]["coherent"]


def test_an_unknown_contract_element_is_reported_not_swallowed():
    """The specific regression: check_contract() used to skip elements it did
    not recognise, so 11 of 20 declared elements were never checked at all."""
    from app.guards.output_guard import check_contract
    from app.sop.types import Phase, TurnPlan

    plan = TurnPlan(
        phase=Phase.PROCESS_CASE, allowed_tools=(),
        forbidden_elements=("a element nobody defined",),
    )
    blocking, missing, unverified = check_contract("some reply text", plan)
    assert not blocking and not missing
    assert any("not in the guard's contract vocabulary" in u for u in unverified)


def test_an_uncheckable_element_admits_it():
    """An element with no deterministic test is reported as unverified rather
    than passing silently — the difference between 'we checked' and 'we
    cannot check' has to survive into the trace."""
    from app.guards.output_guard import check_contract
    from app.sop.types import Phase, TurnPlan

    plan = TurnPlan(
        phase=Phase.RESOLVE_INTENT, allowed_tools=(),
        required_elements=("a restatement of the candidate claim (type, status, rough date)",),
    )
    _, missing, unverified = check_contract("Is it the January one?", plan)
    assert not missing
    assert any("not deterministically checkable" in u for u in unverified)


def test_a_foreign_email_address_is_now_blocked():
    """R8's send-to-file-address-only rule had no enforcement at all: the
    directive asked the model not to, and that was the whole mechanism."""
    from app.guards.output_guard import check_contract
    from app.sop.types import Phase, TurnPlan

    plan = TurnPlan(
        phase=Phase.POST_PROCESS, allowed_tools=(),
        visible_facts={"file_email": "margaret@email.com"},
        forbidden_elements=("sending to any email address other than the one on file",),
    )
    ok, _, _ = check_contract("I'll send that to margaret@email.com now.", plan)
    assert ok == []
    bad, _, _ = check_contract("Sure, I'll send it to someone.else@gmail.com instead.", plan)
    assert bad
