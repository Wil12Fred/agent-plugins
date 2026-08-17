"""Watching a deploy: is the thing that just shipped making anything worse?

Ported from a deploy watcher written for one release, which watched two merge
requests, an error-code baseline, reservation volume and an iOS release, all
hardcoded to one ticket. The ticket-bound part was three constants. What is here
is the part that was not: two measurement techniques that keep being needed and
keep being rewritten.

**Alert below the floor, not below the average.** Reservation volume swings by
hour and by weekday, so "down 30% on yesterday" fires every Monday. The floor is
the *minimum* that hour of day reached across the last 14 days: crossing it is
something that has not happened once in a fortnight. Deliberately blunt — it
misses a shallow dip and it does not cry wolf, and a monitor nobody believes is
worse than no monitor.

**Compare the platform against a control.** A backend problem moves iOS and
Android together; a release problem moves one. So a raw error rate cannot tell
"our new build is broken" from "the API is having a bad hour", and only the
comparison can. Alert when the subject degrades **and the control does not
follow it**.

Everything here is a read: `glab api`, Cloudflare Log Explorer SQL, and the
caller's own database query. It has no `--confirm-prod-write` because it cannot
write.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from opscore.errors import ValidationError

DEFAULT_INTERVAL_SECONDS = 900
DEFAULT_FLOOR_DAYS = 14
DEFAULT_CONTROL_MULTIPLE = 2.0
"""How far above the control the subject must sit before it is the subject's fault."""


@dataclass(frozen=True)
class MergeRequest:
    """One MR being watched, as `project!iid`."""

    project: str
    iid: int

    @classmethod
    def parse(cls, raw: str) -> MergeRequest:
        """`myorg/auth-service!327` -> MergeRequest.

        Raises:
            ValidationError: the string is not `project!iid`.
        """
        project, separator, iid = raw.partition("!")
        if not separator or not iid.isdigit() or not project:
            raise ValidationError(
                f"expected project!iid, got {raw!r}",
                detail="for example myorg/auth-service!327",
            )
        return cls(project=project, iid=int(iid))

    def __str__(self) -> str:
        return f"{self.project}!{self.iid}"


@dataclass(frozen=True)
class MergeState:
    state: str
    merge_commit: str
    pipeline: str

    def as_dict(self) -> dict[str, str]:
        return {"state": self.state, "merge_commit": self.merge_commit, "pipeline": self.pipeline}


@dataclass(frozen=True)
class Reading:
    """One probe's result: a number, a threshold, and whether it crossed."""

    name: str
    value: float
    threshold: float | None
    alert: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "probe": self.name,
            "value": self.value,
            "threshold": self.threshold,
            "alert": self.alert,
            "detail": self.detail,
        }


def _run(command: list[str], *, timeout: int = 300) -> str:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout if completed.returncode == 0 else ""


def _glab(path: str) -> Any:
    raw = _run(["glab", "api", path], timeout=90)
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None


def merge_state(mr: MergeRequest) -> MergeState | None:
    """The MR's state and the pipeline on its head commit.

    Returns None when `glab` cannot answer, which is not the same as "open" —
    the caller must not render an unknown as a state.
    """
    encoded = quote(mr.project, safe="")
    data = _glab(f"projects/{encoded}/merge_requests/{mr.iid}")
    if not isinstance(data, dict):
        return None
    head = data.get("head_pipeline")
    return MergeState(
        state=str(data.get("state") or "?"),
        merge_commit=str(data.get("merge_commit_sha") or "")[:8],
        pipeline=str((head or {}).get("status") or "?"),
    )


def deploy_state(project: str, ref: str) -> str:
    """The newest pipeline on the default branch.

    Merging into it *is* the deploy — `deploy-prod` has no manual gate — so this
    is the answer to "did the merge reach production", and the MR's own pipeline
    is not.
    """
    encoded = quote(project, safe="")
    data = _glab(f"projects/{encoded}/pipelines?ref={ref}&per_page=1")
    if isinstance(data, list) and data:
        first = data[0]
        return f"{first.get('status')} ({str(first.get('sha'))[:8]})"
    return "?"


def below_floor(current: int, floor: int, *, label: str) -> Reading:
    """Volume against the floor. Zero floor means unknown, and never alerts.

    A floor of 0 comes back when the window has no history at all; treating that
    as "everything is below it" would alert on every fresh environment.
    """
    alert = bool(floor) and current < floor
    return Reading(
        name=label,
        value=float(current),
        threshold=float(floor) if floor else None,
        alert=alert,
        detail=(
            f"{current} this hour, below the {DEFAULT_FLOOR_DAYS}-day floor of {floor}"
            if alert
            else f"{current} this hour, floor {floor or 'unknown'}"
        ),
    )


def floor_query(table: str, column: str = "insDate", days: int = DEFAULT_FLOOR_DAYS) -> str:
    """SQL for `(last closed hour, that hour's N-day minimum, the hour)`.

    Both halves close on the hour. Counting the *current* hour against a floor
    built from complete hours compares a partial number with whole ones, which
    reads as a collapse every time the monitor runs at ten past.
    """
    return (
        "SELECT "
        f"(SELECT COUNT(*) FROM {table} "
        f"  WHERE {column} >= DATE_SUB(DATE_FORMAT(NOW(),'%Y-%m-%d %H:00:00'), INTERVAL 1 HOUR) "
        f"    AND {column} <  DATE_FORMAT(NOW(),'%Y-%m-%d %H:00:00')) AS last_hour, "
        "(SELECT MIN(n) FROM ("
        f"  SELECT COUNT(*) n FROM {table} "
        f"   WHERE {column} >= DATE_SUB(NOW(), INTERVAL {days} DAY) "
        f"     AND HOUR({column}) = HOUR(DATE_SUB(NOW(), INTERVAL 1 HOUR)) "
        f"   GROUP BY DATE({column})) x) AS floor_value, "
        "HOUR(DATE_SUB(NOW(), INTERVAL 1 HOUR)) AS hour_of_day"
    )


@dataclass(frozen=True)
class EdgeRate:
    """Requests, error percentage and 5xx count for one user agent."""

    requests: int
    error_pct: float
    server_errors: int


def edge_rate_sql(user_agent: str, start: str, end: str) -> str:
    """Cloudflare Log Explorer SQL for one user agent in a closed window."""
    return (
        "SELECT EdgeResponseStatus AS st, COUNT(*) AS n FROM http_requests "
        f"WHERE ClientRequestUserAgent LIKE '{user_agent}' "
        f"AND EdgeStartTimestamp >= '{start}' AND EdgeStartTimestamp < '{end}' "
        "GROUP BY EdgeResponseStatus ORDER BY n DESC LIMIT 15"
    )


def edge_rate(rows: list[dict[str, Any]]) -> EdgeRate | None:
    """Fold status-code counts into a rate. None when the window had no traffic.

    None must stay distinguishable from `0.0%`: no traffic means the probe did
    not measure, and a release with no users is not a healthy release.
    """
    total = sum(int(row["n"]) for row in rows)
    if not total:
        return None
    errors = sum(int(row["n"]) for row in rows if int(row["st"]) >= 400)
    server = sum(int(row["n"]) for row in rows if int(row["st"]) >= 500)
    return EdgeRate(requests=total, error_pct=100.0 * errors / total, server_errors=server)


def against_control(
    subject: EdgeRate | None,
    control: EdgeRate | None,
    *,
    alert_pct: float,
    control_multiple: float = DEFAULT_CONTROL_MULTIPLE,
    label: str = "edge",
) -> Reading:
    """Subject vs control. Alert only when the subject degrades *alone*.

    Three outcomes, and the middle one is the point:

    - subject over the threshold, control following it -> the backend is having
      a bad hour and this release is not the cause. No alert.
    - subject over the threshold, control flat -> the release. Alert.
    - any 5xx from the subject -> alert regardless, because a server error is
      not a rate.
    """
    if subject is None:
        return Reading(label, 0.0, alert_pct, False, "no traffic in the window — not measured")

    detail = f"{subject.requests} req, {subject.error_pct:.1f}% err, {subject.server_errors} 5xx"
    alert = False
    if control is not None:
        detail += f" | control {control.requests} req, {control.error_pct:.1f}% err"
        if (
            subject.error_pct > alert_pct
            and subject.error_pct > control.error_pct * control_multiple
        ):
            detail += " — subject degrades and control does not"
            alert = True
    elif subject.error_pct > alert_pct:
        detail += " — over threshold, no control to compare against"
        alert = True
    if subject.server_errors > 0:
        detail += f" — {subject.server_errors} 5xx"
        alert = True
    return Reading(label, subject.error_pct, alert_pct, alert, detail)


def closed_window(hours_back: int = 1, *, now: datetime | None = None) -> tuple[str, str]:
    """The last fully closed hour, as `(start, end)` ISO strings.

    The edge lags a few minutes, so the window always ends one hour back. Asking
    for the current hour returns a number that is still being written.
    """
    anchor = (now or datetime.now(UTC)).replace(minute=0, second=0, microsecond=0)
    end = anchor - timedelta(hours=hours_back)
    start = end - timedelta(hours=1)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(fmt), end.strftime(fmt)


def parse_baseline(raw: list[str]) -> dict[str, int]:
    """`["USER_DELETED=0", "LOGIN_STATUS=4"]` -> `{...}`.

    Raises:
        ValidationError: an entry is not `NAME=count`.
    """
    parsed: dict[str, int] = {}
    for entry in raw:
        name, separator, count = entry.partition("=")
        if not separator or not count.strip().isdigit() or not name.strip():
            raise ValidationError(
                f"expected NAME=count, got {entry!r}",
                detail="for example GLOBAL.ERROR_USER_DELETED=0",
            )
        parsed[name.strip()] = int(count.strip())
    return parsed


def over_baseline(name: str, observed: int, baseline: int) -> Reading:
    """An error code against its pre-deploy baseline.

    A baseline of 0 is the strong case: the code has never fired, so the first
    occurrence is unambiguously the change under watch.
    """
    alert = observed > baseline
    return Reading(
        name=name,
        value=float(observed),
        threshold=float(baseline),
        alert=alert,
        detail=(
            f"{observed} occurrence(s) against a baseline of {baseline}"
            if alert
            else f"{observed} occurrence(s), at or below baseline {baseline}"
        ),
    )


def rows_from_command(command: str, sql: str) -> list[dict[str, Any]]:
    """Run `command`, feeding it `sql` on stdin, and parse JSON rows from stdout.

    This is how the volume probe reaches a database without this tool knowing
    what database it is. Anything that reads SQL and prints a JSON array works —
    a `mysql` wrapper, `psql -t`, another CLI — so the credential stays with the
    caller and never has to be configured here.

    Returns an empty list when the command fails or prints something that is not
    JSON. That is deliberate: a probe that cannot read is *unmeasured*, and the
    caller renders it as such. Raising here would turn "I could not check the
    volume" into "the deploy is broken", which is a different and wrong claim.
    """
    try:
        completed = subprocess.run(
            command,
            shell=True,
            input=sql,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    text = completed.stdout
    try:
        start, end = text.index("["), text.rindex("]") + 1
        parsed = json.loads(text[start:end])
    except (ValueError, json.JSONDecodeError):
        return []
    return [row for row in parsed if isinstance(row, dict)]
