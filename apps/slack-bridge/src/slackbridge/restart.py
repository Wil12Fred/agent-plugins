"""Restart a route's tmux worker safely: wait for idle, exit, stop, relaunch, verify.

Why a restart is needed at all: a Claude session reads ``--settings`` (its permission
allowlist) at launch, so a new tool for the worker only works after a restart.

Measured 2026-11-27 (Claude Code 2.1.332, tmux 3.8), each one a way a hand-made restart went
wrong — and what this module does about it:

* ``/exit`` in the worker's pane does **not** end the session: it keeps running in the
  **background** (``claude agents`` shows it ``idle`` with a pid) while the pane, whose own
  process was claude, closes and takes the tmux session with it. A relaunch with
  ``--resume <sid>`` then fails at once — *"That session is running in the background …
  stop it there first"* — and, launched as the pane's process, the tmux session vanishes
  with it, so the bridge logged "launch" for a worker that never ran. -> after ``/exit``
  the session is ``claude stop``-ped, and the relaunch is verified, never assumed.
* ``tmux set-option remain-on-exit`` on the *session* does not keep the pane: it is a
  window/pane option. -> nothing here relies on the pane surviving.
* a worker started by typing into a shell inside the tmux session has a shell as its pane
  process, and :func:`slackbridge.tmuxworker.plan` then calls it a **conflict** (not ours)
  and stops delivering to it. -> the relaunch goes through :meth:`Tmux.launch`, which makes
  claude the pane's own process; a stale tmux session whose pane runs no Claude is killed
  first, and only when it is the route's own name.
* a resumed session came back listed under another name (``support`` instead of
  ``support-worker``) although ``--name`` was passed. -> the name is checked and fixed with
  ``/rename``.
* a restart in the middle of a turn, or while a subagent runs, loses that work. -> it waits
  for ``idle`` and for a pane with no running subagent (``◯``) and no ``esc to interrupt``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from slackbridge import tmuxworker
from slackbridge.routes import AlertRoute

BUSY_MARKERS = ("esc to interrupt",)
SUBAGENT_RUNNING = "◯ "


@dataclass
class Ops:
    """Everything with a side effect, replaced in tests."""

    tmux: tmuxworker.Tmux
    agents: Callable[[], list[dict[str, Any]]]
    stop: Callable[[str], bool]
    send: Callable[[str, str], None]
    command: Callable[[AlertRoute, str], list[str]]
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    parent: Callable[[int], int | None] = lambda pid: _ppid(pid)


def _ppid(pid: int) -> int | None:
    """The parent pid of ``pid`` (Linux ``/proc``); ``None`` when unknown."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[1])
    except (OSError, IndexError, ValueError):
        return None


@dataclass
class Result:
    ok: bool
    steps: list[str] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "steps": self.steps, "error": self.error}


def _agent(agents: list[dict[str, Any]], sid: str) -> dict[str, Any] | None:
    return next((a for a in agents if a.get("sessionId") == sid and a.get("pid")), None)


def pane_busy(pane: str) -> bool:
    """True while the pane shows a turn in progress or a running subagent."""
    if any(m in pane for m in BUSY_MARKERS):
        return True
    return any(line.lstrip().startswith(SUBAGENT_RUNNING) for line in pane.splitlines())


def _gone_from_pane(ops: Ops, name: str, sid: str, pane_pid: int) -> bool:
    """The session no longer runs in (or under) the pane: /exit has taken effect."""
    if not ops.tmux.exists(name) or ops.tmux.pane_pid(name) != pane_pid:
        return True
    agent = _agent(ops.agents(), sid)
    return agent is None or (
        agent.get("pid") != pane_pid and ops.parent(int(agent["pid"])) != pane_pid
    )


def _wait(ops: Ops, until: Callable[[], bool], timeout: float, step: float = 2.0) -> bool:
    end = ops.clock() + timeout
    while True:
        if until():
            return True
        if ops.clock() >= end:
            return False
        ops.sleep(step)


def restart(
    route: AlertRoute, sid: str, ops: Ops, *, wait_idle: float = 0.0, settle: float = 30.0
) -> Result:
    """Restart the worker of ``route`` running session ``sid``; ``wait_idle`` = seconds to wait."""
    res = Result(ok=False)
    name = route.session

    def idle() -> bool:
        a = _agent(ops.agents(), sid)
        if a is not None and a.get("status") == "busy":
            return False
        return not (ops.tmux.exists(name) and pane_busy(ops.tmux.capture(name)))

    if not _wait(ops, idle, wait_idle):
        res.error = f"worker {name!r} is busy (a turn or a subagent is running); not restarted"
        return res
    res.steps.append("idle")

    pane_pid = ops.tmux.pane_pid(name) if ops.tmux.exists(name) else None
    agent = _agent(ops.agents(), sid)
    in_pane = bool(
        pane_pid
        and agent
        and (agent.get("pid") == pane_pid or ops.parent(int(agent["pid"])) == pane_pid)
    )
    if in_pane and pane_pid:
        # claude is the pane's process, or runs in the foreground of a shell in it (typed by
        # hand); `claude stop` does not end an interactive session, /exit does.
        ops.send(name, "/exit")
        _wait(ops, lambda: _gone_from_pane(ops, name, sid, pane_pid), 30)
        res.steps.append("exit sent")
    if _agent(ops.agents(), sid) is not None:
        ops.stop(sid)
        if not _wait(ops, lambda: _agent(ops.agents(), sid) is None, 30):
            res.error = f"session {sid} is still running after `claude stop`"
            return res
        res.steps.append("stopped")
    if ops.tmux.exists(name):
        # Our name, and no Claude session runs in it any more (checked above): a stale shell.
        ops.tmux.kill(name)
        res.steps.append("stale tmux session killed")

    ok, info = ops.tmux.launch(name, str(route.cwd), ops.command(route, sid))
    if not ok:
        res.error = f"tmux launch failed: {info}"
        return res
    res.steps.append("launched")

    def up() -> bool:
        pid = ops.tmux.pane_pid(name) if ops.tmux.exists(name) else None
        a = _agent(ops.agents(), sid)
        return bool(pid and a and a.get("pid") == pid)

    if not _wait(ops, up, settle):
        res.error = (
            f"relaunched but session {sid} is not running in tmux {name!r} "
            "(it may have exited at once: check `claude agents` and the transcript)"
        )
        return res
    res.steps.append("running")
    agent = _agent(ops.agents(), sid) or {}
    if agent.get("name") != name:
        ops.send(name, f"/rename {name}")
        res.steps.append(f"renamed from {agent.get('name')!r}")
    mode = tmuxworker.footer_mode(ops.tmux.capture(name))
    if mode and mode != route.permission_mode:
        res.error = f"running in mode {mode}, expected {route.permission_mode}"
        return res
    res.ok = True
    return res
