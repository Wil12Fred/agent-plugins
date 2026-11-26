"""The tmux worker: a visible, attachable Claude session the bridge owns and keeps in mode.

The failure this exists for: an alert typed into a session whose mode the operator changed
with Shift+Tab, which then stops on a permission prompt nobody sees. And its mirror image:
the bridge typing into, or killing, a session that merely shares the worker's name. Both
directions are pinned here, against a fake tmux; the real launch was measured by hand (see
the module docstring of :mod:`slackbridge.tmuxworker`).
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import make_config
from opscore.errors import ConfigError

from slackbridge import alerts, claude, items, routes, tmuxworker
from slackbridge.config import load
from slackbridge.routes import AlertRoute

CHANNEL = "C0ALERTS"
LOG = logging.getLogger("test-tmux")
SID = "11111111-2222-3333-4444-555555555555"
PID = 4321
FOOTER = {
    "dontAsk": "  ⏵⏵ don't ask on (shift+tab to cycle) · ← 2 agents",
    "manual": "  ⏸ manual mode on · ← 2 agents",
    "acceptEdits": "  ⏵⏵ accept edits on (shift+tab to cycle) · ← 2 agents",
    "plan": "  ⏸ plan mode on (shift+tab to cycle) · ← 2 agents",
    "auto": "  ⏵⏵ auto mode on (shift+tab to cycle) · ← 2 agents",
}


class FakeTmux:
    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.launched: list[tuple[str, str, list[str]]] = []
        self.killed: list[str] = []

    def exists(self, name: str) -> bool:
        return name in self.sessions

    def pane_pid(self, name: str) -> int | None:
        return self.sessions[name]["pid"]

    def capture(self, name: str) -> str:
        return "\n".join(["❯ ", "─" * 20, self.sessions[name]["footer"]])

    def kill(self, name: str) -> bool:
        self.killed.append(name)
        return self.sessions.pop(name, None) is not None

    def launch(self, name: str, cwd: str, argv: list[str]) -> tuple[bool, str]:
        self.launched.append((name, cwd, argv))
        return True, ""


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    fake = FakeTmux()
    seen: dict[str, Any] = {"tmux": fake, "agents": [], "injected": []}
    monkeypatch.setattr(alerts, "tmux_ops", lambda: fake)
    monkeypatch.setattr(claude, "agents", lambda: list(seen["agents"]))
    monkeypatch.setattr(
        claude,
        "send_live",
        lambda pid, msg: (seen["injected"].append((pid, msg)), (True, "tmux:worker"))[1],
    )
    route = AlertRoute(
        name="r",
        channels=frozenset({CHANNEL}),
        trigger="URGENT ALERT",
        session="support-worker",
        cwd=tmp_path,
        instructions="Haz X.",
        permission_mode="dontAsk",
        setting_sources=("project",),
    )
    seen["config"] = make_config(alert_routes=(route,))
    seen["store"] = alerts.AlertStore(tmp_path / "alerts.json")
    return seen


def ours(env: dict[str, Any], mode: str, status: str = "idle") -> None:
    """The recorded worker is running in its tmux session, footer showing ``mode``."""
    env["store"].remember_worker("support-worker", SID, tmux="support-worker")
    env["tmux"].sessions["support-worker"] = {"pid": PID, "footer": FOOTER[mode]}
    env["agents"] = [
        {
            "pid": PID,
            "kind": "interactive",
            "name": "support-worker",
            "sessionId": SID,
            "status": status,
            "cwd": "/w",
        }
    ]


def dispatch(env: dict[str, Any], at: str = "1795564509.1") -> str:
    event = alerts.classify(
        {"channel": CHANNEL, "ts": at, "user": "U0X", "text": "URGENT ALERT\nTicket: ABC-1"},
        env["config"],
        env["store"],
    )
    assert event is not None
    return alerts.dispatch(event, env["config"], env["store"], LOG)


def flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def test_no_worker_is_launched_in_a_detached_tmux_session_with_the_route_flags(
    env: dict[str, Any], tmp_path: Path
) -> None:
    """Rule: the worker runs where the operator can attach to it, in the route's mode, and
    carries its first prompt on the command line (measured: Claude submits it itself)."""
    assert dispatch(env) == "launch"
    [(name, cwd, argv)] = env["tmux"].launched
    assert (name, cwd) == ("support-worker", str(tmp_path))
    assert argv[0] == claude.CLAUDE_BIN and flag(argv, "--name") == "support-worker"
    assert flag(argv, "--permission-mode") == "dontAsk"
    assert flag(argv, "--permission-prompts") == "none"
    assert flag(argv, "--setting-sources") == "project"
    assert "--resume" not in argv and "ABC-1" in argv[-1]
    record = env["store"].worker_record("support-worker")
    assert record == {"sid": flag(argv, "--session-id"), "tmux": "support-worker"}
    assert env["injected"] == []


def test_a_recorded_worker_that_is_not_running_is_resumed(env: dict[str, Any]) -> None:
    """Rule: a relaunch keeps the conversation — `--resume <recorded sid>`."""
    env["store"].remember_worker("support-worker", SID, tmux="support-worker")
    assert dispatch(env) == "launch"
    [(_, _, argv)] = env["tmux"].launched
    assert flag(argv, "--resume") == SID and "--session-id" not in argv


@pytest.mark.parametrize("status", ["idle", "busy"])
def test_our_worker_in_the_route_mode_gets_the_prompt_typed_in(
    env: dict[str, Any], status: str
) -> None:
    """Rule: our worker in mode is injected into — also mid-turn, because Claude queues what
    arrives while it works (measured: queue-operation enqueue/dequeue)."""
    ours(env, "dontAsk", status)
    assert dispatch(env) == "inject"
    [(pid, prompt)] = env["injected"]
    assert pid == PID and "ABC-1" in prompt
    assert env["tmux"].launched == [] and env["tmux"].killed == []


@pytest.mark.parametrize("mode", ["manual", "acceptEdits", "plan", "auto"])
def test_a_shift_tabbed_worker_is_killed_and_resumed_in_the_route_mode(
    env: dict[str, Any], mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule: the footer is the live mode (measured: the transcript only learns of a Shift+Tab
    with the next prompt), so a stale transcript saying dontAsk must not win."""
    monkeypatch.setattr(tmuxworker.worker, "last_mode", lambda path: "dontAsk")
    ours(env, mode)
    assert dispatch(env) == "relaunch"
    assert env["tmux"].killed == ["support-worker"] and env["injected"] == []
    [(_, _, argv)] = env["tmux"].launched
    assert flag(argv, "--resume") == SID and flag(argv, "--permission-mode") == "dontAsk"


def test_a_wrong_mode_worker_mid_turn_is_not_killed(env: dict[str, Any]) -> None:
    """Rule: killing it would lose the turn; the prompt waits in the bridge's queue."""
    ours(env, "manual", "busy")
    assert dispatch(env) == "queued"
    assert env["tmux"].killed == [] and env["tmux"].launched == [] and env["injected"] == []
    assert len(env["store"].pending("support-worker")) == 1


def test_the_transcript_is_the_fallback_when_the_footer_shows_no_mode(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    ours(env, "dontAsk")
    env["tmux"].sessions["support-worker"]["footer"] = "(no footer)"
    monkeypatch.setattr(tmuxworker.worker, "last_mode", lambda path: "default")
    monkeypatch.setattr(tmuxworker.worker, "transcript", lambda w, projects=None: None)
    assert dispatch(env) == "relaunch"  # "default" is the transcript's name for manual


@pytest.mark.parametrize(
    "record",
    [None, {"sid": "99999999-0000-0000-0000-000000000000", "tmux": "support-worker"}],
)
def test_a_tmux_session_that_is_not_the_recorded_worker_is_left_alone(
    env: dict[str, Any], record: dict[str, str] | None
) -> None:
    """Rule: only the pid whose tmux session AND session id match the recorded worker is
    ours. Anything else with the name is neither typed into nor killed."""
    ours(env, "manual")
    env["store"]._workers.clear()
    if record:
        env["store"].remember_worker("support-worker", record["sid"], tmux=record["tmux"])
    assert dispatch(env) == "error"
    assert env["tmux"].killed == [] and env["tmux"].launched == [] and env["injected"] == []
    entry = items.ItemLedger().get(f"{CHANNEL}:1795564509.1")
    assert entry is not None and entry["status"] == items.DISPATCH_ERROR


def test_an_interactive_namesake_outside_tmux_is_never_typed_into(env: dict[str, Any]) -> None:
    env["agents"] = [
        {"pid": 777, "kind": "interactive", "name": "support-worker", "sessionId": "other"}
    ]
    assert dispatch(env) == "launch"
    assert env["injected"] == []


def test_a_launch_moments_ago_queues_instead_of_launching_twice(env: dict[str, Any]) -> None:
    assert dispatch(env, "1795564509.1") == "launch"
    assert dispatch(env, "1795564509.2") == "queued"
    assert len(env["tmux"].launched) == 1


def test_on_start_a_missing_worker_is_launched_without_a_prompt(env: dict[str, Any]) -> None:
    """Rule: after the listener connects the worker is ensured first; the catch-up follows."""
    assert alerts.ensure_workers(env["config"], env["store"], LOG) == {"support-worker": "launch"}
    [(_, _, argv)] = env["tmux"].launched
    assert argv[-2] == "--session-id"  # nothing after the id: no prompt
    alerts._launched.clear()
    ours(env, "dontAsk")
    env["tmux"].launched.clear()
    assert alerts.ensure_workers(env["config"], env["store"], LOG) == {"support-worker": "inject"}
    assert env["tmux"].launched == []  # a running worker is left as it is


def test_the_queue_is_injected_once_the_launch_is_listed(env: dict[str, Any]) -> None:
    dispatch(env, "1795564509.1")
    dispatch(env, "1795564509.2")  # queued during the grace
    sid = env["store"].worker_session("support-worker")
    alerts._launched["support-worker"] = time.monotonic() - alerts.LAUNCH_GRACE - 1
    ours(env, "dontAsk")
    env["store"].remember_worker("support-worker", sid, tmux="support-worker")
    env["agents"][0]["sessionId"] = sid
    assert alerts.drain(env["config"], env["store"], LOG) == {"support-worker": "inject"}
    [(_, prompt)] = env["injected"]
    assert "1795564509.2" in prompt


@pytest.mark.parametrize(("mode", "line"), sorted(FOOTER.items()))
def test_every_measured_footer_reads_as_its_mode(mode: str, line: str) -> None:
    assert tmuxworker.footer_mode("x\n" + line + "\n") == mode
    assert tmuxworker.footer_mode("no mode here") is None


# --- configuration ---------------------------------------------------------------
def _toml(tmp_path: Path, **keys: str) -> str:
    lines = [
        "[[route]]",
        'name = "r"',
        'channels = ["C0A"]',
        'trigger = "X"',
        'instructions = "y"',
        f'cwd = "{tmp_path}"',
    ]
    keys.setdefault("session", "support-worker")
    lines += [f'{k} = "{v}"' for k, v in keys.items()]
    return "\n".join(lines) + "\n"


def test_terminal_defaults_to_tmux_and_only_takes_tmux_or_background(tmp_path: Path) -> None:
    assert routes.parse(_toml(tmp_path), "f").routes[0].terminal == "tmux"
    bg = routes.parse(_toml(tmp_path, terminal="background"), "f").routes[0]
    assert bg.terminal == "background"
    for bad in ("vscode", "konsole", ""):
        with pytest.raises(ConfigError, match="'terminal'"):
            routes.parse(_toml(tmp_path, terminal=bad), "f")


def test_a_tmux_worker_needs_tmux_and_a_name_tmux_can_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule: refuse at startup rather than fail at the first alert."""
    with pytest.raises(ConfigError, match="tmux cannot name"):
        routes.parse(_toml(tmp_path, session="a.b"), "f")
    assert routes.parse(_toml(tmp_path, session="a.b", terminal="background"), "f").routes
    monkeypatch.setattr(routes.shutil, "which", lambda _b: None)
    with pytest.raises(ConfigError, match="tmux is not installed"):
        routes.parse(_toml(tmp_path), "f")


def test_the_env_route_takes_its_terminal_from_slack_alerts_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for key in list(os.environ):
        if key.startswith("SLACK_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("OPSCORE_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setenv("SLACK_ALERTS_CHANNEL_ID", "C0A")
    monkeypatch.setenv("SLACK_ALERTS_TRIGGER", "X")
    monkeypatch.setenv("SLACK_ALERTS_SESSION_NAME", "w")
    assert load().routes()[0].terminal == "tmux"
    monkeypatch.setenv("SLACK_ALERTS_TERMINAL", "background")
    assert load().routes()[0].terminal == "background"
    monkeypatch.setenv("SLACK_ALERTS_TERMINAL", "vscode")
    with pytest.raises(ConfigError, match="SLACK_ALERTS_TERMINAL"):
        load()


def test_tmux_targets_are_exact_and_pane_commands_name_a_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule (measured live): a bare name is a prefix match, so `=name` is used; and pane
    commands need `=name:` — `capture-pane -t =name` fails, which read as "no footer" and
    hid a Shift+Tab until the transcript caught up."""
    calls: list[tuple[str, ...]] = []

    def fake(*args: str) -> Any:
        calls.append(args)
        return type("R", (), {"returncode": 0, "stdout": "123\n", "stderr": ""})()

    monkeypatch.setattr(tmuxworker, "_tmux", fake)
    ops = tmuxworker.Tmux()
    ops.exists("w")
    ops.pane_pid("w")
    ops.capture("w")
    ops.kill("w")
    ops.launch("w", "/tmp", ["claude"])
    targets = {c[0]: c[c.index("-t") + 1] for c in calls if "-t" in c}
    assert targets == {
        "has-session": "=w",
        "list-panes": "=w:",
        "capture-pane": "=w:",
        "kill-session": "=w",
    }
    [launch] = [c for c in calls if c[0] == "new-session"]
    assert launch[:4] == ("new-session", "-d", "-s", "w") and launch[-1] == "claude"


def test_the_transcript_default_mode_is_manual(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Rule (measured): Shift+Tab to "manual" is recorded in the transcript as "default"; a
    manual route must not relaunch its worker forever over the spelling."""
    route = AlertRoute(
        name="r",
        channels=frozenset({CHANNEL}),
        trigger="URGENT ALERT",
        session="support-worker",
        cwd=tmp_path,
        instructions="Haz X.",
        permission_mode="manual",
    )
    env["config"] = make_config(alert_routes=(route,))
    ours(env, "dontAsk")
    env["tmux"].sessions["support-worker"]["footer"] = "(no footer)"
    monkeypatch.setattr(tmuxworker.worker, "last_mode", lambda path: "default")
    monkeypatch.setattr(tmuxworker.worker, "transcript", lambda w, projects=None: None)
    assert dispatch(env) == "inject"
