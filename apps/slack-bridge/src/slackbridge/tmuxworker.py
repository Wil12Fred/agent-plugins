"""A route's worker as an interactive Claude session in a **detached tmux session**.

The default worker (route ``terminal = "tmux"``): visible on demand —
``tmux attach -t <session>`` — without opening a window or stealing focus, and reachable by
``tmux send-keys``. The tmux session is named after the route's ``session`` key, and the
bridge records the Claude session id it runs in the alerts state file, so it can tell its
own worker from anything else that happens to carry the name.

Measured with Claude Code 2.1.332 and tmux 3.8 (2026-11-25), in a detached session:

* ``tmux new-session -d -s <name> -c <cwd> claude …`` runs claude as the pane's own process
  (``pane_pid`` *is* the claude pid); ``claude agents --json`` lists it as ``interactive``,
  ``idle``, about 1.2 s after the launch;
* a prompt given on the command line is submitted by Claude itself (``busy`` 0.6 s after
  launch), so a (re)launch carries its prompt and does not race the TUI's readiness;
* ``--resume <sid>`` in an interactive launch keeps the **same** session id;
* text sent with ``send-keys`` while claude is mid-turn is queued by Claude Code itself
  (``queue-operation`` enqueue/dequeue in the transcript) and answered after the turn;
* Shift+Tab changes the mode at once in the footer (``⏸ manual mode on``, ``⏵⏵ accept edits
  on``, ``⏸ plan mode on``, ``⏵⏵ auto mode on``, ``⏵⏵ don't ask on``) but the transcript only
  records it with the next prompt — and records ``manual`` as ``"default"``. So the live
  mode is read from the pane's footer, and the transcript is only the fallback.

Decisions, before every hand-over (:func:`plan`):

* no tmux session with the name -> **launch**: ``--resume <recorded sid>`` when one is
  recorded (its conversation continues), else ``--session-id <new uuid>``;
* a tmux session with the name that is not the recorded worker (no record, another session
  id, no claude in it) -> **conflict**: it is not ours, so it is neither typed into nor
  killed;
* ours, in the route's mode -> **inject** (also while busy: Claude queues it);
* ours, in another mode -> **relaunch**: ``tmux kill-session`` and launch with
  ``--resume``; while it is mid-turn it is left to finish (**busy**, the prompt waits in the
  bridge's queue).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from slackbridge import claude, worker
from slackbridge.routes import AlertRoute

FOOTER_MODES = (
    ("don't ask on", "dontAsk"),
    ("accept edits on", "acceptEdits"),
    ("plan mode on", "plan"),
    ("auto mode on", "auto"),
    ("manual mode on", "manual"),
    ("bypass permissions on", "bypassPermissions"),  # not measured: never cycled into
)
"""Footer text of each permission mode, as the TUI prints it."""
TRANSCRIPT_ALIASES = {"default": "manual"}
"""The transcript's name for a mode, where it differs from ``--permission-mode``'s."""
PANE_WIDTH, PANE_HEIGHT = "200", "50"
"""A detached pane is 80x24 by default; wide enough that the footer is never cut."""
TMUX_TIMEOUT = 10


def available() -> bool:
    return shutil.which("tmux") is not None


# Targets: `=name` is an exact session match (a bare name is a prefix match, so
# "worker" would hit "worker-2"). Pane commands need `=name:` — measured: `capture-pane -t
# =name` answers "can't find pane: =name", which read as "no footer" and hid a Shift+Tab.
def _tmux(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", *args], capture_output=True, text=True, timeout=TMUX_TIMEOUT, check=False
    )


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=TMUX_TIMEOUT, check=False)


@dataclass
class Tmux:
    """The four tmux operations the worker needs; replaced by a fake in tests."""

    def exists(self, name: str) -> bool:
        return _tmux("has-session", "-t", f"={name}").returncode == 0

    def pane_pid(self, name: str) -> int | None:
        out = _tmux("list-panes", "-t", f"={name}:", "-F", "#{pane_pid}").stdout.split()
        return int(out[0]) if out and out[0].isdigit() else None

    def capture(self, name: str) -> str:
        return _tmux("capture-pane", "-p", "-t", f"={name}:").stdout

    def kill(self, name: str) -> bool:
        return _tmux("kill-session", "-t", f"={name}").returncode == 0

    def launch(self, name: str, cwd: str, argv: list[str]) -> tuple[bool, str]:
        full = launch_argv(name, cwd, argv, scoped=_under_systemd())
        # Both paths go through a module-level runner the tests replace, so no unit test can
        # ever start a real tmux or claude.
        result = _tmux(*full[1:]) if full[0] == "tmux" else _run(full)
        return result.returncode == 0, (result.stderr or result.stdout).strip()


def _under_systemd() -> bool:
    """True when the bridge runs as a systemd unit (``INVOCATION_ID``) and can make a scope."""
    return bool(os.environ.get("INVOCATION_ID")) and shutil.which("systemd-run") is not None


def launch_argv(name: str, cwd: str, argv: list[str], *, scoped: bool) -> list[str]:
    """The command that starts the detached worker session.

    Measured: the bridge's unit has ``KillMode=control-group``, so a tmux server it spawns
    lives in its cgroup and every restart of the bridge killed the worker with it. Under
    systemd the launch goes through its own transient scope (``systemd-run --user
    --scope``), which outlives the unit. If a tmux server is already running elsewhere,
    ``new-session`` simply joins it and the scope holds only the short-lived client.
    """
    tmux = ["tmux", "new-session", "-d", "-s", name, "-x", PANE_WIDTH, "-y", PANE_HEIGHT]
    tmux += ["-c", cwd, *argv]
    if not scoped:
        return tmux
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect", *tmux]


def footer_mode(pane: str) -> str | None:
    """The mode the pane's footer shows, from its last matching line; ``None`` if none."""
    for line in reversed(pane.splitlines()):
        text = line.replace("’", "'")
        for marker, mode in FOOTER_MODES:
            if marker in text:
                return mode
    return None


def normalise(mode: str | None) -> str | None:
    return TRANSCRIPT_ALIASES.get(mode, mode) if mode else None


@dataclass(frozen=True)
class Plan:
    action: str
    """``launch`` | ``inject`` | ``relaunch`` | ``busy`` | ``conflict``."""
    reason: str
    pid: int | None = None
    resume: str | None = None
    """Recorded session id to ``--resume``; ``None`` = a fresh ``--session-id``."""
    namesakes: tuple[int, ...] = ()


def plan(
    route: AlertRoute,
    agents: list[dict[str, Any]],
    *,
    recorded: str | None,
    just_launched: bool = False,
    tmux: Tmux | None = None,
    transcript_mode: Callable[[worker.Worker], str | None] | None = None,
) -> Plan:
    """Decide how a prompt reaches the tmux worker of ``route``. Reads, never changes."""
    ops = tmux or Tmux()
    name = route.session
    if not ops.exists(name):
        if just_launched:
            return Plan("busy", "launched moments ago and not listed yet")
        why = f"no tmux session {name!r}" + (f": resuming {recorded}" if recorded else "")
        return Plan("launch", why, resume=recorded)
    pid = ops.pane_pid(name)
    agent = next((a for a in agents if pid and a.get("pid") == pid), None)
    sid = str((agent or {}).get("sessionId") or "")
    namesakes = tuple(
        int(a["pid"])
        for a in agents
        if a.get("name") == name and a.get("pid") and a.get("pid") != pid
    )
    if agent is None:
        if just_launched:
            return Plan("busy", "launched moments ago and not listed yet", pid)
        return Plan("conflict", f"tmux session {name!r} runs no Claude session (pane pid {pid})")
    if not recorded or sid != recorded:
        return Plan(
            "conflict",
            f"tmux session {name!r} runs session {sid or '?'}, not the recorded worker "
            f"{recorded or '(none recorded)'}: not ours, left alone",
            pid,
        )
    live = footer_mode(ops.capture(name))
    if live is None:
        cwd = str(agent.get("cwd") or route.cwd)
        w = worker.Worker(id=sid[:8], session_id=sid, cwd=cwd, alive=True, busy=False, state="")
        live = normalise((transcript_mode or (lambda x: worker.last_mode(worker.transcript(x))))(w))
    busy = agent.get("status") == "busy"
    if live != route.permission_mode:
        if busy:
            return Plan("busy", f"mode {live} != {route.permission_mode}, mid-turn", pid)
        return Plan(
            "relaunch",
            f"mode {live or 'unknown'} != {route.permission_mode}",
            pid,
            resume=recorded,
            namesakes=namesakes,
        )
    return Plan(
        "inject", f"live in mode {live}" + (", busy" if busy else ""), pid, namesakes=namesakes
    )


def command(
    route: AlertRoute,
    *,
    resume: str | None,
    prompt: str | None,
    has_transcript: Callable[[str], bool] | None = None,
) -> tuple[list[str], str]:
    """``(argv, session id)`` of the interactive claude a (re)launch runs.

    ``--resume`` only when the recorded session has a transcript on disk. Measured: a worker
    launched without a prompt and stopped before its first turn leaves no conversation, and
    ``claude --resume`` of it exits at once — the tmux session closes with it. Such a session
    is started fresh under the same id instead.
    """
    sid = resume or str(uuid.uuid4())
    exists = has_transcript or (lambda s: claude.find_session(s) is not None)
    argv = [claude.CLAUDE_BIN, *route.launch_flags()]
    argv += ["--resume", sid] if resume and exists(sid) else ["--session-id", sid]
    if prompt:
        argv.append(prompt)
    return argv, sid
