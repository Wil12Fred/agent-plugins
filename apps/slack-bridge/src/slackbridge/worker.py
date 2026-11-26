"""The background worker a route's alerts are handed to, and how a prompt reaches it.

An alert is never typed into an interactive terminal session: the operator may have changed
that session's permission mode (Shift+Tab), and a session that stops on a permission prompt
nobody is watching is an alert nobody answers. Each route instead owns a **background**
session (``claude --bg``) named after its ``session`` key, and before every hand-over the
bridge checks that the worker is in the route's mode.

How ``claude --bg`` behaves, measured with Claude Code 2.1.332 (2026-11-25), and what this
module does about each fact:

* ``claude --bg --resume <sid> <prompt>`` while that session's process is alive — idle or
  busy — does not deliver: *"session … is already running in the background, so this
  started a copy as …"*. So a live worker is never resumed directly:

  - **busy** -> the prompt is queued (persisted) and delivered once it is idle;
  - **idle** -> ``claude stop <id>`` first, then the wake below.

* on a stopped session, **any** flag (even the same ones it was started with) starts a copy:
  *"keeps its own saved options, so the flags you passed started a copy"*. Without flags it
  continues the same id: *"woke session … with its saved options (--name,
  --permission-mode, --permission-prompts, --setting-sources, --settings, --model)"*. So a
  worker already in the right mode is woken with **no flags**.
* a short id passed to ``--resume`` also starts a copy; the full ``sessionId`` is used.
* a copy carries the original's conversation. That is how a worker in the **wrong** mode is
  relaunched: ``claude --bg <route flags> --resume <sid> <prompt>`` (after stopping it if
  alive) — same history, new id, the route's mode. The newest background session with the
  name is the worker from then on.
* a stopped background session stays listed by ``claude agents --json --all`` without a
  ``pid``; a live one has a ``pid`` and ``status`` ``idle``/``busy``.

The current mode is the last ``"permissionMode"`` recorded in the worker's transcript,
``<CLAUDE_HOME>/projects/<slug of cwd>/<sessionId>.jsonl`` — the slug replaces every
character that is not a letter or digit with ``-`` (measured: ``/home/u/.cache/x`` ->
``-home-u--cache-x``). The other launch options are compared with the ``respawnFlags`` that
Claude Code saves in ``<CLAUDE_HOME>/jobs/<id>/state.json``, when that file is readable; it
is not a documented interface, so when it is missing only the mode is compared.
An unreadable mode is treated as wrong: relaunching costs a copy, guessing costs an alert.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from slackbridge import claude
from slackbridge.routes import AlertRoute

MODE_RE = re.compile(rb'"permissionMode"\s*:\s*"([^"]+)"')
TAIL_CHUNK = 256 * 1024
COMPARED_FLAGS = ("--permission-mode", "--permission-prompts", "--setting-sources", "--settings")
"""Launch options whose saved value must match the route's (``--name`` is how it is found)."""


@dataclass(frozen=True)
class Worker:
    """One background session carrying the route's session name."""

    id: str
    session_id: str
    cwd: str
    alive: bool
    busy: bool
    state: str


@dataclass(frozen=True)
class Plan:
    """What to do before handing a prompt to a route's worker, and why."""

    action: str
    """``new`` | ``relaunch`` | ``wake`` | ``busy``."""
    reason: str
    worker: Worker | None = None
    resume: str | None = None
    """Full session id to pass to ``--resume``, or ``None`` for a fresh session."""
    stop: str | None = None
    """Short id to ``claude stop`` first (a live, idle worker), or ``None``."""
    namesakes: tuple[int, ...] = ()
    """Pids of interactive sessions with the same name: never used, only reported."""

    def command(self, route: AlertRoute, prompt: str) -> list[str]:
        """The ``claude --bg`` command that carries out this plan."""
        if self.action == "wake" and self.resume:
            return [claude.CLAUDE_BIN, "--bg", "--resume", self.resume, prompt]
        command = [claude.CLAUDE_BIN, "--bg", *route.launch_flags()]
        if self.resume:
            command += ["--resume", self.resume]
        return [*command, prompt]


def project_slug(cwd: str) -> str:
    """Claude Code's project folder name for ``cwd``: non-alphanumerics become ``-``."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def transcript(worker: Worker, projects: Path | None = None) -> Path | None:
    """The worker's transcript: under its cwd's slug, else wherever it is found."""
    base = projects if projects is not None else claude.PROJECTS
    direct = base / project_slug(worker.cwd) / f"{worker.session_id}.jsonl"
    return direct if direct.is_file() else claude.find_session(worker.session_id)


def last_mode(path: Path | None) -> str | None:
    """The last ``permissionMode`` recorded in ``path``, read from the end; ``None`` if none."""
    if path is None:
        return None
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            end = handle.tell()
            pos, carry = end, b""
            while pos > 0:
                step = min(TAIL_CHUNK, pos)
                pos -= step
                handle.seek(pos)
                block = handle.read(step) + carry
                found = MODE_RE.findall(block)
                if found:
                    return str(found[-1].decode())
                carry = block[:200]  # a match split across the chunk boundary
    except OSError:
        return None
    return None


def saved_flags(worker: Worker, home: Path | None = None) -> dict[str, str] | None:
    """The compared options the worker was launched with, from its ``respawnFlags``."""
    base = home if home is not None else claude.CLAUDE_HOME
    try:
        data = json.loads((base / "jobs" / worker.id / "state.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None
    flags = data.get("respawnFlags") if isinstance(data, dict) else None
    if not isinstance(flags, list):
        return None
    out: dict[str, str] = {}
    for i, flag in enumerate(flags):
        if flag in COMPARED_FLAGS and i + 1 < len(flags):
            out[str(flag)] = str(flags[i + 1])
    return out


def _started(agent: dict[str, Any]) -> float:
    try:
        return float(agent.get("startedAt") or 0)
    except (TypeError, ValueError):
        return 0.0


def find(name: str, agents: list[dict[str, Any]]) -> tuple[Worker | None, tuple[int, ...]]:
    """The newest background session named ``name``, and the pids of interactive namesakes."""
    named = sorted((a for a in agents if a.get("name") == name), key=_started, reverse=True)
    namesakes = tuple(
        int(a["pid"]) for a in named if a.get("kind") == "interactive" and a.get("pid")
    )
    for agent in named:
        if agent.get("kind") == "background" and agent.get("sessionId"):
            return (
                Worker(
                    id=str(agent.get("id") or str(agent["sessionId"])[:8]),
                    session_id=str(agent["sessionId"]),
                    cwd=str(agent.get("cwd") or ""),
                    alive=bool(agent.get("pid")),
                    busy=agent.get("status") == "busy",
                    state=str(agent.get("state") or ""),
                ),
                namesakes,
            )
    return None, namesakes


def plan(
    route: AlertRoute,
    agents: list[dict[str, Any]],
    *,
    remembered: str | None = None,
    just_launched: bool = False,
    mode_of: Callable[[Worker], str | None] | None = None,
    flags_of: Callable[[Worker], dict[str, str] | None] | None = None,
) -> Plan:
    """Decide how a prompt reaches ``route``'s worker. Pure apart from the two readers.

    ``remembered`` is the last worker session id the bridge saw, used when ``claude agents``
    no longer lists one (a copy keeps the conversation). ``just_launched`` covers the
    seconds after the bridge itself started a turn, before ``claude agents`` reports it busy.
    """
    worker, namesakes = find(route.session, agents)
    if just_launched and (worker is None or (worker.alive and worker.state != "done")):
        # The bridge started a turn seconds ago and `claude agents` may not show it yet:
        # a second launch now would start a second worker, a stop would kill the turn.
        return Plan("busy", "a turn was launched moments ago", worker, namesakes=namesakes)
    if worker is None:
        return Plan(
            "new",
            f"no background session named {route.session!r}",
            resume=remembered,
            namesakes=namesakes,
        )
    if worker.busy:
        return Plan("busy", f"worker {worker.id} is busy", worker, namesakes=namesakes)
    mode = (mode_of or (lambda w: last_mode(transcript(w))))(worker)
    saved = (flags_of or saved_flags)(worker)
    want = route.launch_options()
    stop = worker.id if worker.alive else None
    wrong: list[str] = []
    if mode != route.permission_mode:
        wrong.append(f"mode {mode or 'unknown'} != {route.permission_mode}")
    if saved is not None:
        for flag in COMPARED_FLAGS[1:]:
            if saved.get(flag) != want.get(flag):
                wrong.append(f"{flag} {saved.get(flag)} != {want.get(flag)}")
    if wrong:
        return Plan(
            "relaunch",
            f"worker {worker.id}: " + "; ".join(wrong),
            worker,
            resume=worker.session_id,
            stop=stop,
            namesakes=namesakes,
        )
    return Plan(
        "wake",
        f"worker {worker.id} in mode {mode}" + (", idle: stopped first" if stop else ""),
        worker,
        resume=worker.session_id,
        stop=stop,
        namesakes=namesakes,
    )
