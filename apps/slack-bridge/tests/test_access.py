"""Access control and the "never answer yourself" guards."""

from __future__ import annotations

import pytest
from conftest import BOT_USER, HUMAN, make_config

from slackbridge.access import human_texts, is_allowed, is_bridge_post, is_human_instruction


class TestAllowlist:
    def test_allowlisted_user_is_allowed(self) -> None:
        assert is_allowed(make_config(), HUMAN)

    def test_stranger_is_refused(self) -> None:
        assert not is_allowed(make_config(), "U_STRANGER")

    def test_bot_user_is_never_allowed_even_when_listed(self) -> None:
        config = make_config(allowed_user_ids=frozenset({HUMAN, BOT_USER}))
        assert not is_allowed(config, BOT_USER)

    def test_empty_allowlist_is_open(self) -> None:
        assert is_allowed(make_config(allowed_user_ids=frozenset()), "U_ANYONE")

    def test_missing_user_is_refused(self) -> None:
        assert not is_allowed(make_config(), None)


class TestBridgePostGuard:
    """OPER-714: the bridge posted a completion notice, the allowlist was empty in that
    process so ``is_allowed`` failed open, and the notice was ingested as a new request —
    every answer spawned another session. The bot_id guard must hold regardless of config.
    """

    @pytest.mark.parametrize(
        "message",
        [
            {"user": BOT_USER, "bot_id": "B0ALQFZPH8X", "app_id": "A0AM9GZA9N0", "text": "listo"},
            {"user": BOT_USER, "subtype": "bot_message", "text": "listo"},
            {"bot_id": "B123", "text": "listo"},
        ],
    )
    def test_bridge_posts_are_never_instructions(self, message: dict[str, object]) -> None:
        open_config = make_config(allowed_user_ids=frozenset(), bot_user_id="")
        assert is_bridge_post(message)
        assert not is_human_instruction(open_config, message)

    def test_human_post_is_an_instruction(self) -> None:
        message = {"user": HUMAN, "text": "revisa el MR"}
        assert not is_bridge_post(message)
        assert is_human_instruction(make_config(), message)


class TestHumanTexts:
    def test_keeps_order_drops_duplicates_and_bots(self) -> None:
        config = make_config()
        messages = [
            {"user": HUMAN, "text": "uno"},
            {"user": BOT_USER, "bot_id": "B1", "text": "ruido"},
            {"user": HUMAN, "text": "uno"},
            {"user": HUMAN, "text": "dos"},
            {"user": "U_STRANGER", "text": "tres"},
            {"user": HUMAN, "text": "   "},
        ]
        assert human_texts(config, messages) == ["uno", "dos"]
