"""Configuration for the Slack <-> Claude/Codex bridge.

Everything here comes from the environment (the repo ``.env`` is exported once by
:func:`opscore.settings.load_env_file`). Tokens are resolved through
:mod:`opscore.secrets` and are **never** printed — :func:`opscore.secrets.redact`
is the only way a token may reach a human.

Two token identities exist and they are not interchangeable:

``SLACK_BOT_TOKEN`` (``xoxb-…``)
    The app's own identity. **This is the one the bridge must post with.** Reply
    processing separates the human's instructions from the bridge's own posts by
    ``bot_id``/``app_id``; posting with the user token makes the bridge's messages
    indistinguishable from the human's, so the parser re-ingests its own output and
    loops (see the OPER-714 regression covered in ``tests/test_access.py``).

``SLACK_TOKEN`` (``xoxp-…``)
    Wilber's user identity. Only used where the *user* must be the author (deleting
    the human's own messages — Slack lets each identity delete only what it wrote).

Other knobs:

``SLACK_CHANNEL_ID``     private session-control channel; the listener ignores everything else.
``SLACK_ALLOWED_USER_IDS`` allowlist of Slack **user ids** allowed to drive sessions.
``SLACK_PRIVATE_USER_ID`` fallback single-user allowlist + the ``<@id>`` mention prefix.
``SLACK_BOT_USER_ID``    the app's own user id, never allowlisted.
``SLACK_PRIVATE_EMAIL``  DM recipient when no channel id is configured.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from opscore.env import load_env_file
from opscore.errors import ConfigError
from opscore.secrets import resolve


def keyring_user_token() -> tuple[str, str]:
    """Where the xoxp user token lives in the OS keyring, as `(service, account)`.

    Configurable because the account name is whoever set it up: hardcoding one
    person's is how a tool stops working the moment somebody else installs it.

        secret-tool lookup service <SLACK_KEYRING_SERVICE> account <SLACK_KEYRING_ACCOUNT>
    """
    return (
        os.environ.get("SLACK_KEYRING_SERVICE", "slack-user-oauth"),
        os.environ.get("SLACK_KEYRING_ACCOUNT", os.environ.get("USER", "default")),
    )


REQUIRED_BOT_SCOPES = (
    "users:read.email",
    "im:write",
    "chat:write",
    "im:history",
)
"""Minimum scopes for the bridge: resolve a DM by email, open it, post, read it back."""


def _split_ids(raw: str) -> frozenset[str]:
    return frozenset(x.strip() for x in raw.replace(",", " ").split() if x.strip())


@dataclass(frozen=True)
class SlackConfig:
    """Resolved Slack bridge configuration. Tokens may be ``None`` when unset."""

    bot_token: str | None
    app_token: str | None
    user_token: str | None
    channel_id: str
    allowed_user_ids: frozenset[str]
    bot_user_id: str
    private_user_id: str
    private_email: str
    new_session_cwd: Path
    new_session_terminal: str
    watchdog_seconds: int

    def require_bot_token(self) -> str:
        """Return the bot token or explain, without leaking anything, that it is missing."""
        if not self.bot_token:
            raise ConfigError(
                "missing secret (Slack bot identity): set SLACK_BOT_TOKEN in .env",
                detail="the bridge must post as the app (xoxb-…); a user token makes its own "
                "posts look like yours and breaks reply parsing",
            )
        return self.bot_token

    def require_app_token(self) -> str:
        """Return the Socket Mode app-level token (``xapp-…``)."""
        if not self.app_token:
            raise ConfigError(
                "missing secret (Socket Mode): set SLACK_APP_TOKEN in .env",
                detail="Socket Mode needs an app-level token with connections:write",
            )
        return self.app_token

    def require_channel(self, override: str | None = None) -> str:
        """Return the target channel id, preferring an explicit override."""
        channel = (override or self.channel_id).strip()
        if not channel:
            raise ConfigError(
                "no Slack channel: pass --channel or set SLACK_CHANNEL_ID in .env",
            )
        return channel

    def mention(self) -> str:
        """``<@USERID> `` prefix so the human is notified even inside a thread."""
        return f"<@{self.private_user_id}> " if self.private_user_id else ""


def load() -> SlackConfig:
    """Resolve the bridge configuration from the environment and the repo ``.env``."""
    load_env_file()
    cwd = os.environ.get("NEW_SESSION_CWD") or os.environ.get("CODEX_SESSIONS_NEW_SESSION_CWD", "")
    return SlackConfig(
        bot_token=resolve(env_var="SLACK_BOT_TOKEN", required=False),
        app_token=resolve(env_var="SLACK_APP_TOKEN", required=False),
        user_token=resolve(env_var="SLACK_TOKEN", keyring=keyring_user_token(), required=False),
        channel_id=os.environ.get("SLACK_CHANNEL_ID", "").strip(),
        allowed_user_ids=_split_ids(
            os.environ.get("SLACK_ALLOWED_USER_IDS")
            or os.environ.get("SLACK_PRIVATE_USER_ID")
            or ""
        ),
        bot_user_id=os.environ.get("SLACK_BOT_USER_ID", "").strip(),
        private_user_id=os.environ.get("SLACK_PRIVATE_USER_ID", "").strip(),
        private_email=os.environ.get("SLACK_PRIVATE_EMAIL", "").strip(),
        new_session_cwd=Path(cwd).expanduser() if cwd else Path.cwd(),
        new_session_terminal=os.environ.get("NEW_SESSION_TERMINAL", "auto"),
        watchdog_seconds=int(os.environ.get("SLACK_WATCHDOG_SECONDS", "600")),
    )
