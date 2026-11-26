"""The items ledger: every alert or question the bridge handed over, and what became of it.

Without it, nothing remembers that an item was dispatched: a catch-up on start re-sends
alerts that were delivered and handled long ago, and a Slack redelivery after a restart
does the same. The ledger is the answer to "was this already sent?", and the one place
the receiving session writes down what it did with the item.

One JSON file (``SLACKBRIDGE_ITEMS_FILE``, else ``$XDG_STATE_HOME/slackbridge/items.json``,
else ``~/.local/state/slackbridge/items.json``)::

    {"version": 1, "items": {"<channel>:<ts>": {...entry...}}}

Two processes write it — the listener and ``slackbridge items set`` run by the session —
so every write is read-modify-write under an exclusive ``flock`` on a sidecar ``.lock``
file, and lands with a temporary file plus ``os.replace``: a reader never sees half a file,
and no writer works from a stale copy. ``flock`` locks belong to the open file, so two
threads of one process exclude each other as well, each opening the lock file itself.

A corrupt file is refused, never treated as empty: an empty ledger means "nothing was
ever sent", which is exactly the re-dispatch storm this exists to prevent.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from opscore.errors import ConfigError, NotFoundError, ValidationError

DISPATCHED = "dispatched"
"""Written by the bridge when it hands an item over."""
DISPATCH_ERROR = "dispatch-error"
"""Written by the bridge when the hand-over itself failed; the item is not retried."""
MIGRATED_NOTE = "known from the tracked-alerts state before the ledger existed"

KINDS = ("alert", "question")
QUESTION_STATUSES = frozenset({"in-progress", "done", "skipped"})
"""What a session may record for a question. Alerts take any non-empty status — the
tracker's own status names belong to whoever runs it, not to this code."""
ALERT_STATUSES: frozenset[str] | None = None
"""``None`` = any non-empty string; a set restricts alerts to it."""
TERMINAL_STATUSES = frozenset({"done", "skipped", "taken-by-other"})
"""An item in one of these is closed: replies in its thread are no longer routed by the
ledger. The tracker's own closing statuses are added with ``SLACKBRIDGE_TERMINAL_STATUSES``
(comma-separated), because their names belong to whoever runs the tracker."""

KEY_RE = re.compile(r"^([A-Z0-9]+):(\d+\.\d+)$")
PERMALINK_RE = re.compile(r"/archives/([A-Z0-9]+)/p(\d{7,})")


# --- where things are --------------------------------------------------------
def items_path() -> Path:
    """The ledger file: ``SLACKBRIDGE_ITEMS_FILE``, else under ``XDG_STATE_HOME``."""
    raw = os.environ.get("SLACKBRIDGE_ITEMS_FILE", "").strip()
    if raw:
        return Path(raw).expanduser()
    state = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(state).expanduser() if state else Path.home() / ".local" / "state"
    return base / "slackbridge" / "items.json"


def terminal_statuses() -> frozenset[str]:
    """:data:`TERMINAL_STATUSES` plus ``SLACKBRIDGE_TERMINAL_STATUSES``, compared casefolded."""
    extra = os.environ.get("SLACKBRIDGE_TERMINAL_STATUSES", "")
    names = TERMINAL_STATUSES | {x.strip() for x in extra.split(",") if x.strip()}
    return frozenset(n.casefold() for n in names)


def is_open(entry: dict[str, Any] | None) -> bool:
    """True when ``entry`` exists and its status is not terminal."""
    if entry is None:
        return False
    return str(entry.get("status") or "").strip().casefold() not in terminal_statuses()


def bridge_bin() -> str:
    """The ``slackbridge`` the receiving session runs: ``SLACKBRIDGE_BIN``, else on ``PATH``."""
    return os.environ.get("SLACKBRIDGE_BIN", "").strip() or "slackbridge"


# --- keys ---------------------------------------------------------------------
def make_key(channel: str, ts: str) -> str:
    return f"{channel}:{ts}"


def parse_key(raw: str) -> str:
    """``C123:1700000000.000100`` or a Slack permalink, as a ledger key.

    A permalink to a reply carries ``thread_ts``; the item is the thread's top-level
    message, so that one wins.
    """
    text = raw.strip()
    if KEY_RE.match(text):
        return text
    match = PERMALINK_RE.search(text)
    if match:
        channel, digits = match.group(1), match.group(2)
        ts = f"{digits[:-6]}.{digits[-6:]}"
        thread = parse_qs(urlparse(text).query).get("thread_ts")
        if thread and re.fullmatch(r"\d+\.\d+", thread[0]):
            ts = thread[0]
        return make_key(channel, ts)
    raise ValidationError(
        f"not an item key: {raw!r}",
        detail="use CHANNEL:TS (e.g. C0123ABC:1700000000.000100) or a Slack message permalink",
    )


def validate_status(kind: str, status: str) -> str:
    """The status, stripped, if ``kind`` accepts it; otherwise a :class:`ValidationError`."""
    value = status.strip()
    if not value:
        raise ValidationError("empty status")
    if kind == "question" and value not in QUESTION_STATUSES:
        raise ValidationError(
            f"status {value!r} is not valid for a question",
            detail=f"use one of: {', '.join(sorted(QUESTION_STATUSES))}",
        )
    if kind == "alert" and ALERT_STATUSES is not None and value not in ALERT_STATUSES:
        raise ValidationError(
            f"status {value!r} is not valid for an alert",
            detail=f"use one of: {', '.join(sorted(ALERT_STATUSES))}",
        )
    return value


def update_command(key: str, kind: str) -> str:
    """The exact line a session runs to record what it did with ``key``."""
    status = "<estado del ticket>" if kind == "alert" else "in-progress|done|skipped"
    return f'{bridge_bin()} items set {key} --status "{status}"'


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# --- the file ----------------------------------------------------------------
class ItemLedger:
    """Read and write the ledger file; every write holds the lock across read-modify-write."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or items_path()).expanduser()

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise ConfigError(f"cannot read the items ledger {self.path}: {exc}") from None
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"the items ledger {self.path} is not valid JSON: {exc}",
                detail="fix or move the file aside; it is never overwritten while unreadable",
            ) from None
        items = data.get("items") if isinstance(data, dict) else None
        if data and not isinstance(items, dict):
            raise ConfigError(f"the items ledger {self.path} has no 'items' object")
        return dict(items or {})

    def _store(self, items: dict[str, dict[str, Any]]) -> None:
        fd, tmp = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "items": items}, handle, ensure_ascii=False, indent=1)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _mutate(self, change: Callable[[dict[str, dict[str, Any]]], Any]) -> Any:
        with self._locked():
            items = self._load()
            result = change(items)
            self._store(items)
            return result

    # --- reads -----------------------------------------------------------------
    def get(self, key: str) -> dict[str, Any] | None:
        entry = self._load().get(key)
        return dict(entry) if entry is not None else None

    def all(self) -> list[dict[str, Any]]:
        """Every entry with its ``key``, oldest message first."""
        rows = [{"key": key, **entry} for key, entry in self._load().items()]
        return sorted(rows, key=lambda r: float(r.get("ts") or 0))

    # --- writes ----------------------------------------------------------------
    def admit(
        self,
        *,
        channel: str,
        ts: str,
        kind: str,
        ticket: str | None,
        permalink: str,
        known: bool = False,
    ) -> bool:
        """Record a top-level item; ``True`` only when it was new and must be dispatched.

        Check and insert happen under one lock, so two deliveries of the same message —
        a redelivery racing a catch-up — produce exactly one ``True``. ``known`` marks an
        item the pre-ledger state already tracked: it is recorded, and not dispatched.
        """
        key = make_key(channel, ts)

        def change(items: dict[str, dict[str, Any]]) -> bool:
            if key in items:
                return False
            now = _now()
            items[key] = {
                "channel": channel,
                "ts": ts,
                "kind": kind,
                "ticket": ticket,
                "permalink": permalink,
                "status": DISPATCHED,
                "note": MIGRATED_NOTE if known else "",
                "handler": None,
                "first_dispatched_at": now,
                "updated_at": now,
                "history": [
                    {"at": now, "status": DISPATCHED, "by": "migration" if known else "bridge"}
                ],
            }
            return not known

        return bool(self._mutate(change))

    def set_status(
        self,
        key: str,
        status: str | None,
        *,
        note: str | None = None,
        handler: str | None = None,
        by: str = "",
    ) -> dict[str, Any]:
        """Set ``key``'s status, note and/or handler, appending to its history.

        ``None`` leaves a field as it is. An unknown key is refused, never created: only the
        bridge creates entries, so a typo cannot invent an item.
        """

        def change(items: dict[str, dict[str, Any]]) -> dict[str, Any]:
            entry = items.get(key)
            if entry is None:
                raise NotFoundError(
                    f"no item {key} in the ledger",
                    detail=f"`slackbridge items list` shows what is there ({self.path})",
                )
            now = _now()
            if status is not None:
                entry["status"] = status
            if note is not None:
                entry["note"] = note
            record: dict[str, Any] = {"at": now, "status": entry.get("status"), "by": by}
            if handler is not None:
                entry["handler"] = handler
                record["handler"] = handler
            entry["updated_at"] = now
            entry.setdefault("history", []).append(record)
            return {"key": key, **entry}

        result: dict[str, Any] = self._mutate(change)
        return result
