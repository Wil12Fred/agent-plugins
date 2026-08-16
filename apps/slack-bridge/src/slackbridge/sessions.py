"""One dispatch policy shared by the CLI, the slash commands and the message listener.

This replaces ``listeners/_session_bridge.py``, which shelled out to
``claude-sessions.sh --json`` / ``codex-sessions.sh --json`` and re-parsed their stdout.
The launchers existed only to load ``.env`` and pick a Python; both are now the CLI's job,
so the Bolt listeners call :mod:`slackbridge.claude` and :mod:`slackbridge.codex` directly
— no subprocess, no JSON-scraping of another script's log lines.

Dispatch policy (unchanged, it is what makes a Slack reply work):

* ``<sid> <instruction>`` goes to Claude first — live session gets an injected keystroke,
  closed session gets ``claude --resume``;
* if the id is not a Claude session, Codex claims it (``codex exec resume``);
* a **bare id** returns that session's last response and runs nothing;
* ``<sid> stop|close|compact`` are control words, not instructions.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from slackbridge import claude, codex

STOP_WORDS = frozenset({"stop", "cancel", "detener", "parar", "cancelar"})
CLOSE_WORDS = frozenset({"close", "cerrar", "kill", "terminar", "quit", "exit"})
COMPACT_WORDS = frozenset({"compact", "compactar"})
COMPACT_CMD = "/compact"
"""The actual TUI command injected to compact a live session's context."""


def matches(row: dict[str, Any], query: str) -> bool:
    """Case-insensitive AND-match of a free-text query against title/id/project."""
    haystack = " ".join(str(row.get(k, "")) for k in ("title", "short", "sid", "project")).lower()
    return all(term in haystack for term in query.lower().split())


def list_all(
    *,
    project: Path | None = None,
    all_projects: bool = True,
    live_only: bool = False,
    engine: str = "all",
    limit: int = 10,
    query: str = "",
) -> list[dict[str, Any]]:
    """Merged Claude + Codex session rows, each tagged with ``engine``.

    ``live_only`` keeps just the sessions confirmed open in a terminal. Those are Claude
    rows with a ``pid`` (from ``claude agents``); Codex is excluded entirely because it
    publishes no process <-> session mapping, so "live" cannot be established for it.
    """
    rows: list[dict[str, Any]] = []
    if engine in ("all", "claude"):
        rows += claude.list_sessions(
            project=project,
            all_projects=all_projects,
            limit=limit,
            include_closed=not live_only,
        )
    if engine in ("all", "codex") and not live_only:
        rows += codex.list_sessions(project=project, all_projects=all_projects, limit=limit)
    if live_only:
        rows = [r for r in rows if r.get("pid")]
    if query:
        rows = [r for r in rows if matches(r, query)]
    return rows


def is_live(sid: str) -> bool:
    """True when the session has a live process (open in a terminal)."""
    full = claude.resolve_sid(sid)
    return bool(full and claude.live_pid(full))


def last_response(sid: str) -> str:
    """The session's current last answer, read-only and safe to poll. ``""`` when unknown."""
    return claude.last_response(sid) or ""


def stop(sid: str) -> str:
    """Interrupt the current generation (ESC). Only a live session can be interrupted."""
    full = claude.resolve_sid(sid)
    if not full:
        return f"No se encontró la sesión: '{sid}'"
    pid = claude.live_pid(full)
    if not pid:
        return f"La sesión {full[:8]} no está viva (no hay proceso que interrumpir)."
    ok, info = claude.interrupt_live(pid)
    return f"⏹️ Interrupción enviada a {full[:8]} ({info})" if ok else info


def close(sid: str) -> str:
    """Terminate the session's process (SIGTERM then SIGKILL)."""
    full = claude.resolve_sid(sid)
    if not full:
        return f"No se encontró la sesión: '{sid}'"
    pid = claude.live_pid(full)
    if not pid:
        return f"La sesión {full[:8]} no está viva (no hay proceso que cerrar)."
    ok, info = claude.close_live(pid)
    return f"🗙 Sesión {full[:8]} cerrada ({info})" if ok else info


def dispatch(text: str, *, wait_timeout: int = claude.DEFAULT_WAIT, fork: bool = False) -> str:
    """Route ``"<sid> [instruction]"`` and return the answer as text.

    Claude is tried first; exit code 2 ("not a Claude session") hands the id to Codex.
    """
    parts = text.split(None, 1)
    if not parts:
        return "Uso: `<sessionId> <instrucción>`"
    sid = parts[0]
    instruction = parts[1].strip() if len(parts) > 1 else ""

    if instruction:
        word = instruction.lower()
        if word in STOP_WORDS:
            return stop(sid)
        if word in CLOSE_WORDS:
            return close(sid)
        if word in COMPACT_WORDS:
            instruction = COMPACT_CMD  # inject the real TUI command

    code, out = claude.dispatch(sid, instruction, wait_timeout=wait_timeout, fork=fork)
    if code != 2:
        return out

    session, error = codex.resolve_session(sid)
    if not session:
        return error or out
    if not instruction:
        return session.last_response or "(no hay respuesta previa en esa sesión)"
    _, codex_out = codex.resume(session, instruction)
    return codex_out


def health() -> dict[str, Any]:
    """Liveness probe of both backends. No auth call, no token cost.

    ``claude_ok``/``codex_ok`` mean the CLI is installed and answered; ``live`` is the
    number of sessions currently open in a terminal. The watchdog alerts on
    ``claude_ok`` flipping false, which is what a post-suspend logout looks like.
    """
    return {
        "claude_ok": _cli_ok(["claude", "agents", "--json", "--all"]),
        "codex_ok": bool(shutil.which("codex")),
        "live": len(list_all(all_projects=True, live_only=True, engine="claude", limit=100)),
    }


def _cli_ok(command: list[str]) -> bool:
    if not shutil.which(command[0]):
        return False
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    try:
        json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    return True
