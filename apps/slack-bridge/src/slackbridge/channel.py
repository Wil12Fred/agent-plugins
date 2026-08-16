"""Bulk channel cleanup for the private session-control channel.

Ported from ``scripts/utils/sessions/clean_channel.py``. Two facts the legacy script
encoded and this module keeps:

* **thread replies must be deleted before their parent** — deleting the parent first
  orphans the replies and they can no longer be enumerated;
* **each identity can only delete what it wrote**, so every message is attempted with the
  bot token first and then the user token. That is why both are resolved here.

This is destructive and irreversible. Nothing deletes unless the caller passes an
explicit confirmation through :func:`opscore.guard.check_write`.
"""

from __future__ import annotations

import time
from typing import Any

from slackbridge.api import SlackAPI

DELETE_PAUSE = 0.25
"""Seconds between deletes — ``chat.delete`` is rate-limited (Tier 3, ~50/min)."""


def collect_timestamps(api: SlackAPI, channel: str) -> list[str]:
    """Every message ts in the channel, thread replies BEFORE their parent."""
    timestamps: list[str] = []
    for message in api.iter_history(channel):
        ts = message.get("ts")
        if message.get("reply_count") and ts:
            replies = api.replies(channel, ts, limit=1000)
            timestamps.extend(r["ts"] for r in replies[1:] if r.get("ts"))
        if ts:
            timestamps.append(ts)
    return timestamps


def purge(
    channel: str,
    timestamps: list[str],
    clients: list[SlackAPI],
    *,
    pause: float = DELETE_PAUSE,
) -> dict[str, Any]:
    """Delete ``timestamps`` from ``channel``, trying each identity in turn.

    Returns ``{"total", "deleted", "failed"}``. A message already gone counts as deleted.
    """
    deleted = 0
    failed: list[str] = []
    for ts in timestamps:
        if any(client.delete(channel, ts) for client in clients):
            deleted += 1
        else:
            failed.append(ts)
        time.sleep(pause)
    return {"total": len(timestamps), "deleted": deleted, "failed": failed}
