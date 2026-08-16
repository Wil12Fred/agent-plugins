"""GKE metrics for an incident window — where the CPU actually went.

The distinction this exists to make: a container throttled by *its own* limit
and a container starved at the *node* level look identical from inside the pod.
Only comparing the two series separates them.

That is how the 2026-06-23 write-up concluded node saturation rather than a
container limit: node CPU peaked near 94.5% at 16:23 UTC while the authorizer
container's own CPU *dropped*. A container hitting its limit pins high; one
starved by its neighbours falls, because it is not being scheduled.

Read-only against the Cloud Monitoring API, with a bearer token from
``gcloud auth print-access-token``.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

from opscore.errors import ApiError, ConfigError
from opscore.http import HttpClient

from cloudprobe.gke import DEFAULT_CLUSTER

MONITORING_API = "https://monitoring.googleapis.com/v3"
ALIGNMENT_SECONDS = 60
GCLOUD_TIMEOUT = 60

# The two series that answer "was this the node or the container?".
NODE_CPU = "kubernetes.io/node/cpu/allocatable_utilization"
CONTAINER_CPU = "kubernetes.io/container/cpu/limit_utilization"

DEFAULT_HOT_THRESHOLD = 0.85


def access_token() -> str:
    """Mint a read-only Monitoring token via gcloud. Never rendered."""
    gcloud = shutil.which("gcloud")
    if gcloud is None:
        raise ConfigError("gcloud not found: install the Google Cloud SDK and authenticate")
    try:
        completed = subprocess.run(
            [gcloud, "auth", "print-access-token"],
            capture_output=True,
            text=True,
            timeout=GCLOUD_TIMEOUT,
            check=False,
        )
    except subprocess.SubprocessError as exc:
        raise ApiError(f"gcloud auth print-access-token failed: {exc}") from exc

    token = completed.stdout.strip()
    if completed.returncode != 0 or not token:
        raise ConfigError(
            "could not mint a GCP access token",
            detail=(completed.stderr.strip()[:300] or "run `gcloud auth login`"),
        )
    return token


@dataclass
class Sample:
    """One aligned minute of a metric, across every series."""

    timestamp: str
    maximum: float
    average: float
    series_count: int
    hot_series: int
    """How many individual series were above the threshold in that minute."""

    top_series: str = ""
    """Which series held the maximum.

    For a node-saturation incident, *which* node pinned at 94.5% is the finding
    — a bare maximum says a node saturated without saying which, and the next
    step (drain it, check its pods) needs the name.
    """

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "max": round(self.maximum, 4),
            "avg": round(self.average, 4),
            "series": self.series_count,
            "hot": self.hot_series,
            "top_series": self.top_series,
        }


def fetch(
    project: str,
    metric_type: str,
    *,
    start: str,
    end: str,
    cluster: str | None = None,
    resource_filter: str = "",
    token: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch raw time series, aligned to one-minute means.

    Scoped to one cluster. Without it the query returns **every** GKE cluster in
    the project, so a dev cluster's pods land inside what is meant to be a
    production incident window — the same defect the sibling `probe-failures`
    had, in the command next to it.

    Three states, and the middle one is why this is not a plain string:

    * ``None`` — use the configured cluster, and **refuse** if there is none.
      Unconfigured must not silently become "everything".
    * ``""`` — across clusters, deliberately. The escape hatch survives.
    * a name — that cluster.

    Collapsing the first two is how a deliberate opt-out becomes an accidental
    default the day the configured value goes missing.
    """
    if cluster is None:
        cluster = DEFAULT_CLUSTER
        if not cluster:
            raise ConfigError(
                "no cluster: set CLOUDPROBE_CLUSTER or pass --cluster",
                detail='pass cluster="" to query across every cluster on purpose',
            )
    clauses = [f'metric.type="{metric_type}"']
    if cluster:
        clauses.append(f'resource.labels.cluster_name="{cluster}"')
    if resource_filter:
        clauses.append(resource_filter)
    metric_filter = " AND ".join(clauses)

    with HttpClient(
        base_url=MONITORING_API,
        headers={"Authorization": f"Bearer {token or access_token()}"},
    ) as http:
        data = http.get(
            f"/projects/{project}/timeSeries",
            params={
                "filter": metric_filter,
                "interval.startTime": start,
                "interval.endTime": end,
                "aggregation.alignmentPeriod": f"{ALIGNMENT_SECONDS}s",
                "aggregation.perSeriesAligner": "ALIGN_MEAN",
                "view": "FULL",
            },
        )

    if not isinstance(data, dict):
        raise ApiError("unexpected Monitoring response")
    series = data.get("timeSeries")
    return [s for s in series if isinstance(s, dict)] if isinstance(series, list) else []


LABEL_KEYS = ("node_name", "pod_name", "container_name", "instance_id")
"""Resource labels that identify a series, most specific answer first."""


def series_label(entry: dict[str, Any]) -> str:
    """Name the series a point came from, so a peak can be attributed."""
    labels = (entry.get("resource") or {}).get("labels") or {}
    for key in LABEL_KEYS:
        value = labels.get(key)
        if value:
            return str(value)
    return "?"


def summarise(
    series: list[dict[str, Any]], *, hot_threshold: float = DEFAULT_HOT_THRESHOLD
) -> list[Sample]:
    """Collapse the series into a per-minute view.

    ``max`` is what shows saturation — an average across 30 nodes hides the one
    node that pinned.
    """
    by_minute: dict[str, list[tuple[float, str]]] = {}
    for entry in series:
        label = series_label(entry)
        for point in entry.get("points") or []:
            interval = (point.get("interval") or {}).get("endTime")
            value = (point.get("value") or {}).get("doubleValue")
            if value is None:
                value = (point.get("value") or {}).get("int64Value")
            if interval is None or value is None:
                continue
            by_minute.setdefault(str(interval), []).append((float(value), label))

    samples: list[Sample] = []
    for timestamp in sorted(by_minute):
        readings = by_minute[timestamp]
        values = [v for v, _ in readings]
        peak_value, peak_label = max(readings, key=lambda pair: pair[0])
        samples.append(
            Sample(
                timestamp=timestamp,
                maximum=peak_value,
                average=sum(values) / len(values),
                series_count=len(values),
                hot_series=sum(1 for v in values if v >= hot_threshold),
                top_series=peak_label,
            )
        )
    return samples
