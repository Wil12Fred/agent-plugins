"""Routing a Slack message to the right engine.

The bridge opens a Claude session for a top-level message, unless the message
asks for Codex. That second half was lost in the port: `codex.create_session`
survived with no callers, so Slack could resume an existing Codex session but
never start one.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from slackbridge.ui import wants_codex


# --- Slack could no longer start a Codex session ----------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Codex revisa el guard", "revisa el guard"),
        ("codex: arregla el test", "arregla el test"),
        ("  Codex   con espacios  ", "con espacios"),
        # A bare mention is a question about Codex, not an instruction for it.
        ("Codex", None),
        ("revisa el guard", None),
        # Must not fire on a word that merely starts with it.
        ("codexify algo", None),
    ],
)
def test_only_a_codex_prefixed_message_routes_to_codex(text: str, expected: str | None) -> None:
    # The port kept `codex.create_session` but wired nothing to it, so every
    # top-level Slack message opened a *Claude* session and there was no way to
    # start a Codex one at all.
    assert wants_codex(text) == expected


# --- the routing branch itself, not just the predicate ----------------------
def test_a_top_level_message_routes_to_the_right_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The three-way branch, exercised as `callback` actually runs it.

    Only `wants_codex` was tested. Nothing covered the wiring, and this module
    runs under a live systemd unit on the user's real control channel — a
    reordered guard or a mis-wired thread would pass every other test.
    """
    from slackbridge.listeners import messages

    opened: list[tuple[str, str]] = []
    monkeypatch.setattr(
        messages, "open_new_codex_session", lambda *a, **k: opened.append(("codex", a[3]))
    )
    monkeypatch.setattr(
        messages, "open_new_session", lambda *a, **k: opened.append(("claude", a[3]))
    )
    # Run the dispatch inline so the assertions do not race a daemon thread.
    monkeypatch.setattr(
        messages.threading,
        "Thread",
        lambda target, args, kwargs=None, daemon=None: _Inline(target, args, kwargs or {}),
    )
    monkeypatch.setattr(messages, "is_allowed", lambda *_a: True)
    monkeypatch.setattr(messages, "is_bridge_post", lambda *_a: False)
    monkeypatch.setattr(messages, "_seen", lambda *_a: False)

    config = SimpleNamespace(
        channel_id="C1", new_session_cwd=Path("/tmp"), new_session_terminal="auto"
    )
    logger = logging.getLogger("test")

    messages.callback(
        {"channel": "C1", "ts": "1", "user": "U1", "text": "Codex revisa el guard"},
        object(),  # type: ignore[arg-type]
        logger,
        config,  # type: ignore[arg-type]
    )
    messages.callback(
        {"channel": "C1", "ts": "2", "user": "U1", "text": "revisa el guard"},
        object(),  # type: ignore[arg-type]
        logger,
        config,  # type: ignore[arg-type]
    )
    assert opened == [("codex", "revisa el guard"), ("claude", "revisa el guard")]


class _Inline:
    """A Thread stand-in that runs on `start()`, so assertions do not race."""

    def __init__(self, target: object, args: tuple[object, ...], kwargs: dict[str, object]) -> None:
        self._call = (target, args, kwargs)

    def start(self) -> None:
        target, args, kwargs = self._call
        target(*args, **kwargs)  # type: ignore[operator]
