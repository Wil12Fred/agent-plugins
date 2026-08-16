"""GKE incident forensics: the scoping that keeps dev out of a prod comparison."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from opscore.errors import ApiError, ConfigError

from cloudprobe import gcloud, gke, metrics


def test_the_probe_filter_scopes_to_a_cluster_and_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # dev and prod run the same image with the same pod-name prefixes, so a
    # filter matching on pod_name alone pulls dev-cluster entries into a prod-cluster
    # window and corrupts the baseline-vs-incident rate. `trace-task` documents
    # this rule; probe-failures had lost it, along with --project entirely.
    seen: dict[str, list[str]] = {}

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        seen["command"] = command
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(gcloud.shutil, "which", lambda _n: "/usr/bin/gcloud")
    monkeypatch.setattr(gcloud.subprocess, "run", fake_run)
    gke.read_probe_events("authorizer", gke.Window("A", "B"), cluster="prod-cluster")

    command = seen["command"]
    log_filter = command[3]
    assert 'resource.labels.cluster_name="prod-cluster"' in log_filter
    assert 'resource.labels.namespace_name="default"' in log_filter
    assert "--project" in command, "without it the query follows the local gcloud config"
    assert command[command.index("--project") + 1] == gke.DEFAULT_PROJECT


def test_compare_threads_every_scope_flag_to_gcloud(monkeypatch: pytest.MonkeyPatch) -> None:
    # `compare` forwards a dict whose keys must match `read_probe_events`'s
    # parameter names. Only `cluster` was covered; a typo in `namespace` or
    # `project` would silently fall back to the default and widen the query.
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(gcloud.shutil, "which", lambda _n: "/usr/bin/gcloud")
    monkeypatch.setattr(gcloud.subprocess, "run", fake_run)
    gke.compare(
        ["authorizer"],
        gke.Window("2026-06-23T13:00:00Z", "2026-06-23T16:00:00Z"),
        gke.Window("2026-06-23T16:18:00Z", "2026-06-23T16:32:00Z"),
        project="other-project",
        cluster="dev-cluster",
        namespace="other-ns",
        limit=77,
    )

    assert len(commands) == 2, "both windows must be read"
    for command in commands:
        assert command[command.index("--project") + 1] == "other-project"
        assert "--limit=77" in command
        assert 'resource.labels.cluster_name="dev-cluster"' in command[3]
        assert 'resource.labels.namespace_name="other-ns"' in command[3]


def test_metrics_scopes_to_one_cluster_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without this the query returns every GKE cluster in the project, so
    # dev-cluster and other-cluster land inside what is meant to be a prod-cluster incident
    # window. Same defect as probe-failures had, in the command next to it.
    captured: dict[str, object] = {}

    class _Http:
        def __enter__(self) -> _Http:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, _path: str, params: dict[str, object]) -> dict[str, object]:
            captured["filter"] = params["filter"]
            return {"timeSeries": []}

    monkeypatch.setattr(metrics, "HttpClient", lambda **_kw: _Http())
    monkeypatch.setattr(metrics, "access_token", lambda: "t")

    metrics.fetch("my-project", metrics.NODE_CPU, start="A", end="B")
    assert 'resource.labels.cluster_name="prod-cluster"' in str(captured["filter"])

    metrics.fetch("my-project", metrics.NODE_CPU, start="A", end="B", cluster="")
    assert "cluster_name" not in str(captured["filter"]), "an empty cluster must be deliberate"


def test_a_peak_says_which_series_held_it() -> None:
    # "the node saturated at 94.5%" is not actionable without the node name.
    samples = metrics.summarise(
        [
            {
                "resource": {"labels": {"node_name": "gke-pool-3-4tpz"}},
                "points": [{"interval": {"endTime": "T1"}, "value": {"doubleValue": 94.5}}],
            },
            {
                "resource": {"labels": {"node_name": "gke-pool-1-aaaa"}},
                "points": [{"interval": {"endTime": "T1"}, "value": {"doubleValue": 42.0}}],
            },
        ]
    )
    assert samples[0].maximum == 94.5
    assert samples[0].top_series == "gke-pool-3-4tpz"
    assert samples[0].as_dict()["top_series"] == "gke-pool-3-4tpz"


class TestGcloudRunner:
    """The error paths `logs.py` and `gke.py` used to each own a copy of.

    Extracting them is only safe if they still behave the same, and these are
    the three ways a `gcloud logging read` goes wrong. None was covered while
    the code was duplicated — which is how two copies drift without anyone
    noticing.
    """

    def test_a_non_zero_exit_becomes_an_api_error_carrying_stderr(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gcloud.shutil, "which", lambda _n: "/usr/bin/gcloud")
        monkeypatch.setattr(
            gcloud.subprocess,
            "run",
            lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="", stderr="PERMISSION_DENIED"),
        )
        with pytest.raises(ApiError) as caught:
            gcloud.read_json(["gcloud", "logging", "read"])
        assert "PERMISSION_DENIED" in str(caught.value.detail)

    def test_unparseable_output_is_reported_as_such(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An HTML error page from a proxy is not an empty result set."""
        monkeypatch.setattr(gcloud.shutil, "which", lambda _n: "/usr/bin/gcloud")
        monkeypatch.setattr(
            gcloud.subprocess,
            "run",
            lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="<html>", stderr=""),
        )
        with pytest.raises(ApiError, match="unparseable"):
            gcloud.read_json(["gcloud", "logging", "read"])

    def test_no_entries_is_an_answer_not_a_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gcloud.shutil, "which", lambda _n: "/usr/bin/gcloud")
        monkeypatch.setattr(
            gcloud.subprocess,
            "run",
            lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="", stderr=""),
        )
        assert gcloud.read_json(["gcloud", "logging", "read"]) == []

    def test_a_missing_binary_names_what_to_install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gcloud.shutil, "which", lambda _n: None)
        with pytest.raises(ConfigError, match="Google Cloud SDK"):
            gcloud.require_gcloud()
