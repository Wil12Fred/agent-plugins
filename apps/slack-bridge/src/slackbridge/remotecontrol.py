"""Is a named Claude session reachable through Remote Control right now?

Measured on Claude Code 2.1.332 — the file format is not a documented interface, so this
reads it defensively: every live session has ``$CLAUDE_HOME/sessions/<pid>.json`` with its
``name``, ``pid``, ``kind`` and ``startedAt``, and a session connected to Remote Control
also carries ``bridgeSessionId``. That id is the claude.ai session: the conversation opens
at ``https://claude.ai/code/<bridgeSessionId>`` (the same URL Claude writes into a commit's
``Claude-Session:`` trailer).

A stale file (its pid is gone) is ignored, and when several live sessions share a name the
newest one is the one reported.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from slackbridge import claude

SESSIONS = claude.CLAUDE_HOME / "sessions"
URL = "https://claude.ai/code/{}"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def live_sessions(
    directory: Path | None = None, alive: Callable[[int], bool] = _alive
) -> list[dict[str, Any]]:
    """Every session state file whose process is still running; unreadable files skipped."""
    base = directory if directory is not None else SESSIONS
    found: list[dict[str, Any]] = []
    for path in sorted(base.glob("*.json")) if base.is_dir() else []:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        pid = data.get("pid")
        if isinstance(data, dict) and isinstance(pid, int) and alive(pid):
            found.append(data)
    return found


def status(names: Iterable[str], sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per requested name: found or not, and whether Remote Control is connected."""
    rows: list[dict[str, Any]] = []
    for name in names:
        matches = [s for s in sessions if s.get("name") == name]
        if not matches:
            rows.append({"name": name, "found": False, "remote_control": False})
            continue
        newest = max(matches, key=lambda s: s.get("startedAt") or 0)
        bridge = str(newest.get("bridgeSessionId") or "")
        rows.append(
            {
                "name": name,
                "found": True,
                "pid": newest.get("pid"),
                "kind": newest.get("kind"),
                "remote_control": bool(bridge),
                "url": URL.format(bridge) if bridge else None,
            }
        )
    return rows
