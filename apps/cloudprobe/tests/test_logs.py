"""The three ways a GKE log query silently finds nothing."""

from __future__ import annotations

import pytest
from opscore.errors import ConfigError

from cloudprobe import logs
from cloudprobe.logs import LogEntry, build_filter, timeline


def test_the_filter_searches_all_three_task_id_fields() -> None:
    # The producer logs data.taskId, the consumer data.messageId, and the
    # consumed event only carries it inside rawTask — searching one finds a
    # third of the story.
    f = build_filter("abc-123")
    assert 'jsonPayload.data.taskId="abc-123"' in f
    assert 'jsonPayload.data.messageId="abc-123"' in f
    assert 'jsonPayload.data.rawTask:"abc-123"' in f


def test_raw_task_uses_substring_match_not_equality() -> None:
    # rawTask holds the whole serialised payload, so `=` would never match.
    assert 'rawTask:"abc"' in build_filter("abc")
    assert 'rawTask="abc"' not in build_filter("abc")


def test_the_cluster_is_always_pinned() -> None:
    # dev and prod run the same container image; without this a dev query
    # returns production entries.
    assert 'resource.labels.cluster_name="prod-cluster"' in build_filter("x")
    assert 'cluster_name="dev-cluster"' in build_filter("x", cluster="dev-cluster")


def test_the_filter_covers_both_log_shapes() -> None:
    """This test used to assert the opposite, and was wrong.

    It read: "the services log structured JSON and GKE parses it, so
    textPayload is empty and filtering on it matches nothing." That premise was
    never checked against the logs. queue-reservation logs through console.log,
    so GCP files the whole line under `textPayload` — and
    `jsonPayload.data.taskId` matches nothing at all, in the entire project.

    The test passed for as long as the bug existed, because it asserted the
    same assumption the code was built on. Exactly the failure mode
    SPECIFICATION.md §4 names: ask whether the test would fail if the behaviour
    were wrong. This one could not.
    """
    built = build_filter("x")
    assert "textPayload" in built, "queue-reservation logs land here"
    assert "jsonPayload" in built, "kept for services that do log structured data"


def test_a_container_narrows_without_dropping_the_cluster() -> None:
    f = build_filter("x", container="lessons-service")
    assert 'container_name="lessons-service"' in f
    assert "cluster_name" in f


def test_structured_payloads_are_read_from_json_payload() -> None:
    entry = logs._parse(
        {
            "timestamp": "2026-08-06T10:00:00Z",
            "jsonPayload": {"event": "TASK_CONSUMED", "data": {"taskId": "abc"}},
            "resource": {"labels": {"cluster_name": "prod-cluster", "container_name": "lessons"}},
        }
    )
    assert entry.event == "TASK_CONSUMED"
    assert entry.resource["container_name"] == "lessons"


def test_an_unparsed_line_falls_back_to_text_payload() -> None:
    entry = logs._parse({"timestamp": "T", "textPayload": '{"event": "X"}'})
    assert entry.event == "X"
    # And a line that is not JSON at all is kept rather than dropped.
    assert logs._parse({"timestamp": "T", "textPayload": "boom"}).payload["raw"] == "boom"


def test_the_timeline_runs_oldest_first() -> None:
    entries = [
        LogEntry(timestamp="2026-08-06T10:05:00Z", event="b", payload={}, resource={}),
        LogEntry(timestamp="2026-08-06T10:00:00Z", event="a", payload={}, resource={}),
    ]
    assert [row["event"] for row in timeline(entries)] == ["a", "b"]


def test_the_filter_searches_textpayload_which_is_where_the_id_actually_is() -> None:
    """`trace-task` returned 0 hits for every task id until this was fixed.

    It searched `jsonPayload.data.taskId`, `.messageId` and `.rawTask`. A
    `jsonPayload.data.taskId:*` query over the whole `my-project` project
    returns **zero entries in 24 hours** — the field does not exist.
    queue-reservation logs through console.log, so GCP files the entire line
    under `textPayload` with the id as its first token.

    This is the failure mode the specification warns about: the command
    type-checked, shipped, and answered "no entries" — indistinguishable from a
    task that genuinely produced no logs. It was only caught by running it
    against a task id taken from the live logs.
    """
    built = logs.build_filter("6fc39002-904b-46b1-81f4-53adf173650d")
    assert 'textPayload:"6fc39002-904b-46b1-81f4-53adf173650d"' in built, (
        "textPayload is the only field these logs actually populate; without it "
        "trace-task silently finds nothing"
    )
    assert "textPayload" in logs.TASK_ID_SUBSTRING_FIELDS


def test_an_unconfigured_cluster_is_refused_not_silently_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule: `cluster_name=""` matches nothing and reads like "there were no logs".

    That is the worst shape a query can have — a search that could not have
    matched, reported as proof of absence. The original code shipped a hardcoded
    cluster, which was wrong for anyone else; replacing it with an empty default
    would have been wrong for everyone, quietly. So it refuses.
    """
    monkeypatch.setattr(logs, "DEFAULT_CLUSTER", "")

    with pytest.raises(ConfigError) as caught:
        build_filter("task-1")

    assert "CLOUDPROBE_CLUSTER" in str(caught.value)


def test_the_cluster_is_read_at_call_time_not_import_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule: a default bound at `def` time cannot be reconfigured.

    `cluster: str = DEFAULT_CLUSTER` evaluates once, at import — so an
    environment variable set afterwards, or a test that patches the constant,
    changes nothing and the code looks configurable while being fixed. Caught by
    six tests failing after the constant became environment-driven.
    """
    monkeypatch.setattr(logs, "DEFAULT_CLUSTER", "somewhere-else")

    assert 'cluster_name="somewhere-else"' in build_filter("task-1")
