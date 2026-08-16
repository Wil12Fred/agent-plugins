"""Per-minute metric collapse: max is what shows saturation."""

from __future__ import annotations

from cloudprobe import metrics
from cloudprobe.metrics import summarise


def series(*values_per_minute: tuple[str, list[float]]) -> list[dict[str, object]]:
    """Build one Monitoring series per value, sharing timestamps."""
    count = max(len(v) for _, v in values_per_minute)
    return [
        {
            "points": [
                {"interval": {"endTime": ts}, "value": {"doubleValue": values[i]}}
                for ts, values in values_per_minute
                if i < len(values)
            ]
        }
        for i in range(count)
    ]


def test_max_is_reported_because_an_average_hides_the_saturated_node() -> None:
    raw = [
        {
            "points": [
                {"interval": {"endTime": "2026-06-23T16:23:00Z"}, "value": {"doubleValue": 0.945}}
            ]
        },
        *[
            {
                "points": [
                    {
                        "interval": {"endTime": "2026-06-23T16:23:00Z"},
                        "value": {"doubleValue": 0.20},
                    }
                ]
            }
            for _ in range(9)
        ],
    ]
    sample = summarise(raw)[0]
    assert sample.maximum == 0.945
    # The average across ten nodes would read as healthy.
    assert sample.average < 0.3


def test_hot_counts_individual_series_over_the_threshold() -> None:
    raw = [
        {"points": [{"interval": {"endTime": "T"}, "value": {"doubleValue": v}}]}
        for v in (0.95, 0.90, 0.10)
    ]
    sample = summarise(raw, hot_threshold=0.85)[0]
    assert sample.hot_series == 2
    assert sample.series_count == 3


def test_samples_come_back_in_time_order() -> None:
    raw = [
        {
            "points": [
                {"interval": {"endTime": "2026-06-23T16:25:00Z"}, "value": {"doubleValue": 0.5}},
                {"interval": {"endTime": "2026-06-23T16:23:00Z"}, "value": {"doubleValue": 0.9}},
            ]
        }
    ]
    assert [s.timestamp for s in summarise(raw)] == [
        "2026-06-23T16:23:00Z",
        "2026-06-23T16:25:00Z",
    ]


def test_int_valued_metrics_are_read_too() -> None:
    raw = [{"points": [{"interval": {"endTime": "T"}, "value": {"int64Value": 3}}]}]
    assert summarise(raw)[0].maximum == 3.0


def test_points_with_no_value_are_skipped_not_counted_as_zero() -> None:
    raw = [
        {
            "points": [
                {"interval": {"endTime": "T"}, "value": {}},
                {"interval": {"endTime": "T"}, "value": {"doubleValue": 0.4}},
            ]
        }
    ]
    assert summarise(raw)[0].series_count == 1


def test_the_two_metrics_that_separate_node_from_container_are_named() -> None:
    assert "node/cpu" in metrics.NODE_CPU
    assert "container/cpu" in metrics.CONTAINER_CPU


def test_an_empty_response_yields_no_samples() -> None:
    assert summarise([]) == []
