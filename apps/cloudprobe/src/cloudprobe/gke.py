"""GKE incident forensics: what the cluster was doing when it fell over.

Kubernetes reports both a starving pod and a dead pod as an ``Unhealthy`` probe
event, so a raw event count says nothing. The distinction is in the message:

* ``context deadline exceeded`` — the probe **timed out**. The pod answered too
  slowly, which is a CPU-starvation signal.
* ``connection refused`` — nothing was listening. The pod is down or draining.

A CPU incident shows a spike in the first and not the second, which is how the
"+543% authorizer timeout" figure in the 2026-06-23 incident write-up was
derived. The rate, not the count, is what is comparable: windows differ in
length, so ``count / window_minutes`` is computed before anything is compared.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from enum import StrEnum

from opscore.errors import ConfigError

from cloudprobe import gcloud
from cloudprobe.gcloud import require_gcloud

DEFAULT_PROJECT = os.environ.get("CLOUDPROBE_PROJECT", "")
"""The GCP project. Unset by default, for the same reason as the cluster."""
DEFAULT_CLUSTER = os.environ.get("CLOUDPROBE_CLUSTER", "")
"""The GKE cluster to read. No default: guessing one silently reads the wrong
environment, and an incident window built from the wrong cluster looks like
evidence. Set `CLOUDPROBE_CLUSTER` or pass `--cluster`."""
DEFAULT_NAMESPACE = "default"
DEFAULT_LIMIT = 5000
"""The original runner's defaults. 1000 was a silent cap: an incident window
busy enough to matter can exceed it, and a truncated read understates the very
rate the comparison is measuring."""


class ProbeFailure(StrEnum):
    """Why a probe failed — the two mean opposite things."""

    TIMEOUT = "timeout"
    """`context deadline exceeded`: the pod answered too slowly (CPU starvation)."""

    REFUSED = "refused"
    """`connection refused`: nothing listening (pod down or draining)."""

    OTHER = "other"


TIMEOUT_MARKER = "context deadline exceeded"
REFUSED_MARKER = "connection refused"


def classify(message: str) -> ProbeFailure:
    lowered = message.lower()
    if TIMEOUT_MARKER in lowered:
        return ProbeFailure.TIMEOUT
    if REFUSED_MARKER in lowered:
        return ProbeFailure.REFUSED
    return ProbeFailure.OTHER


@dataclass(frozen=True)
class Window:
    """A time window, in RFC3339."""

    start: str
    end: str

    @property
    def minutes(self) -> float:
        started = dt.datetime.fromisoformat(self.start.replace("Z", "+00:00"))
        ended = dt.datetime.fromisoformat(self.end.replace("Z", "+00:00"))
        return max((ended - started).total_seconds() / 60, 1e-9)


@dataclass
class Counts:
    """Probe failures in one window for one service."""

    timeout: int = 0
    refused: int = 0
    other: int = 0

    def add(self, kind: ProbeFailure) -> None:
        setattr(self, kind.value, getattr(self, kind.value) + 1)

    def rate(self, kind: ProbeFailure, window: Window) -> float:
        return float(getattr(self, kind.value)) / window.minutes


def read_probe_events(
    service: str,
    window: Window,
    *,
    project: str = DEFAULT_PROJECT,
    cluster: str | None = None,
    namespace: str = DEFAULT_NAMESPACE,
    limit: int = DEFAULT_LIMIT,
) -> list[str]:
    """Read `Unhealthy` probe events for one service in one window (read-only).

    **`cluster_name` and `namespace_name` are part of the filter, and `--project`
    is passed explicitly** — the same rule `trace-task` documents. dev and prod
    run the same image with the same pod-name prefixes, so a filter matching on
    `pod_name` alone can pull a dev cluster's entries into a prod window and
    corrupt the baseline-versus-incident rate this exists to compute. Without
    `--project` the query silently follows whatever the local gcloud config
    happens to point at.
    """
    cluster = DEFAULT_CLUSTER if cluster is None else cluster
    if not cluster:
        raise ConfigError(
            "no cluster: set CLOUDPROBE_CLUSTER or pass --cluster",
            detail="an empty cluster matches nothing and reads like an empty result",
        )
    log_filter = (
        'resource.type="k8s_pod" '
        'AND jsonPayload.reason="Unhealthy" '
        f'AND resource.labels.cluster_name="{cluster}" '
        f'AND resource.labels.namespace_name="{namespace}" '
        f'AND resource.labels.pod_name:"{service}" '
        f'AND timestamp>="{window.start}" AND timestamp<="{window.end}"'
    )
    command = [
        require_gcloud(),
        "logging",
        "read",
        log_filter,
        "--project",
        project,
        "--format=json",
        f"--limit={limit}",
    ]
    entries = gcloud.read_json(command)
    return [str((entry.get("jsonPayload") or {}).get("message", "")) for entry in entries]


def count_failures(service: str, window: Window, **scope: object) -> Counts:
    counts = Counts()
    for message in read_probe_events(service, window, **scope):  # type: ignore[arg-type]
        counts.add(classify(message))
    return counts


def percent_change(baseline_rate: float, incident_rate: float) -> float | None:
    """Percentage change between two rates.

    ``None`` when the baseline rate is zero: going from "never" to "sometimes"
    is not a percentage, and reporting one would be inventing a number.
    """
    if baseline_rate == 0:
        return None
    return (incident_rate / baseline_rate - 1) * 100


def compare(
    services: list[str],
    baseline: Window,
    incident: Window,
    *,
    project: str = DEFAULT_PROJECT,
    cluster: str | None = None,
    namespace: str = DEFAULT_NAMESPACE,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, object]]:
    """Compare probe-failure rates per service between two windows.

    Both windows are read with the **same** scope, deliberately: a baseline and
    an incident measured against different clusters is not a comparison.
    """
    scope: dict[str, object] = {
        "project": project,
        "cluster": cluster,
        "namespace": namespace,
        "limit": limit,
    }
    rows: list[dict[str, object]] = []
    for service in services:
        base_counts = count_failures(service, baseline, **scope)
        inc_counts = count_failures(service, incident, **scope)
        for kind in (ProbeFailure.TIMEOUT, ProbeFailure.REFUSED):
            base_rate = base_counts.rate(kind, baseline)
            inc_rate = inc_counts.rate(kind, incident)
            rows.append(
                {
                    "service": service,
                    "cluster": cluster,
                    "failure": kind.value,
                    "baseline_count": getattr(base_counts, kind.value),
                    "incident_count": getattr(inc_counts, kind.value),
                    "baseline_rate_per_min": round(base_rate, 4),
                    "incident_rate_per_min": round(inc_rate, 4),
                    "pct_change": percent_change(base_rate, inc_rate),
                }
            )
    return rows
