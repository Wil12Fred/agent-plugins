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


@pytest.fixture
def config() -> SlackConfig:
    return make_config()
