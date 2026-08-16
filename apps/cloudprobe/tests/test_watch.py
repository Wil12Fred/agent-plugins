"""Deploy watching: the floor rule and the control rule.

Both came out of `specs/OPER-803/monitor-despliegue.py`, and both exist because
the obvious version of the check produces alerts nobody believes. Each test
names the rule it enforces.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from opscore.errors import ValidationError

from cloudprobe import watch

# --------------------------------------------------------------------------- #
# Parsing — a bad argument is a message, not a traceback
# --------------------------------------------------------------------------- #


def test_a_merge_request_is_project_then_iid() -> None:
    """Rule: `project!iid`, because that is how GitLab spells an MR."""
    mr = watch.MergeRequest.parse("myorg/auth-service!327")

    assert mr.project == "myorg/auth-service"
    assert mr.iid == 327
    assert str(mr) == "myorg/auth-service!327"


@pytest.mark.parametrize("raw", ["no-separator", "!327", "project!abc", "project!"])
def test_a_malformed_merge_request_is_rejected_with_an_example(raw: str) -> None:
    """Rule: reject it here, not with a 404 twenty seconds later."""
    with pytest.raises(ValidationError):
        watch.MergeRequest.parse(raw)


def test_a_baseline_is_name_equals_count() -> None:
    parsed = watch.parse_baseline(["GLOBAL.ERROR_USER_DELETED=0", "GLOBAL.ERROR_LOGIN_STATUS=4"])
    assert parsed == {"GLOBAL.ERROR_USER_DELETED": 0, "GLOBAL.ERROR_LOGIN_STATUS": 4}


@pytest.mark.parametrize("raw", ["NAME", "=4", "NAME=four", "NAME="])
def test_a_malformed_baseline_is_rejected(raw: str) -> None:
    with pytest.raises(ValidationError):
        watch.parse_baseline([raw])


# --------------------------------------------------------------------------- #
# The floor rule
# --------------------------------------------------------------------------- #


def test_volume_below_the_floor_alerts() -> None:
    """Rule: crossing the 14-day minimum is something that has not happened in a fortnight."""
    reading = watch.below_floor(3, 10, label="reservations")
    assert reading.alert
    assert "below" in reading.detail


def test_volume_at_the_floor_does_not_alert() -> None:
    """The boundary: equal to the minimum has happened before, so it is not news."""
    assert not watch.below_floor(10, 10, label="reservations").alert


def test_an_unknown_floor_never_alerts() -> None:
    """Rule: floor 0 means no history, and 'no history' must not read as 'everything is low'.

    Without this, a fresh environment or a new table alerts on its first run and
    keeps alerting, which is how a monitor gets muted for good.
    """
    reading = watch.below_floor(0, 0, label="reservations")
    assert not reading.alert
    assert reading.threshold is None


def test_the_floor_query_closes_both_windows_on_the_hour() -> None:
    """Rule: compare a whole hour with whole hours.

    Counting the *current* hour against a floor built from complete hours
    compares a partial number with full ones — it reads as a collapse every time
    the monitor runs at ten past.
    """
    sql = watch.floor_query("membership_lesson")

    assert "DATE_FORMAT(NOW(),'%Y-%m-%d %H:00:00')" in sql
    assert "INTERVAL 14 DAY" in sql
    assert "MIN(n)" in sql, "the floor is a minimum, not an average"
    assert "AVG(" not in sql


# --------------------------------------------------------------------------- #
# The control rule — the reason this probe is not a threshold
# --------------------------------------------------------------------------- #


def test_a_degraded_subject_with_a_degraded_control_does_not_alert() -> None:
    """Rule: when both platforms move, it is the backend, not the release.

    This is the whole point of the probe. A bare threshold fires here and sends
    somebody to read a release diff during an API incident.
    """
    subject = watch.EdgeRate(requests=200, error_pct=20.0, server_errors=0)
    control = watch.EdgeRate(requests=2000, error_pct=18.0, server_errors=0)

    assert not watch.against_control(subject, control, alert_pct=12.0).alert


def test_a_degraded_subject_with_a_flat_control_alerts() -> None:
    """Rule: one platform moving alone is the release."""
    subject = watch.EdgeRate(requests=200, error_pct=20.0, server_errors=0)
    control = watch.EdgeRate(requests=2000, error_pct=2.5, server_errors=0)

    reading = watch.against_control(subject, control, alert_pct=12.0)
    assert reading.alert
    assert "control does not" in reading.detail


def test_any_server_error_alerts_regardless_of_the_rate() -> None:
    """Rule: a 5xx is not a rate. One is a defect even at 0.1%."""
    subject = watch.EdgeRate(requests=5000, error_pct=0.1, server_errors=1)
    control = watch.EdgeRate(requests=5000, error_pct=0.1, server_errors=0)

    assert watch.against_control(subject, control, alert_pct=12.0).alert


def test_no_traffic_is_reported_as_not_measured_not_as_healthy() -> None:
    """Rule: a release nobody used is not a release that worked.

    `edge_rate` returns None rather than 0.0% so this stays distinguishable —
    the empty-result-read-as-a-negative-answer failure.
    """
    reading = watch.against_control(None, None, alert_pct=12.0)
    assert not reading.alert
    assert "not measured" in reading.detail


def test_edge_rate_returns_none_for_an_empty_window() -> None:
    assert watch.edge_rate([]) is None


def test_edge_rate_counts_4xx_as_errors_and_5xx_separately() -> None:
    rows = [{"st": "200", "n": "90"}, {"st": "404", "n": "8"}, {"st": "503", "n": "2"}]

    rate = watch.edge_rate(rows)

    assert rate is not None
    assert rate.requests == 100
    assert rate.error_pct == pytest.approx(10.0)
    assert rate.server_errors == 2


def test_a_subject_over_threshold_without_a_control_still_alerts() -> None:
    """Rule: no control is a weaker signal, not a licence to stay silent."""
    subject = watch.EdgeRate(requests=200, error_pct=20.0, server_errors=0)

    reading = watch.against_control(subject, None, alert_pct=12.0)
    assert reading.alert
    assert "no control" in reading.detail


# --------------------------------------------------------------------------- #
# The window
# --------------------------------------------------------------------------- #


def test_the_window_is_the_last_fully_closed_hour() -> None:
    """Rule: the edge lags, so the window ends an hour back — never at now."""
    now = datetime(2026, 8, 16, 14, 37, 12, tzinfo=UTC)

    start, end = watch.closed_window(now=now)

    assert start == "2026-08-16T12:00:00Z"
    assert end == "2026-08-16T13:00:00Z"


def test_the_baseline_probe_alerts_on_the_first_occurrence_when_the_baseline_is_zero() -> None:
    """Rule: a code that has never fired makes its first occurrence unambiguous."""
    assert watch.over_baseline("GLOBAL.ERROR_USER_DELETED", 1, 0).alert
    assert not watch.over_baseline("GLOBAL.ERROR_USER_DELETED", 0, 0).alert


def test_a_code_expected_to_rise_does_not_alert_at_its_baseline() -> None:
    """OPER-803's case: LOGIN_STATUS rising was the fix working, not a regression."""
    assert not watch.over_baseline("GLOBAL.ERROR_LOGIN_STATUS", 4, 4).alert
    assert watch.over_baseline("GLOBAL.ERROR_LOGIN_STATUS", 5, 4).alert
