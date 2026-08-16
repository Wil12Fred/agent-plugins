"""Cloudflare Log Explorer — what the edge saw before the request reached us.

Cloudflare sits in front of the GCP load balancer, so a request rejected at the
edge never appears in any of your logs. When a partner reports "we called your
webhook and got nothing", this is the only place that can tell you whether the
call arrived at all, and what the edge did with it.

Authenticated with the **Global API Key** (``CF_EMAIL`` + ``CF_GLOBAL_API_KEY``)
rather than a scoped token — the Log Explorer SQL API requires it. Read-only:
only ``SELECT`` is issued, and the same guard the database uses enforces that.

Three details about the request shape, all of which have to be right together
or the API answers ``unsupported dataset http_requests``:

* ``http_requests`` is exposed at **zone** level, not account level;
* the request is a **POST**, not a GET;
* the SQL travels as the **raw request body** (``text/plain``) — not as a JSON
  field and not as a query parameter.

The zone id is resolved from the host, so callers pass a hostname rather than
looking an opaque id up by hand.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Any

import httpx
from opscore.errors import ApiError, ConfigError, NotFoundError
from opscore.http import DEFAULT_TIMEOUT, HttpClient
from opscore.secrets import resolve
from opscore.sql import assert_read_only

CF_API = "https://api.cloudflare.com/client/v4"

# The column set the OPER-595 TotalPass webhook investigation used, kept
# verbatim so a re-run of that forensics reproduces the same shape.
DEFAULT_COLUMNS = (
    "botscore",
    "botscoresrc",
    "botdetectionids",
    "cachecachestatus",
    "rayid",
    "clientasn",
    "clientcountry",
    "clientip",
    "clientrequesthost",
    "clientrequestmethod",
    "clientrequestprotocol",
    "clientrequestpath",
    "clientrequesturi",
    "clientrequestreferer",
    "clientrequestscheme",
    "contentscanobjresults",
    "contentscanobjtypes",
    "edgestarttimestamp",
    "edgeresponsecontenttype",
    "edgeresponsestatus",
    "ja3hash",
    "originresponsestatus",
    "clientrequestuseragent",
    "securityactions",
    "securitysources",
    "wafattackscore",
    "wafrceattackscore",
    "wafsqliattackscore",
    "wafxssattackscore",
    "clientxrequestedwith",
    "zonename",
)

DEFAULT_HOST = os.environ.get("CLOUDPROBE_HOST", "")
"""The host whose Cloudflare zone to query. Set `CLOUDPROBE_HOST` or pass it."""
DEFAULT_WEBHOOK_PATH = "/integration-calendar/totalpass/webhook"
DEFAULT_DAYS = 30
DATASET = "http_requests"


def credentials() -> tuple[str, str]:
    """Resolve the Global API Key pair. Neither value is ever rendered."""
    email = resolve(env_var="CF_EMAIL", required=False)
    key = resolve(env_var="CF_GLOBAL_API_KEY", required=False)
    if not email or not key:
        raise ConfigError(
            "missing CF_EMAIL / CF_GLOBAL_API_KEY",
            detail="the Log Explorer SQL API needs the Global API Key, not a scoped token",
        )
    return email, key


def window(days: int = DEFAULT_DAYS) -> tuple[str, str]:
    """Return an ``(start, end)`` RFC3339 pair covering the last ``days``."""
    end = dt.datetime.now(dt.UTC)
    start = end - dt.timedelta(days=days)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_query(
    *,
    host: str = DEFAULT_HOST,
    path: str = DEFAULT_WEBHOOK_PATH,
    days: int = DEFAULT_DAYS,
    columns: tuple[str, ...] = DEFAULT_COLUMNS,
    limit: int = 500,
) -> str:
    """Build the default forensics query: every hit on one host+path."""
    start, end = window(days)
    selected = ", ".join(columns)
    return (
        f"SELECT {selected} FROM {DATASET} "
        f"WHERE clientRequestHost = '{host}' "
        f"AND clientRequestPath = '{path}' "
        f"AND edgeStartTimestamp >= '{start}' AND edgeStartTimestamp <= '{end}' "
        f"ORDER BY edgeStartTimestamp DESC LIMIT {limit}"
    )


def _auth_headers() -> dict[str, str]:
    email, key = credentials()
    return {"X-Auth-Email": email, "X-Auth-Key": key}


def resolve_zone_id(host: str) -> str:
    """Find the Cloudflare zone that serves ``host``.

    A host like ``api.example.com`` is served by the ``example.com`` zone, so the
    registrable domain is what gets looked up.
    """
    override = os.environ.get("CF_ZONE_ID")
    if override:
        return override

    parts = host.split(".")
    candidates = [host, ".".join(parts[-2:])] if len(parts) > 2 else [host]

    with HttpClient(base_url=CF_API, headers=_auth_headers()) as http:
        for candidate in candidates:
            data = http.get("/zones", params={"name": candidate})
            zones = data.get("result") if isinstance(data, dict) else None
            if zones:
                return str(zones[0]["id"])

    raise NotFoundError(
        f"no Cloudflare zone serves {host}",
        detail=f"tried {', '.join(candidates)}; set CF_ZONE_ID to override",
    )


def run(zone_id: str, sql: str) -> list[dict[str, Any]]:
    """Execute a read-only SQL query against the Log Explorer.

    Raises:
        GuardError: the statement is not a read (same guard as the database).
    """
    assert_read_only(sql)

    # The SQL is the request body, as text. Sending it as a query parameter or
    # a JSON field returns "unsupported dataset http_requests", which reads as
    # though the dataset were wrong rather than the envelope.
    with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
        response = client.post(
            f"{CF_API}/zones/{zone_id}/logs/explorer/query/sql",
            headers={**_auth_headers(), "Content-Type": "text/plain"},
            content=sql.encode("utf-8"),
        )
    if response.status_code >= 400:
        raise ApiError(
            f"Log Explorer returned {response.status_code}",
            status_code=response.status_code,
            body=response.text[:500],
        )
    data = response.json()

    if not isinstance(data, dict):
        raise ApiError("unexpected Log Explorer response")
    if not data.get("success", True):
        raise ApiError("Cloudflare rejected the query", body=str(data.get("errors"))[:500])

    result = data.get("result")
    if isinstance(result, list):
        return [row for row in result if isinstance(row, dict)]
    if isinstance(result, dict):
        rows = result.get("rows") or result.get("data")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate a result set the way an incident write-up needs it."""
    by_status: dict[str, int] = {}
    by_country: dict[str, int] = {}
    for row in rows:
        status = str(row.get("edgeresponsestatus") or row.get("edgeResponseStatus") or "?")
        by_status[status] = by_status.get(status, 0) + 1
        country = str(row.get("clientcountry") or row.get("clientCountry") or "?")
        by_country[country] = by_country.get(country, 0) + 1

    return {
        "total": len(rows),
        "by_edge_status": dict(sorted(by_status.items(), key=lambda kv: -kv[1])),
        "by_country": dict(sorted(by_country.items(), key=lambda kv: -kv[1])[:10]),
    }
