"""Who is allowed to drive a Claude/Codex session from Slack.

Access control is an **allowlist by Slack user id** (``SLACK_ALLOWED_USER_IDS``, comma or
space separated; ``SLACK_PRIVATE_USER_ID`` is accepted as a single-user fallback). Anyone
not on the list is ignored — the bridge can run arbitrary local commands through the
Claude CLI, so this is the security boundary, not a convenience filter.

Two independent guards keep the bridge from answering itself, and both are needed:

1. :func:`is_bridge_post` drops anything carrying ``bot_id``/``app_id``/``subtype=bot_message``.
   This is **config-independent**: it holds even when the allowlist env vars are empty in
   the listener process, where :func:`is_allowed` would otherwise fail open.
2. the allowlist itself never contains ``SLACK_BOT_USER_ID``.

Regression that motivates guard 1 (OPER-714): the app posted a completion notice as
itself; the allowlist was empty in that process, ``is_allowed`` failed open, and the
notice was ingested as a new top-level request — every answer spawned another session.
"""

from __future__ import annotations

from typing import Any

from slackbridge.config import SlackConfig


def is_allowed(config: SlackConfig, user_id: str | None) -> bool:
    """True when ``user_id`` may drive sessions.

    An empty allowlist means "open" for backwards compatibility with the original
    single-user setup — set ``SLACK_ALLOWED_USER_IDS`` to lock the bridge down.
    """
    if not user_id or user_id == config.bot_user_id:
        return False
    if not config.allowed_user_ids:
        return True
    return user_id in config.allowed_user_ids


def is_bridge_post(message: dict[str, Any]) -> bool:
    """True when the message was posted by an app/bot — including this bridge itself."""
    return bool(
        message.get("bot_id") or message.get("app_id") or message.get("subtype") == "bot_message"
    )


def is_human_instruction(config: SlackConfig, message: dict[str, Any]) -> bool:
    """True when a message counts as an actionable instruction from an allowed human."""
    if is_bridge_post(message):
        return False
    return is_allowed(config, message.get("user"))


def human_texts(config: SlackConfig, messages: list[dict[str, Any]]) -> list[str]:
    """Non-empty texts of the allowed humans in ``messages``, de-duplicated in order."""
    texts = [
        (m.get("text") or "").strip()
        for m in messages
        if is_human_instruction(config, m) and (m.get("text") or "").strip()
    ]
    return list(dict.fromkeys(texts))
