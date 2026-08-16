"""Probe-failure classification and the rate arithmetic behind the incident figures."""

from __future__ import annotations

import pytest

from cloudprobe import gke
from cloudprobe.gke import Counts, ProbeFailure, Window, classify, percent_change


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Readiness probe failed: context deadline exceeded", ProbeFailure.TIMEOUT),
        (
            "Liveness probe failed: dial tcp 10.0.0.1:8080: connect: connection refused",
            ProbeFailure.REFUSED,
        ),
        ("Readiness probe failed: HTTP probe failed with statuscode: 500", ProbeFailure.OTHER),
    ],
)
def test_the_two_failure_modes_are_told_apart(message: str, expected: ProbeFailure) -> None:
    # A raw Unhealthy count conflates CPU starvation with a dead pod; only the
    # message distinguishes them.
    assert classify(message) is expected


def test_window_length_is_what_makes_counts_comparable() -> None:
    assert Window("2026-06-23T13:00:00Z", "2026-06-23T16:00:00Z").minutes == 180
    assert Window("2026-06-23T16:18:00Z", "2026-06-23T16:32:00Z").minutes == 14


def test_the_543_percent_figure_reproduces() -> None:
    # The authorizer timeout figure from the 2026-06-23 incident write-up:
    # baseline 6 events / 180 min, incident 3 / 14 min.
    baseline = Window("2026-06-23T13:00:00Z", "2026-06-23T16:00:00Z")
    incident = Window("2026-06-23T16:18:00Z", "2026-06-23T16:32:00Z")

    base = Counts(timeout=6)
    inc = Counts(timeout=3)
    change = percent_change(
        base.rate(ProbeFailure.TIMEOUT, baseline), inc.rate(ProbeFailure.TIMEOUT, incident)
    )
    assert change is not None
    assert round(change) == 543


def test_a_zero_baseline_yields_no_percentage_rather_than_infinity() -> None:
    # Going from "never" to "sometimes" is not a percentage; reporting one
    # would be inventing a number.
    assert percent_change(0.0, 0.5) is None


def test_counts_accumulate_per_failure_mode() -> None:
    counts = Counts()
    counts.add(ProbeFailure.TIMEOUT)
    counts.add(ProbeFailure.TIMEOUT)
    counts.add(ProbeFailure.REFUSED)
    assert (counts.timeout, counts.refused, counts.other) == (2, 1, 0)


def test_the_markers_match_what_kubernetes_actually_emits() -> None:
    assert gke.TIMEOUT_MARKER == "context deadline exceeded"
    assert gke.REFUSED_MARKER == "connection refused"
