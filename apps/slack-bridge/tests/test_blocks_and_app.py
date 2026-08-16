"""Block Kit rendering, channel purge ordering, and offline construction of the Bolt app."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import make_config

from slackbridge import channel as channel_mod
from slackbridge.blocks import result_blocks, session_card, sessions_blocks
from slackbridge.bolt_app import build_app


def _action_ids(blocks: list[dict[str, Any]]) -> list[str]:
    return [
        element["action_id"]
        for block in blocks
        if block.get("type") == "actions"
        for element in block["elements"]
    ]


class TestBlocks:
    def test_live_session_card_offers_close_and_stop(self) -> None:
        row = {
            "sid": "abc12345-x",
            "short": "abc12345",
            "status": "busy",
            "pid": 42,
            "engine": "claude",
            "title": "t",
            "project": "/p",
            "started": "01-01 10:00",
        }
        ids = _action_ids(session_card(row))
        assert "sess_close" in ids
        assert "sess_stop" in ids

    def test_closed_session_card_hides_close_and_stop(self) -> None:
        row = {
            "sid": "abc12345-x",
            "short": "abc12345",
            "status": "idle",
            "pid": None,
            "engine": "codex",
            "title": "t",
            "project": "",
            "started": "01-01 10:00",
        }
        ids = _action_ids(session_card(row))
        assert "sess_close" not in ids
        assert "sess_stop" not in ids
        assert "sess_continue" in ids

    def test_refresh_button_carries_the_filter_so_a_refresh_reproduces_the_view(self) -> None:
        blocks = sessions_blocks([], {"all": True, "live": False, "query": "payments"})
        footer = next(b for b in blocks if b.get("block_id") == "sess_footer")
        refresh = next(e for e in footer["elements"] if e["action_id"] == "sess_refresh")
        assert '"query": "payments"' in refresh["value"]

    def test_result_blocks_truncate_a_huge_answer(self) -> None:
        blocks = result_blocks("abc12345", "x" * 10000)
        assert len(blocks[0]["text"]["text"]) < 3600

    def test_full_session_id_travels_in_the_button_value(self) -> None:
        blocks = result_blocks("abc12345-6789-full-id", "hola")
        assert all(
            element["value"] == "abc12345-6789-full-id"
            for block in blocks
            if block.get("type") == "actions"
            for element in block["elements"]
        )


class FakeAPI:
    """Minimal stand-in for :class:`slackbridge.api.SlackAPI`."""

    def __init__(self, messages: list[dict[str, Any]], replies: dict[str, list[dict[str, Any]]]):
        self._messages = messages
        self._replies = replies
        self.deleted: list[str] = []

    def iter_history(self, channel: str) -> list[dict[str, Any]]:
        return self._messages

    def replies(self, channel: str, ts: str, limit: int = 200) -> list[dict[str, Any]]:
        return self._replies.get(ts, [])

    def delete(self, channel: str, ts: str) -> bool:
        self.deleted.append(ts)
        return True


class TestChannelPurge:
    def test_thread_replies_are_collected_before_their_parent(self) -> None:
        api = FakeAPI(
            messages=[{"ts": "100.0", "reply_count": 2}, {"ts": "90.0"}],
            replies={"100.0": [{"ts": "100.0"}, {"ts": "101.0"}, {"ts": "102.0"}]},
        )
        order = channel_mod.collect_timestamps(api, "C1")  # type: ignore[arg-type]
        assert order == ["101.0", "102.0", "100.0", "90.0"]

    def test_purge_reports_what_it_deleted(self) -> None:
        api = FakeAPI(messages=[], replies={})
        result = channel_mod.purge("C1", ["1", "2"], [api], pause=0)  # type: ignore[list-item]
        assert result == {"total": 2, "deleted": 2, "failed": []}
        assert api.deleted == ["1", "2"]


class TestBoltApp:
    def test_app_builds_without_connecting_and_registers_every_handler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app, config = build_app(make_config())
        assert config.channel_id == "C_TARGET"
        # 3 slash commands + 8 button actions + 2 modals + 2 events (message, app_home_opened)
        assert len(app._listeners) == 15
