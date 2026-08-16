"""One way to shell out to `gcloud logging read`.

`logs.py` and `gke.py` each grew their own copy: the same `require_gcloud`,
the same timeout constant, and the same twelve lines of run-check-parse with
byte-identical error messages. Two copies of an error path is where they drift —
one gains a timeout the other does not, and the difference only shows up during
an incident, which is the one time these commands get used.

The parsing stays with the caller. What is shared is the part that has nothing
to do with what is being read: finding the binary, running it under a timeout,
and turning a non-zero exit or unparseable output into an `ApiError`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from opscore.errors import ApiError, ConfigError

GCLOUD_TIMEOUT = 300
"""Seconds. A 30-day log read against a busy cluster genuinely takes minutes."""


def require_gcloud() -> str:
    """The `gcloud` binary, or a `ConfigError` naming what to install."""
    path = shutil.which("gcloud")
    if path is None:
        raise ConfigError("gcloud not found: install the Google Cloud SDK and authenticate")
    return path


def read_json(command: list[str], *, timeout: int = GCLOUD_TIMEOUT) -> list[dict[str, Any]]:
    """Run a `gcloud ... --format=json` command and return the parsed entries.

    Read-only by construction: it is the caller's job to build a `logging read`
    (or equally non-mutating) argument list, and nothing here adds a flag.

    Returns `[]` for empty output — an absent log entry is a legitimate answer,
    not a failure.
    """
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.SubprocessError as exc:
        raise ApiError(f"gcloud logging read failed: {exc}") from exc

    if completed.returncode != 0:
        raise ApiError("gcloud logging read failed", body=completed.stderr.strip()[:500])

    try:
        parsed = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ApiError(f"gcloud returned unparseable JSON: {exc}") from exc

    return [entry for entry in parsed if isinstance(entry, dict)]
