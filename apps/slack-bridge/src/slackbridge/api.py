"""Thin Slack Web API wrapper used by every command in this package.

The three legacy scripts (``list.py``, ``codex_sessions.py``, ``clean_channel.py``) each
carried their own copy of ``_slack_api`` / ``slack_token`` / ``slack_dm_channel`` built on
``urllib``. They are unified here on ``slack_sdk.WebClient``, which already handles
retries, rate-limit headers and pagination cursors.

Identity rules (load-bearing, see :mod:`slackbridge.config`):

* the bridge posts as the **bot** (``SLACK_BOT_TOKEN``, ``xoxb-…``) so its own messages
  carry a ``bot_id`` and can be filtered out of the human's instructions;
* the **user** token (``SLACK_TOKEN``, ``xoxp-…``) is only used where Slack requires the
  original author — ``chat.delete`` can only remove messages the calling identity wrote.

Nothing in this module prints or returns a token.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from slackbridge.config import SlackConfig
from slackbridge.core.errors import ApiError, ConfigError

_HISTORY_PAGE = 200
_REPLY_PAGE = 200


class SlackAPI:
    """Slack Web API calls the bridge needs, bound to one token identity."""

    def __init__(self, token: str, *, identity: str = "bot") -> None:
        """Args:
        token: an ``xoxb-``/``xoxp-`` token. Never logged.
        identity: ``"bot"`` or ``"user"``, used only in error messages.
        """
        self._client = WebClient(token=token)
        self.identity = identity

    @classmethod
    def bot(cls, config: SlackConfig) -> SlackAPI:
        """Client posting as the app. This is the correct identity for the bridge."""
        return cls(config.require_bot_token(), identity="bot")

    @classmethod
    def user(cls, config: SlackConfig) -> SlackAPI:
        """Client acting as the human. Only for deleting the human's own messages."""
        if not config.user_token:
            raise ConfigError("missing secret (Slack user identity): set SLACK_TOKEN in .env")
        return cls(config.user_token, identity="user")

    @property
    def client(self) -> WebClient:
        """The underlying ``WebClient`` (the Bolt listeners want the raw object)."""
        return self._client

    # --- reads -------------------------------------------------------------
    def history(self, channel: str, *, limit: int = _HISTORY_PAGE) -> list[dict[str, Any]]:
        """Top-level messages of ``channel``, newest first (one page)."""
        return self._messages("conversations_history", channel=channel, limit=limit)

    def iter_history(self, channel: str) -> Iterator[dict[str, Any]]:
        """Every top-level message of ``channel``, following the pagination cursor."""
        cursor: str | None = None
        while True:
            resp = self._call(
                "conversations_history",
                channel=channel,
                limit=_HISTORY_PAGE,
                **({"cursor": cursor} if cursor else {}),
            )
            yield from resp.get("messages", [])
            cursor = (resp.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                return

    def replies(self, channel: str, ts: str, *, limit: int = _REPLY_PAGE) -> list[dict[str, Any]]:
        """A thread's messages. Index 0 is the parent; the rest are the replies."""
        return self._messages("conversations_replies", channel=channel, ts=ts, limit=limit)

    def dm_channel(self, *, email: str = "", user_id: str = "") -> str:
        """Resolve a DM channel id from a user id, or from an email as a last resort.

        ``users.lookupByEmail`` needs ``users:read.email``; passing ``user_id``
        (``SLACK_PRIVATE_USER_ID``) skips that scope entirely.
        """
        uid = user_id
        if not uid:
            if not email:
                raise ConfigError("no Slack recipient: set SLACK_PRIVATE_USER_ID or pass an email")
            uid = self._call("users_lookupByEmail", email=email)["user"]["id"]
        return str(self._call("conversations_open", users=uid)["channel"]["id"])

    # --- writes ------------------------------------------------------------
    def post(
        self,
        channel: str,
        text: str,
        *,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> str:
        """Post a message; returns its ``ts``. ``thread_ts`` keeps it inside a thread."""
        kwargs: dict[str, Any] = {"channel": channel, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        if blocks:
            kwargs["blocks"] = blocks
        return str(self._call("chat_postMessage", **kwargs)["ts"])

    def delete(self, channel: str, ts: str) -> bool:
        """Delete one message. ``True`` when it is gone (already-deleted counts as gone).

        Only the identity that authored a message may delete it, which is why the
        purge path tries the bot client and then the user client.
        """
        try:
            self._call("chat_delete", channel=channel, ts=ts)
        except ApiError as exc:
            return "message_not_found" in exc.message
        return True

    # --- plumbing ----------------------------------------------------------
    def _messages(self, method: str, **kwargs: Any) -> list[dict[str, Any]]:
        return list(self._call(method, **kwargs).get("messages", []))

    def _call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = getattr(self._client, method)(**kwargs)
        except SlackApiError as exc:
            error = (exc.response or {}).get("error", str(exc))
            raise ApiError(f"{method}: {error}") from exc
        data = response.data
        if isinstance(data, dict) and not data.get("ok", True):
            raise ApiError(f"{method}: {data.get('error')}")
        return dict(data) if isinstance(data, dict) else {}
