"""Restarting a tmux worker: each rule is a way a hand-made restart failed on 2026-11-27."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from slackbridge import restart
from slackbridge.routes import AlertRoute

SID = "11111111-2222-3333-4444-555555555555"
NAME = "support-worker"
FOOTER = "  ⏵⏵ don't ask on (shift+tab to cycle)"


class World:
    """A fake tmux + Claude that behaves as measured: /exit backgrounds the session."""

    def __init__(
        self, *, pane: str = FOOTER, pane_is_claude: bool = True, name: str = NAME
    ) -> None:
        self.session = True
        self.pane_pid: int | None = 100
        self.pane = pane
        self.agent: dict[str, Any] | None = (
            {"sessionId": SID, "pid": 100, "status": "idle", "name": name, "id": "abc"}
            if pane_is_claude
            else {"sessionId": SID, "pid": 555, "status": "idle", "name": name, "id": "abc"}
        )
        self.sent: list[str] = []
        self.killed = 0
        self.launched: list[list[str]] = []
        self.relaunch_dies = False
        self.relaunch_name = NAME
        self.t = 0.0
        self.child: int | None = None  # claude in the foreground of a shell pane

    # tmux
    def exists(self, _n: str) -> bool:
        return self.session

    def pane_pid_of(self, _n: str) -> int | None:
        return self.pane_pid if self.session else None

    def capture(self, _n: str) -> str:
        return self.pane

    def kill(self, _n: str) -> bool:
        self.session, self.killed = False, self.killed + 1
        return True

    def launch(self, _n: str, _cwd: str, argv: list[str]) -> tuple[bool, str]:
        self.launched.append(argv)
        if self.agent is not None or self.relaunch_dies:
            # "That session is running in the background": claude exits, the pane with it.
            self.session = False
            return True, ""
        self.session, self.pane_pid, self.pane = True, 200, FOOTER
        self.agent = {"sessionId": SID, "pid": 200, "status": "idle", "name": self.relaunch_name}
        return True, ""

    # claude
    def agents(self) -> list[dict[str, Any]]:
        return [self.agent] if self.agent else []

    def stop(self, _sid: str) -> bool:
        if self.agent and self.agent["pid"] == self.child:
            return True  # measured: `claude stop` does not end an interactive session
        self.agent = None
        return True

    def send(self, _n: str, text: str) -> None:
        self.sent.append(text)
        if text == "/exit" and self.agent and self.agent["pid"] == self.pane_pid:
            self.session = False  # the pane closes; the session keeps running in background
            self.agent = {**self.agent, "pid": 999}
        elif text == "/exit" and self.agent and self.agent["pid"] == self.child:
            self.agent = {**self.agent, "pid": 999}  # shell stays; session goes to background

    def ops(self) -> restart.Ops:
        tmux = type(
            "T",
            (),
            {
                "exists": lambda _s, n: self.exists(n),
                "pane_pid": lambda _s, n: self.pane_pid_of(n),
                "capture": lambda _s, n: self.capture(n),
                "kill": lambda _s, n: self.kill(n),
                "launch": lambda _s, n, c, a: self.launch(n, c, a),
            },
        )()
        return restart.Ops(
            tmux=tmux,  # type: ignore[arg-type]
            agents=self.agents,
            stop=self.stop,
            send=self.send,
            command=lambda _r, s: ["claude", "--resume", s],
            sleep=lambda s: setattr(self, "t", self.t + s),
            clock=lambda: self.t,
            parent=lambda pid: self.pane_pid if pid == self.child else 1,
        )


def route(tmp_path: Path) -> AlertRoute:
    return AlertRoute(
        name="r",
        channels=frozenset({"C1"}),
        trigger="*",
        session=NAME,
        cwd=tmp_path,
        instructions="x",
        permission_mode="dontAsk",
        setting_sources=("project",),
    )


def test_a_busy_worker_is_not_restarted(tmp_path: Path) -> None:
    """Rule: a turn in progress or a running subagent is never killed."""
    for pane in (
        FOOTER + "\n  ✻ Working… (esc to interrupt)",
        "  ● main\n  ◯ general-purpose  Revisando",
    ):
        w = World(pane=pane)
        res = restart.restart(route(tmp_path), SID, w.ops())
        assert not res.ok and "busy" in res.error
        assert w.sent == [] and w.launched == []


def test_exit_backgrounds_the_session_so_it_is_stopped_before_relaunch(tmp_path: Path) -> None:
    """Rule: after /exit the session lives on in background; resume only works once stopped."""
    w = World()
    res = restart.restart(route(tmp_path), SID, w.ops())
    assert res.ok, res.error
    assert w.sent[0] == "/exit" and "stopped" in res.steps
    assert w.launched == [["claude", "--resume", SID]]


def test_a_relaunch_that_dies_at_once_is_reported_not_assumed(tmp_path: Path) -> None:
    """Rule: "launched" is verified; the bridge once logged a launch that never ran."""
    w = World()
    w.relaunch_dies = True
    res = restart.restart(route(tmp_path), SID, w.ops(), settle=10)
    assert not res.ok and "not running" in res.error


def test_a_shell_pane_is_replaced_by_claude_as_the_pane_process(tmp_path: Path) -> None:
    """Rule: a worker typed into a shell looks like a conflict to the bridge; restart fixes it."""
    w = World(pane_is_claude=False)
    res = restart.restart(route(tmp_path), SID, w.ops())
    assert res.ok, res.error
    assert "/exit" not in w.sent  # never typed into a shell
    assert w.killed == 1 and w.pane_pid == 200 and w.agent and w.agent["pid"] == 200


def test_a_resumed_session_under_another_name_is_renamed(tmp_path: Path) -> None:
    """Rule: the worker must keep its name (it came back as 'support' once)."""
    w = World()
    w.relaunch_name = "support"
    res = restart.restart(route(tmp_path), SID, w.ops())
    assert res.ok and w.sent[-1] == f"/rename {NAME}"


def test_wait_lets_a_busy_worker_finish_first(tmp_path: Path) -> None:
    """Rule: --wait polls until idle instead of refusing."""
    w = World(pane="  ◯ general-purpose  Revisando")
    orig = w.ops()

    def sleep(s: float) -> None:
        w.t += s
        if w.t >= 10:
            w.pane = FOOTER

    ops = restart.Ops(
        orig.tmux, orig.agents, orig.stop, orig.send, orig.command, sleep, orig.clock, orig.parent
    )
    res = restart.restart(route(tmp_path), SID, ops, wait_idle=60)
    assert res.ok, res.error


def test_claude_in_the_foreground_of_a_shell_pane_gets_exit_then_stop(tmp_path: Path) -> None:
    """Rule: claude typed into a shell is a child of the pane; /exit it (stop alone fails)."""
    w = World(pane_is_claude=False)
    w.child = 555
    res = restart.restart(route(tmp_path), SID, w.ops())
    assert res.ok, res.error
    assert w.sent[0] == "/exit" and w.killed == 1 and w.agent and w.agent["pid"] == 200
