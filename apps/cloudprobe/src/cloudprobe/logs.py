"""Following one unit of work through the GKE logs.

Three things about GKE's logs decide whether a query finds anything, and all
three are easy to get wrong in a way that returns zero rows and looks like "it
never happened":

1. **The services log structured JSON**, and GKE parses it, so the fields land
   in ``jsonPayload`` and ``textPayload`` is empty. A filter on ``textPayload``
   silently matches nothing.
2. **``cluster_name`` is mandatory.** dev and prod run the *same container
   image*, so without it a dev query happily returns production entries.
3. **A task id appears under three different field names** depending on which
   side logged it: ``data.taskId`` when the producer enqueues, ``data.messageId``
   once the consumer has parsed the payload, and inside ``data.rawTask`` for
   the consumed event — which is logged *before* parsing, so it is the only one
   that survives a malformed payload. Searching one of the three finds a third
   of the story.

This was learned reconstructing an OPER-816 run and is the reusable half of a
script whose trigger endpoint never shipped.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from opscore.errors import ConfigError

from cloudprobe import gcloud
from cloudprobe.gcloud import require_gcloud

DEFAULT_CLUSTER = os.environ.get("CLOUDPROBE_CLUSTER", "")
"""See `gke.DEFAULT_CLUSTER`: unset on purpose, because the wrong cluster
produces an answer rather than an error."""
DEFAULT_FRESHNESS_MINUTES = 60

# Where a task id can appear, by who logged it.
#
# The structured fields below were the original assumption and they find
# nothing: a `jsonPayload.data.taskId:*` query over the whole `my-project`
# project returns **zero entries in 24 hours**. The field does not exist.
# queue-reservation logs through console.log, so GCP files the whole line under
# `textPayload` with the id as its first token:
#
#     6fc39002-…-53adf173650d 🚀 ~ file: index.js:355 ~ La task fue publicada…
#
# `textPayload` is what actually matches; the structured names are kept because
# they cost one OR clause and a service that logs properly would land there.
# Found by running `trace-task` for the first time — it had returned 0 hits for
# every task id since it was written.
TASK_ID_FIELDS = ("jsonPayload.data.taskId", "jsonPayload.data.messageId")
TASK_ID_SUBSTRING_FIELDS = ("jsonPayload.data.rawTask", "textPayload")


@dataclass
class LogEntry:
    """One structured log line."""

    timestamp: str
    event: str
    payload: dict[str, Any]
    resource: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "event": self.event,
            "container": self.resource.get("container_name"),
            "cluster": self.resource.get("cluster_name"),
            "payload": self.payload,
        }


def build_filter(
    task_id: str,
    *,
    cluster: str | None = None,
    container: str | None = None,
) -> str:
    """Build a filter that finds the task under all three of its field names."""
    cluster = DEFAULT_CLUSTER if cluster is None else cluster
    if not cluster:
        raise ConfigError(
            "no cluster: set CLOUDPROBE_CLUSTER or pass --cluster",
            detail=(
                'an empty cluster would emit cluster_name="", which matches nothing '
                "and reads exactly like 'there were no such logs'"
            ),
        )
    clauses = [
        'resource.type="k8s_container"',
        f'resource.labels.cluster_name="{cluster}"',
    ]
    if container:
        clauses.append(f'resource.labels.container_name="{container}"')

    # `:` is substring match — rawTask holds the whole serialised payload.
    identity = " OR ".join(
        [
            *(f'{field}="{task_id}"' for field in TASK_ID_FIELDS),
            *(f'{field}:"{task_id}"' for field in TASK_ID_SUBSTRING_FIELDS),
        ]
    )
    clauses.append(f"({identity})")
    return " AND ".join(clauses)


def read(
    task_id: str,
    *,
    cluster: str | None = None,
    container: str | None = None,
    freshness_minutes: int = DEFAULT_FRESHNESS_MINUTES,
    limit: int = 200,
) -> list[LogEntry]:
    """Read every log line mentioning ``task_id`` (read-only)."""
    command = [
        require_gcloud(),
        "logging",
        "read",
        build_filter(task_id, cluster=cluster, container=container),
        f"--freshness={freshness_minutes}m",
        f"--limit={limit}",
        "--format=json",
    ]
    return [_parse(entry) for entry in gcloud.read_json(command)]


def _parse(entry: dict[str, Any]) -> LogEntry:
    payload = entry.get("jsonPayload")
    if not isinstance(payload, dict):
        # textPayload is only a fallback for a line the log agent could not
        # parse as JSON — normally it is empty.
        try:
            payload = json.loads(entry.get("textPayload") or "{}")
        except (json.JSONDecodeError, TypeError):
            payload = {"raw": entry.get("textPayload")}

    return LogEntry(
        timestamp=str(entry.get("timestamp", "")),
        event=str(payload.get("event") or payload.get("message") or ""),
        payload=payload if isinstance(payload, dict) else {},
        resource=(entry.get("resource") or {}).get("labels") or {},
    )


def timeline(entries: list[LogEntry]) -> list[dict[str, Any]]:
    """Order the entries oldest-first — the shape a forensic write-up needs."""
    return [e.as_dict() for e in sorted(entries, key=lambda e: e.timestamp)]
