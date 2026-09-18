"""Tests for the idle-budget policy layer.

These assert the SHAPE of the policy, not a specific number of seconds —
the numbers are meant to move once the instrumentation has data, and a test
that pins them would just have to be edited every time it did its job."""
from __future__ import annotations

import json

from app.obs import idle_metrics
from app.sop.spec import RESPONSE_EFFORT_SECONDS, load_spec
from app.sop.types import Phase

INSURANCE = "sops/insurance_claims.yaml"
BANK = "sops/bank_kyc.yaml"


def test_effort_levels_are_ordered():
    assert (
        RESPONSE_EFFORT_SECONDS["quick"]
        < RESPONSE_EFFORT_SECONDS["considered"]
        < RESPONSE_EFFORT_SECONDS["offline_task"]
    )


def test_every_phase_resolves_to_a_budget():
    for path in (INSURANCE, BANK):
        spec = load_spec(path)
        for phase, ps in spec.phases.items():
            assert ps.idle_base_seconds > 0, f"{path}:{phase}"


def test_unknown_effort_falls_back_rather_than_raising():
    # A typo in a SOP file must not take the service down; it degrades to the
    # middle setting, which is wrong but safe in both directions.
    spec = load_spec(INSURANCE)
    ps = spec.phase_spec(Phase.VERIFY_ID)
    broken = type(ps)(**{**ps.__dict__, "response_effort": "nonsense"})
    assert broken.idle_base_seconds == RESPONSE_EFFORT_SECONDS["considered"]


def test_document_gathering_waits_longer_than_a_yes_no():
    """The core claim of the design: the wait tracks what we asked the caller
    to DO. PROCESS_CASE sends people to find paperwork; POST_PROCESS asks a
    yes/no. If these two were ever equal the policy would be decorative."""
    spec = load_spec(INSURANCE)
    process = spec.phase_spec(Phase.PROCESS_CASE).idle_base_seconds
    post = spec.phase_spec(Phase.POST_PROCESS).idle_base_seconds
    verify = spec.phase_spec(Phase.VERIFY_ID).idle_base_seconds
    assert process > post
    assert process > verify


def test_effort_is_a_property_of_the_question_not_the_vertical():
    """Per-phase effort should come out the SAME across verticals — asking
    for a date of birth is not harder at a bank. An earlier version of this
    design claimed banks should be configured more patient overall; this test
    exists to stop that idea coming back in through the phase table."""
    ins, bank = load_spec(INSURANCE), load_spec(BANK)
    for phase in (Phase.VERIFY_ID, Phase.RESOLVE_INTENT, Phase.PROCESS_CASE, Phase.POST_PROCESS):
        assert (
            ins.phase_spec(phase).response_effort == bank.phase_spec(phase).response_effort
        ), f"{phase} differs between verticals — effort is about the question, not the industry"


def test_vertical_difference_is_a_security_ceiling_and_runs_the_other_way():
    """A verified banking session left open on an unattended screen is an
    exposure a claims-status chat is not, so the KYC SOP's hard ceiling is
    SHORTER — the opposite direction to 'banks are more patient'."""
    assert load_spec(BANK).max_session_idle_seconds < load_spec(INSURANCE).max_session_idle_seconds


def test_rates_are_none_not_zero_without_data():
    assert idle_metrics._rate(0, 0) is None
    assert idle_metrics._rate(0, 4) == 0.0


def test_report_counts_fast_replies_as_false_positives(tmp_path, monkeypatch):
    path = tmp_path / "client_events.jsonl"
    monkeypatch.setattr(idle_metrics, "EVENTS_PATH", path)
    rows = [
        {"session_id": "a", "event": "idle_nudge_shown", "payload": {"response_effort": "quick"}},
        # answered almost at once => we interrupted someone who was there
        {"session_id": "a", "event": "idle_nudge_recovered", "payload": {"ms_after_nudge": 1200}},
        {"session_id": "b", "event": "idle_nudge_shown", "payload": {"response_effort": "quick"}},
        # came back much later => the nudge was doing its job
        {"session_id": "b", "event": "idle_nudge_recovered", "payload": {"ms_after_nudge": 60_000}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows))

    rep = idle_metrics.build_report({"a": True, "b": True})
    assert rep["nudges_shown"] == 2
    assert rep["nudges_false_positive"] == 1
    assert rep["nudge_false_positive_rate"] == 0.5
    assert rep["per_effort"]["quick"]["false_positive_rate"] == 0.5


def test_abandoned_without_close_counts_only_quiet_sessions(tmp_path, monkeypatch):
    path = tmp_path / "client_events.jsonl"
    monkeypatch.setattr(idle_metrics, "EVENTS_PATH", path)
    path.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"session_id": "quiet_closed", "event": "idle_expired", "payload": {}},
                {"session_id": "quiet_open", "event": "idle_nudge_shown", "payload": {}},
            ]
        )
    )
    # `busy` never went quiet, so it must not appear in the denominator even
    # though it is an open session — otherwise every live conversation would
    # look like an abandonment.
    rep = idle_metrics.build_report(
        {"quiet_closed": True, "quiet_open": False, "busy": False}
    )
    assert rep["sessions_quiet"] == 2
    assert rep["sessions_abandoned_without_close"] == 1
    assert rep["abandoned_without_close_rate"] == 0.5


def test_unknown_event_names_are_dropped(tmp_path, monkeypatch):
    path = tmp_path / "client_events.jsonl"
    monkeypatch.setattr(idle_metrics, "EVENTS_PATH", path)
    idle_metrics.record_event("s", "definitely_not_a_real_event", {})
    assert not path.exists()


def test_torn_line_does_not_break_the_report(tmp_path, monkeypatch):
    path = tmp_path / "client_events.jsonl"
    monkeypatch.setattr(idle_metrics, "EVENTS_PATH", path)
    path.write_text(
        json.dumps({"session_id": "a", "event": "idle_nudge_shown", "payload": {}})
        + "\n{\"session_id\": \"b\", \"eve"
    )
    assert idle_metrics.build_report({})["nudges_shown"] == 1
