"""Alert routes: which watched channel's alerts go to which background worker session.

A route is one channel group -> one named Claude Code **background** session (the
"worker"), started and kept by the bridge in the route's permission mode. Routes live in a
user config file, ``$XDG_CONFIG_HOME/slackbridge/routes.toml`` (``~/.config/…`` when the
variable is unset), or wherever ``SLACKBRIDGE_ROUTES_FILE`` points. ``slackbridge routes
example`` prints a commented template and ``slackbridge routes check`` validates one.

Precedence, decided once at startup:

* **file present** -> the routes come only from it;
* **file absent** -> the legacy ``SLACK_ALERTS_*`` variables become a single route named
  ``env`` (see :func:`legacy`), so a setup from before the file existed keeps working;
* **both** -> the file wins, and one warning names the variables that are ignored;
* an **explicit** path (``SLACKBRIDGE_ROUTES_FILE``) that does not exist is an error, not
  a quiet fall back: somebody asked for that file.

Validation refuses to start on anything that would otherwise route silently wrong: a
missing required key, a duplicate route name, a channel claimed by two routes, a channel
trigger for a channel the route does not watch, an unknown permission mode, a settings
file that does not exist, two routes sharing a session with different launch options (the
worker would be relaunched back and forth). Every error names the file, the route and the
key. An unknown key is only a warning: a typo in an optional key must not take the
listener down, but it must not go unnoticed either.
"""

from __future__ import annotations

import os
import re
import shutil
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opscore.errors import ConfigError

ROUTES_FILE_ENV = "SLACKBRIDGE_ROUTES_FILE"
ENV_ROUTE_NAME = "env"
ANY = "*"
"""A trigger meaning: every top-level human message is an alert (a Q&A channel)."""
DEFAULT_PERMISSION_MODE = "auto"
DEFAULT_FOLLOW_DAYS = 5.0
DEFAULT_BACKFILL_HOURS = 24.0
DEFAULT_INSTRUCTIONS = "Atiéndelo según tus instrucciones para este tipo de alerta."
PERMISSION_MODES = frozenset(
    {"acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"}
)
"""What ``claude --permission-mode`` accepts (``claude --help``, 2.1.332)."""
PERMISSION_PROMPTS = frozenset({"host", "none"})
"""What ``claude --permission-prompts`` accepts."""
SETTING_SOURCES = ("user", "project", "local")
"""What ``claude --setting-sources`` accepts."""
TERMINALS = ("tmux", "background")
"""How the worker runs: an interactive session in a detached tmux session (default, visible
with ``tmux attach -t <session>``), or a ``claude --bg`` background session."""
DEFAULT_TERMINAL = "tmux"
TMUX_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
"""A tmux worker's session name doubles as its tmux session name: ``.``/``:`` are targets."""

ENV_ALERT_VARS = (
    "SLACK_ALERTS_CHANNEL_ID",
    "SLACK_ALERTS_TRIGGER",
    "SLACK_ALERTS_CHANNEL_TRIGGERS",
    "SLACK_ALERTS_SESSION_NAME",
    "SLACK_ALERTS_SESSION_CWD",
    "SLACK_ALERTS_IGNORE_USER_IDS",
    "SLACK_ALERTS_SEED_STATE",
    "SLACK_ALERTS_INSTRUCTIONS",
    "SLACK_ALERTS_BACKFILL_HOURS",
    "SLACK_ALERTS_TERMINAL",
)
"""The legacy single-route variables the file replaces."""

TOP_KEYS = frozenset({"workspace_url", "route"})
REQUIRED_KEYS = ("name", "channels", "session", "cwd", "instructions")
OPTIONAL_KEYS = frozenset(
    {
        "trigger",
        "channel_triggers",
        "test_channels",
        "ignore_users",
        "permission_mode",
        "permission_prompts",
        "settings",
        "setting_sources",
        "follow_thread_days",
        "backfill_hours",
        "seed_state",
        "from",
        "terminal",
        "remote_control",
    }
)
ROUTE_KEYS = frozenset(REQUIRED_KEYS) | OPTIONAL_KEYS


@dataclass(frozen=True)
class AlertRoute:
    """One channel group and the background worker session its alerts are handed to."""

    name: str
    channels: frozenset[str]
    trigger: str
    session: str
    cwd: Path
    instructions: str
    channel_triggers: Mapping[str, str] = field(default_factory=dict)
    test_channels: frozenset[str] = frozenset()
    ignore_users: frozenset[str] = frozenset()
    from_users: frozenset[str] = frozenset()
    """TOML ``from``: when set, only these authors open items, and their thread replies are
    routed; the route's items are then requests from the operator, not reports."""
    permission_mode: str = DEFAULT_PERMISSION_MODE
    terminal: str = DEFAULT_TERMINAL
    permission_prompts: str | None = None
    settings: Path | None = None
    setting_sources: tuple[str, ...] = ()
    remote_control: bool = False
    """Launch the worker with ``--remote-control <session>``: it must be reachable from
    claude.ai/mobile. A worker started with ``--setting-sources project`` never reads a
    user-level ``remoteControlAtStartup``, so the flag is the only way it gets it."""
    follow_thread_days: float = DEFAULT_FOLLOW_DAYS
    backfill_hours: float = DEFAULT_BACKFILL_HOURS
    seed_state: Path | None = None

    @property
    def all_channels(self) -> frozenset[str]:
        return self.channels | self.test_channels

    @property
    def follow_seconds(self) -> float:
        return self.follow_thread_days * 24 * 3600

    def trigger_for(self, channel: str) -> str:
        """The trigger in ``channel``: its own override, else the route's phrase."""
        return self.channel_triggers.get(channel, self.trigger)

    def is_test(self, channel: str) -> bool:
        """True for a channel listed under ``test_channels``: the prompt says so."""
        return channel in self.test_channels

    @property
    def prompts(self) -> str | None:
        """``--permission-prompts``: the route's value, else ``none`` under ``dontAsk``.

        ``dontAsk`` exists so nothing waits on a person; ``none`` makes the same promise
        for whatever would still prompt, instead of leaving it to a host nobody is.
        """
        if self.permission_prompts:
            return self.permission_prompts
        return "none" if self.permission_mode == "dontAsk" else None

    def launch_options(self) -> dict[str, str]:
        """The options a worker must have been started with, by ``claude`` flag name."""
        options = {"--permission-mode": self.permission_mode}
        if self.prompts:
            options["--permission-prompts"] = self.prompts
        if self.setting_sources:
            options["--setting-sources"] = ",".join(self.setting_sources)
        if self.settings:
            options["--settings"] = str(self.settings)
        return options

    def launch_flags(self) -> list[str]:
        """``--name <session>`` plus :meth:`launch_options`, as ``claude`` arguments."""
        flags = ["--name", self.session]
        for flag, value in self.launch_options().items():
            flags += [flag, value]
        if self.remote_control:
            flags += ["--remote-control", self.session]
        return flags

    def summary(self, instructions_cap: int = 80) -> dict[str, Any]:
        """JSON-friendly view for ``routes check``: instructions truncated, never in full."""
        text = " ".join(self.instructions.split())
        if len(text) > instructions_cap:
            text = text[: instructions_cap - 1] + "…"
        return {
            "name": self.name,
            "channels": sorted(self.channels),
            "test_channels": sorted(self.test_channels),
            "trigger": self.trigger,
            "channel_triggers": dict(sorted(self.channel_triggers.items())),
            "ignore_users": sorted(self.ignore_users),
            "from": sorted(self.from_users),
            "session": self.session,
            "cwd": str(self.cwd),
            "terminal": self.terminal,
            "launch": self.launch_flags(),
            "follow_thread_days": self.follow_thread_days,
            "backfill_hours": self.backfill_hours,
            "seed_state": str(self.seed_state) if self.seed_state else None,
            "instructions": text,
        }


@dataclass(frozen=True)
class RoutesFile:
    """The routes read from a file, and what was worth warning about."""

    routes: tuple[AlertRoute, ...]
    source: str
    workspace_url: str = ""
    warnings: tuple[str, ...] = ()


# --- where the file is ------------------------------------------------------
def default_path(environ: Mapping[str, str] | None = None) -> Path:
    """``$SLACKBRIDGE_ROUTES_FILE``, else ``$XDG_CONFIG_HOME/slackbridge/routes.toml``."""
    env = os.environ if environ is None else environ
    override = env.get(ROUTES_FILE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    xdg = env.get("XDG_CONFIG_HOME", "").strip()
    # The XDG spec says a relative value is invalid and must be ignored.
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".config"
    return base / "slackbridge" / "routes.toml"


def locate(environ: Mapping[str, str] | None = None) -> Path | None:
    """The routes file to use, ``None`` when there is none (use the env), or an error.

    An explicit ``SLACKBRIDGE_ROUTES_FILE`` that does not exist is refused: falling back
    to the variables would run a configuration nobody asked for.
    """
    env = os.environ if environ is None else environ
    target = default_path(env)
    if target.is_file():
        return target
    if env.get(ROUTES_FILE_ENV, "").strip():
        raise ConfigError(
            f"routes file {target}: does not exist ({ROUTES_FILE_ENV} points at it)",
            detail=f"fix the path, or unset {ROUTES_FILE_ENV} to use the default location",
        )
    return None


# --- parsing ----------------------------------------------------------------
def _ids(value: Any, where: str, key: str, *, required: bool) -> frozenset[str]:
    if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
        raise ConfigError(
            f"{where}: key {key!r} must be a list of non-empty strings",
            detail=f'example: {key} = ["C0123456789"]',
        )
    ids = frozenset(x.strip() for x in value)
    if required and not ids:
        raise ConfigError(f"{where}: key {key!r} must not be empty")
    return ids


def _text(value: Any, where: str, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: key {key!r} must be a non-empty string")
    return value.strip()


def _absolute(value: Any, where: str, key: str) -> Path:
    path = Path(_text(value, where, key)).expanduser()
    if not path.is_absolute():
        raise ConfigError(
            f"{where}: key {key!r} must be an absolute path, got {str(path)!r}",
            detail="the listener and the worker run in different directories; a relative "
            "path would mean two different files",
        )
    return path


def _number(raw: dict[str, Any], key: str, default: float, where: str, *, zero: bool) -> float:
    value = raw.get(key, default)
    ok = not isinstance(value, bool) and isinstance(value, int | float)
    if not ok or value < 0 or (value == 0 and not zero):
        bound = ">= 0 (0 turns it off)" if zero else "> 0"
        raise ConfigError(f"{where}: key {key!r} must be a number {bound}, got {value!r}")
    return float(value)


def _flag(raw: Mapping[str, Any], key: str, label: str) -> bool:
    value = raw.get(key, False)
    if not isinstance(value, bool):
        raise ConfigError(f"{label}: key {key!r} must be true or false, got {value!r}")
    return value


def parse_setting_sources(value: Any, where: str) -> tuple[str, ...]:
    """A list or a comma string of ``user``/``project``/``local``, order kept, no repeats."""
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
    elif isinstance(value, list) and all(isinstance(p, str) for p in value):
        parts = [p.strip() for p in value]
    else:
        raise ConfigError(f"{where}: key 'setting_sources' must be a list or a comma string")
    parts = [p for p in parts if p]
    bad = [p for p in parts if p not in SETTING_SOURCES]
    if bad or not parts:
        raise ConfigError(
            f"{where}: key 'setting_sources' is {value!r}",
            detail="expected a non-empty selection of: " + ", ".join(SETTING_SOURCES),
        )
    return tuple(dict.fromkeys(parts))


def _channel_triggers(value: Any, where: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ConfigError(
            f"{where}: key 'channel_triggers' must be a table",
            detail='example: channel_triggers = { C0123456789 = "*" }',
        )
    return {
        _text(channel, where, "channel_triggers"): _text(trigger, where, "channel_triggers")
        for channel, trigger in value.items()
    }


def _route(raw: Any, index: int, source: str, warnings: list[str]) -> AlertRoute:
    label = f"routes file {source}: route #{index + 1}"
    if not isinstance(raw, dict):
        raise ConfigError(f"{label}: each [[route]] must be a table")
    if isinstance(raw.get("name"), str) and raw["name"].strip():
        label = f"routes file {source}: route {raw['name'].strip()!r}"
    for key in REQUIRED_KEYS:
        if key not in raw:
            raise ConfigError(
                f"{label}: missing required key {key!r}",
                detail="required: " + ", ".join(REQUIRED_KEYS),
            )
    for key in sorted(set(raw) - ROUTE_KEYS):
        warnings.append(f"{label}: unknown key {key!r} ignored")

    mode = raw.get("permission_mode", DEFAULT_PERMISSION_MODE)
    if mode not in PERMISSION_MODES:
        raise ConfigError(
            f"{label}: key 'permission_mode' is {mode!r}",
            detail="expected one of: " + ", ".join(sorted(PERMISSION_MODES)),
        )
    prompts = raw.get("permission_prompts")
    if prompts is not None and prompts not in PERMISSION_PROMPTS:
        raise ConfigError(
            f"{label}: key 'permission_prompts' is {prompts!r}",
            detail="expected one of: " + ", ".join(sorted(PERMISSION_PROMPTS)),
        )
    settings = _absolute(raw["settings"], label, "settings") if "settings" in raw else None
    if settings is not None and not settings.is_file():
        raise ConfigError(
            f"{label}: key 'settings' — {settings} does not exist",
            detail="the worker would be started without the settings that restrict it",
        )
    sources = (
        parse_setting_sources(raw["setting_sources"], label) if "setting_sources" in raw else ()
    )
    cwd = _absolute(raw["cwd"], label, "cwd")
    if not cwd.is_dir():
        raise ConfigError(f"{label}: key 'cwd' — {cwd} is not a directory")

    channels = _ids(raw["channels"], label, "channels", required=True)
    tests = _ids(raw.get("test_channels", []), label, "test_channels", required=False)
    if both := channels & tests:
        raise ConfigError(
            f"{label}: key 'test_channels' repeats {', '.join(sorted(both))} from 'channels'",
            detail="a channel is either a real alerts channel or a test one",
        )
    per_channel = _channel_triggers(raw.get("channel_triggers", {}), label)
    if stray := sorted(set(per_channel) - (channels | tests)):
        raise ConfigError(
            f"{label}: key 'channel_triggers' names {', '.join(stray)}, which the route "
            "does not watch",
            detail="a trigger for an unwatched channel would never fire; add the channel "
            "to 'channels' or drop it from 'channel_triggers'",
        )
    trigger = _text(raw["trigger"], label, "trigger") if "trigger" in raw else ""
    if not trigger and (uncovered := sorted((channels | tests) - set(per_channel))):
        raise ConfigError(
            f"{label}: missing key 'trigger' for {', '.join(uncovered)}",
            detail="set 'trigger', or give every channel its own in 'channel_triggers'",
        )
    ignore = _ids(raw.get("ignore_users", []), label, "ignore_users", required=False)
    authors = _ids(raw.get("from", []), label, "from", required=False)
    if both := authors & ignore:
        raise ConfigError(
            f"{label}: key 'from' repeats {', '.join(sorted(both))} from 'ignore_users'",
            detail="an author is either the one whose messages are routed or one who is "
            "never routed, not both",
        )
    terminal = raw.get("terminal", DEFAULT_TERMINAL)
    if terminal not in TERMINALS:
        raise ConfigError(
            f"{label}: key 'terminal' is {terminal!r}",
            detail="expected one of: " + ", ".join(TERMINALS),
        )
    session = _text(raw["session"], label, "session")
    if terminal == "tmux":
        if not TMUX_NAME.match(session):
            raise ConfigError(
                f"{label}: key 'session' is {session!r}, which tmux cannot name",
                detail="use letters, digits, '-' and '_' (it is also the tmux session name)",
            )
        if shutil.which("tmux") is None:
            raise ConfigError(
                f"{label}: key 'terminal' is 'tmux' but tmux is not installed",
                detail='install tmux, or set terminal = "background" (a claude --bg worker)',
            )
    seed = raw.get("seed_state")
    return AlertRoute(
        name=_text(raw["name"], label, "name"),
        channels=channels,
        test_channels=tests,
        trigger=trigger,
        channel_triggers=per_channel,
        ignore_users=ignore,
        from_users=authors,
        session=session,
        cwd=cwd,
        permission_mode=str(mode),
        terminal=str(terminal),
        permission_prompts=prompts,
        settings=settings,
        setting_sources=sources,
        remote_control=_flag(raw, "remote_control", label),
        follow_thread_days=_number(
            raw, "follow_thread_days", DEFAULT_FOLLOW_DAYS, label, zero=False
        ),
        backfill_hours=_number(raw, "backfill_hours", DEFAULT_BACKFILL_HOURS, label, zero=True),
        seed_state=_absolute(seed, label, "seed_state") if seed else None,
        instructions=_text(raw["instructions"], label, "instructions"),
    )


def check_unique(routes: tuple[AlertRoute, ...], source: str) -> None:
    """Refuse a duplicate route name, a channel two routes claim, or a session two routes
    would launch differently."""
    names: set[str] = set()
    owner: dict[str, str] = {}
    sessions: dict[str, AlertRoute] = {}
    for route in routes:
        if route.name in names:
            raise ConfigError(
                f"routes file {source}: route {route.name!r}: key 'name' is duplicated",
                detail="route names must be unique",
            )
        names.add(route.name)
        for channel in sorted(route.all_channels):
            if channel in owner:
                raise ConfigError(
                    f"routes file {source}: route {route.name!r}: key 'channels' — "
                    f"{channel} is already routed by {owner[channel]!r}",
                    detail="a channel belongs to exactly one route",
                )
            owner[channel] = route.name
        other = sessions.setdefault(route.session, route)
        if (other.cwd, other.terminal, other.launch_options()) != (
            route.cwd,
            route.terminal,
            route.launch_options(),
        ):
            raise ConfigError(
                f"routes file {source}: route {route.name!r}: key 'session' — "
                f"{route.session!r} is also the session of route {other.name!r}, with a "
                "different cwd, terminal or launch options",
                detail="one worker has one mode; give each route its own session name, or "
                "make their cwd/permission keys identical",
            )


def parse(text: str, source: str) -> RoutesFile:
    """Parse and validate a routes file's text. ``source`` names it in every error."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"routes file {source}: not valid TOML: {exc}") from None
    warnings = [
        f"routes file {source}: unknown key {k!r} ignored" for k in sorted(set(data) - TOP_KEYS)
    ]
    raw_routes = data.get("route", [])
    if not isinstance(raw_routes, list) or not raw_routes:
        raise ConfigError(
            f"routes file {source}: no [[route]] defined",
            detail="add at least one [[route]]; `slackbridge routes example` prints a template",
        )
    routes = tuple(_route(raw, i, source, warnings) for i, raw in enumerate(raw_routes))
    check_unique(routes, source)
    url = data.get("workspace_url", "")
    if not isinstance(url, str):
        raise ConfigError(f"routes file {source}: key 'workspace_url' must be a string")
    return RoutesFile(
        routes=routes, source=source, workspace_url=url.strip(), warnings=tuple(warnings)
    )


def load_file(path: Path, environ: Mapping[str, str] | None = None) -> RoutesFile:
    """Read and validate one routes file; warn about legacy variables it overrides."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"routes file {path}: cannot be read: {exc.strerror or exc}") from None
    loaded = parse(text, str(path))
    env = os.environ if environ is None else environ
    if set_vars := [v for v in ENV_ALERT_VARS if env.get(v, "").strip()]:
        warning = (
            f"routes file {path} is present: the env alert vars are ignored ({', '.join(set_vars)})"
        )
        loaded = RoutesFile(
            loaded.routes, loaded.source, loaded.workspace_url, (*loaded.warnings, warning)
        )
    return loaded


# --- the legacy variables ---------------------------------------------------
def legacy(
    *,
    channels: frozenset[str],
    trigger: str,
    channel_triggers: Mapping[str, str],
    session: str,
    cwd: Path,
    ignore_users: frozenset[str],
    instructions: str,
    backfill_hours: float,
    seed_state: Path | None,
    terminal: str = DEFAULT_TERMINAL,
) -> AlertRoute:
    """The single route the ``SLACK_ALERTS_*`` variables describe (already validated).

    Equivalent to what those variables did before the file existed, permission mode
    included (``auto``): a setup that never wrote a routes file behaves as it did, except
    that its session is now always a background worker.
    """
    return AlertRoute(
        name=ENV_ROUTE_NAME,
        channels=channels,
        trigger=trigger,
        channel_triggers=dict(channel_triggers),
        session=session,
        cwd=cwd,
        ignore_users=ignore_users,
        instructions=instructions or DEFAULT_INSTRUCTIONS,
        backfill_hours=backfill_hours,
        seed_state=seed_state,
        terminal=terminal,
    )


EXAMPLE = '''\
# slackbridge alert routes — ~/.config/slackbridge/routes.toml
# ($XDG_CONFIG_HOME/slackbridge/routes.toml, or wherever SLACKBRIDGE_ROUTES_FILE points)
#
# Each [[route]] watches some channels and hands every alert, and every reply in a
# tracked alert's thread, to one named Claude Code session — the worker — that the bridge
# starts and owns. By default it runs in a DETACHED tmux session named after it: nothing
# opens on screen, and `tmux attach -t <session>` shows it (detach with Ctrl-b d). Before
# every hand-over the bridge reads the worker's live mode and, if someone changed it
# (Shift+Tab), kills that tmux session and resumes the same conversation in this route's
# mode. It never types into a session that merely shares the worker's name.
# The bridge never writes to these channels. Validate with:  slackbridge routes check
#
# When this file exists the SLACK_ALERTS_* variables are ignored (one warning says so).

# Optional: https://<workspace>.slack.com, used to put a permalink in each prompt.
workspace_url = "https://example.slack.com"

# Three routes, one worker. Routes that share a `session` must share its cwd and every
# launch option (terminal, permission_mode, permission_prompts, settings,
# setting_sources): one
# worker has one mode. A channel belongs to exactly one route.

[[route]]
name = "critical"                     # required, unique
channels = ["C0000000001"]            # required; a channel belongs to one route
trigger = "TICKET CRITICO"            # phrase making a top-level message an alert;
                                      # accent-, case- and emoji-insensitive
# channel_triggers = { C0000000001 = "*" }  # optional per-channel override of `trigger`
# test_channels = ["C0000000009"]     # optional; same handling, the prompt says "test"
ignore_users = ["U0000000001"]        # optional; never routed (the operator's own posts)
session = "support-worker"            # required; the background worker's session name.
                                      # Do not reuse the name of a terminal session.
cwd = "/path/to/project"              # required, absolute; where the worker runs
terminal = "tmux"                     # optional, default "tmux": a detached tmux session
                                      # (needs tmux); "background" = a claude --bg session
permission_mode = "dontAsk"           # optional, default "auto": acceptEdits, auto,
                                      # bypassPermissions, manual, dontAsk, plan
# permission_prompts = "none"         # optional: host | none; default "none" under dontAsk
settings = "/path/to/project/.claude/worker.settings.json"  # optional: --settings
setting_sources = "project"           # optional: user, project, local (list or "a,b").
                                      # Allow-lists MERGE across sources: a user-level
                                      # "Bash(*)" would still apply unless "user" is left out.
# follow_thread_days = 5              # optional: stop following a thread after this
# backfill_hours = 24                 # optional: catch-up window on start; 0 = off
# seed_state = "/path/to/state.json"  # optional; read-only {"alerts": {ts: {...}}} seed
instructions = """
Triage the alert following your instructions for this kind of alert.
"""

[[route]]
name = "questions"
channels = ["C0000000002"]
trigger = "*"                         # every top-level human message is a question
ignore_users = ["U0000000001"]
session = "support-worker"
cwd = "/path/to/project"
terminal = "tmux"
permission_mode = "dontAsk"
settings = "/path/to/project/.claude/worker.settings.json"
setting_sources = "project"
instructions = "Answer the question following your instructions for questions."

[[route]]
name = "owner"
channels = ["C0000000003"]            # the control channel: once a route claims it, no
                                      # message there opens a new session any more
trigger = "*"
from = ["U0000000001"]                # only these authors open items, and their thread
                                      # replies are routed; must not overlap ignore_users.
                                      # Posts made through an app's API are not theirs.
session = "support-worker"
cwd = "/path/to/project"
terminal = "tmux"
permission_mode = "dontAsk"
settings = "/path/to/project/.claude/worker.settings.json"
setting_sources = "project"
instructions = "Do what the operator asks, following your instructions for requests."
'''
