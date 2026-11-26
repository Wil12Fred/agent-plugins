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
    loops (see the reply-loop regression covered in ``tests/test_access.py``).

``SLACK_TOKEN`` (``xoxp-…``)
    Wilber's user identity. Only used where the *user* must be the author (deleting
    the human's own messages — Slack lets each identity delete only what it wrote).

Other knobs:

``SLACK_CHANNEL_ID``     private session-control channel; the listener ignores everything else.
``SLACK_ALLOWED_USER_IDS`` allowlist of Slack **user ids** allowed to drive sessions.
``SLACK_PRIVATE_USER_ID`` fallback single-user allowlist + the ``<@id>`` mention prefix.
``SLACK_BOT_USER_ID``    the app's own user id, never allowlisted.
``SLACK_PRIVATE_EMAIL``  DM recipient when no channel id is configured.

Alert routing (:mod:`slackbridge.alerts`) is configured by a routes file
(:mod:`slackbridge.routes`: ``$XDG_CONFIG_HOME/slackbridge/routes.toml`` or
``SLACKBRIDGE_ROUTES_FILE``). When that file is absent, the legacy variables below describe
one equivalent route named ``env``; when it is present, they are ignored with a warning.
Off when neither exists:

``SLACK_ALERTS_CHANNEL_ID``     channel(s) watched for alerts, comma/space separated; never
                                written to. May include the control channel: there an alert
                                takes the alert path and everything else behaves as before.
``SLACK_ALERTS_TRIGGER``        phrase that makes a top-level message an alert (required
                                unless every channel has its own, below).
``SLACK_ALERTS_CHANNEL_TRIGGERS`` per-channel override, ``C1=*;C2=SOME PHRASE``: ``;``-separated
                                because a phrase may hold commas. ``*`` means every top-level
                                human message is an alert (a Q&A channel), except those from
                                ``SLACK_ALERTS_IGNORE_USER_IDS``; its text reaches the session
                                as data. Every channel named here must also be watched.
``SLACK_ALERTS_SESSION_NAME``   the background worker session the alerts go to (required).
``SLACK_ALERTS_SESSION_CWD``    where that session starts; defaults to ``NEW_SESSION_CWD``.
``SLACK_ALERTS_IGNORE_USER_IDS`` thread replies from these users are not routed;
                                defaults to ``SLACK_PRIVATE_USER_ID``.
``SLACK_ALERTS_INSTRUCTIONS``   what the session is told to do with a new alert.
``SLACK_ALERTS_BACKFILL_HOURS`` on start, how far back to look for alerts that are not in
                                the items ledger and whose thread is still empty; default
                                24, 0 = off.
``SLACK_ALERTS_STATE``          tracked alerts file; ``SLACK_ALERTS_SEED_STATE`` a read-only seed.

The items ledger (:mod:`slackbridge.items`) reads its own variables, so the
``slackbridge items`` CLI needs nothing else configured:

``SLACKBRIDGE_ITEMS_FILE``      the ledger; default ``$XDG_STATE_HOME/slackbridge/items.json``.
``SLACKBRIDGE_BIN``             the ``slackbridge`` the alerts session is told to run.
``SLACKBRIDGE_TERMINAL_STATUSES`` closing statuses beyond ``done``/``skipped``/
                                ``taken-by-other``, comma-separated: replies in such an item's
                                thread stop being routed by the ledger.
``SLACK_WORKSPACE_URL``         ``https://<workspace>.slack.com``, for message permalinks.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from opscore.env import load_env_file
from opscore.errors import ConfigError
from opscore.secrets import resolve

from slackbridge import routes as routes_mod
from slackbridge.routes import AlertRoute


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
    alerts_channel_ids: frozenset[str] = frozenset()
    alerts_trigger: str = ""
    alerts_channel_triggers: dict[str, str] = field(default_factory=dict)
    alerts_session_name: str = ""
    alerts_session_cwd: Path | None = None
    alerts_ignore_user_ids: frozenset[str] = frozenset()
    alerts_instructions: str = ""
    alerts_backfill_hours: float = 24.0
    alerts_state_path: Path = Path.home() / ".local" / "state" / "slackbridge" / "alerts.json"
    alerts_seed_state_path: Path | None = None
    workspace_url: str = ""
    alert_routes: tuple[AlertRoute, ...] = ()
    """Routes read from the routes file; empty = derive one from the ``alerts_*`` fields."""
    routes_source: str = ""
    routes_warnings: tuple[str, ...] = ()

    def routes(self) -> tuple[AlertRoute, ...]:
        """Every alert route: the file's, else the one the legacy variables describe."""
        if self.alert_routes:
            return self.alert_routes
        if not self.alerts_channel_ids:
            return ()
        return (
            routes_mod.legacy(
                channels=self.alerts_channel_ids,
                trigger=self.alerts_trigger,
                channel_triggers=self.alerts_channel_triggers,
                session=self.alerts_session_name,
                cwd=self.alerts_session_cwd or self.new_session_cwd,
                ignore_users=self.alerts_ignore_user_ids,
                instructions=self.alerts_instructions,
                backfill_hours=self.alerts_backfill_hours,
                seed_state=self.alerts_seed_state_path,
            ),
        )

    def route_for(self, channel: str) -> AlertRoute | None:
        """The route watching ``channel``, or ``None``."""
        return next((r for r in self.routes() if channel in r.all_channels), None)

    def watched_channels(self) -> frozenset[str]:
        """Every channel some alert route watches."""
        return frozenset().union(*(r.all_channels for r in self.routes()))

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


def _channel_triggers(raw: str) -> dict[str, str]:
    """``C1=*;C2=PHRASE`` as ``{channel: trigger}``; a malformed entry is refused, not skipped."""
    out: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        channel, sep, trigger = part.partition("=")
        channel, trigger = channel.strip(), trigger.strip()
        if not sep or not channel or not trigger:
            raise ConfigError(
                f"SLACK_ALERTS_CHANNEL_TRIGGERS entry {part!r} is not CHANNEL=TRIGGER",
                detail="use ';' between entries, e.g. C123=*;C456=TICKET CRITICO",
            )
        out[channel] = trigger
    return out


def _hours(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number of hours, got {raw!r}") from None
    if value < 0:
        raise ConfigError(f"{name} must be >= 0 (0 turns it off), got {raw!r}")
    return value


def _path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else None


def _alerts_settings(private_user_id: str) -> dict[str, object]:
    """The alert-routing fields, refusing a half-configured feature.

    A channel without a trigger or a target session would not fail: it would silently
    route nothing, which reads exactly like "no alerts arrived".
    """
    channels = _split_ids(os.environ.get("SLACK_ALERTS_CHANNEL_ID", ""))
    if not channels:
        return {}
    per_channel = _channel_triggers(os.environ.get("SLACK_ALERTS_CHANNEL_TRIGGERS", ""))
    unknown = sorted(set(per_channel) - channels)
    if unknown:
        raise ConfigError(
            f"SLACK_ALERTS_CHANNEL_TRIGGERS names {', '.join(unknown)}, which "
            "SLACK_ALERTS_CHANNEL_ID does not watch",
            detail="a trigger for an unwatched channel would never fire; add the channel to "
            "SLACK_ALERTS_CHANNEL_ID or drop it from SLACK_ALERTS_CHANNEL_TRIGGERS",
        )
    trigger = os.environ.get("SLACK_ALERTS_TRIGGER", "").strip()
    name = os.environ.get("SLACK_ALERTS_SESSION_NAME", "").strip()
    # The global phrase is only optional when no watched channel falls back to it.
    needs_global = bool(channels - set(per_channel))
    required = [("SLACK_ALERTS_SESSION_NAME", name)]
    if needs_global:
        required.insert(0, ("SLACK_ALERTS_TRIGGER", trigger))
    for var, value in required:
        if not value:
            raise ConfigError(
                f"SLACK_ALERTS_CHANNEL_ID is set but {var} is not: set {var} in .env",
                detail="alert routing needs both a trigger phrase and a target session name; "
                "unset SLACK_ALERTS_CHANNEL_ID to turn the feature off",
            )
    settings: dict[str, object] = {
        "alerts_channel_ids": channels,
        "alerts_trigger": trigger,
        "alerts_channel_triggers": per_channel,
        "alerts_session_name": name,
        "alerts_session_cwd": _path("SLACK_ALERTS_SESSION_CWD"),
        "alerts_ignore_user_ids": _split_ids(
            os.environ.get("SLACK_ALERTS_IGNORE_USER_IDS") or private_user_id
        ),
        "alerts_instructions": os.environ.get("SLACK_ALERTS_INSTRUCTIONS", "").strip(),
        "alerts_backfill_hours": _hours("SLACK_ALERTS_BACKFILL_HOURS", 24.0),
        "alerts_seed_state_path": _path("SLACK_ALERTS_SEED_STATE"),
    }
    return settings


def load() -> SlackConfig:
    """Resolve the bridge configuration from the environment and the repo ``.env``."""
    load_env_file()
    cwd = os.environ.get("NEW_SESSION_CWD") or os.environ.get("CODEX_SESSIONS_NEW_SESSION_CWD", "")
    private_user_id = os.environ.get("SLACK_PRIVATE_USER_ID", "").strip()
    alerts: dict[str, object]
    routes_file = routes_mod.locate()
    if routes_file is not None:
        loaded = routes_mod.load_file(routes_file)
        alerts = {
            "alert_routes": loaded.routes,
            "routes_source": loaded.source,
            "routes_warnings": loaded.warnings,
        }
        if loaded.workspace_url:
            alerts["workspace_url"] = loaded.workspace_url
    else:
        alerts = _alerts_settings(private_user_id)
        if alerts:
            alerts["routes_source"] = routes_mod.ENV_ROUTE_NAME
    state = _path("SLACK_ALERTS_STATE")
    if state:
        alerts["alerts_state_path"] = state
    settings: dict[str, object] = {
        "workspace_url": os.environ.get("SLACK_WORKSPACE_URL", "").strip(),
        **alerts,
    }
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
        **settings,  # type: ignore[arg-type]
    )
