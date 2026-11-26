"""Shared fixtures: a fully-populated config that needs no environment and no network."""

from __future__ import annotations

from pathlib import Path

import pytest

from slackbridge.config import SlackConfig

HUMAN = "U06C51LCEAX"
BOT_USER = "U0AM2GP95GV"


def make_config(**overrides: object) -> SlackConfig:
    """A :class:`SlackConfig` with fake tokens, so nothing reads ``.env`` or the keyring."""
    base: dict[str, object] = {
        "bot_token": "xoxb-test",
        "app_token": "xapp-test",
        "user_token": None,
        "channel_id": "C_TARGET",
        "allowed_user_ids": frozenset({HUMAN}),
        "bot_user_id": BOT_USER,
        "private_user_id": HUMAN,
        "private_email": "wilber@example.com",
        "new_session_cwd": Path("/tmp"),
        "new_session_terminal": "auto",
        "watchdog_seconds": 600,
    }
    base.update(overrides)
    return SlackConfig(**base)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _isolated_items_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own items ledger: the default path is the operator's real one."""
    path = tmp_path / "state" / "items.json"
    monkeypatch.setenv("SLACKBRIDGE_ITEMS_FILE", str(path))
    return path


@pytest.fixture(autouse=True)
def _isolated_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Path:
    """No test reads the operator's real ``routes.toml``, no launch grace leaks across, and
    no unit test can start a real Claude session or touch the real tmux: a mutation that
    sent a test down the other worker path once ran the real ``claude --bg``."""
    from slackbridge import alerts, claude

    if request.node.get_closest_marker("integration") is None:
        monkeypatch.setattr(claude, "CLAUDE_BIN", str(tmp_path / "no-claude-in-unit-tests"))

    monkeypatch.delenv("SLACKBRIDGE_ROUTES_FILE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    alerts._launched.clear()

    class _NoTmux:
        def __getattr__(self, name: str) -> object:
            raise OSError("tests never drive the real tmux: replace alerts.tmux_ops")

    monkeypatch.setattr(alerts, "tmux_ops", _NoTmux)
    return tmp_path / "xdg-config" / "slackbridge" / "routes.toml"


@pytest.fixture
def config() -> SlackConfig:
    return make_config()
