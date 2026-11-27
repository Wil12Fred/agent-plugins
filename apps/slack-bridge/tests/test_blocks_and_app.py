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

    @staticmethod
    def _heading(row: dict[str, Any]) -> str:
        text = session_card(row)[0]["text"]["text"]
        return str(text).split("\n", 1)[0]

    def test_card_names_the_session_so_two_alike_sessions_can_be_told_apart(self) -> None:
        """Rule: a named session shows its name in bold before the title."""
        row = {
            "sid": "abc12345-x",
            "short": "abc12345",
            "status": "busy",
            "pid": 42,
            "name": "support-worker",
            "title": "[Pasted text #1]",
        }
        assert self._heading(row) == "🟠 *support-worker*  ·  [Pasted text #1]"

    def test_card_does_not_repeat_an_auto_name_equal_to_the_title(self) -> None:
        """Rule: when the name is the title (Claude's auto name), it is shown once."""
        row = {
            "sid": "abc12345-x",
            "short": "abc12345",
            "status": "idle",
            "pid": 42,
            "name": "Pantalla oscura",
            "title": "Pantalla oscura",
        }
        assert self._heading(row) == "🟢 *Pantalla oscura*"

    def test_card_without_a_name_keeps_the_title_heading(self) -> None:
        """Rule: closed sessions carry no name; the card looks as it did before."""
        row = {
            "sid": "abc12345-x",
            "short": "abc12345",
            "status": "idle",
            "pid": None,
            "title": "Estado de V",
        }
        assert self._heading(row) == "🟢 *Estado de V*"

    def test_list_sessions_carries_the_name_claude_agents_reports(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rule: the live row's name comes from `claude agents`, not from the title."""
        from slackbridge import claude

        agent = {
            "pid": 1156,
            "kind": "interactive",
            "status": "busy",
            "name": "support-worker",
            "sessionId": "61ffed4b-81db-43e4-882d-c4b7846cae80",
            "cwd": "/nowhere",
            "startedAt": 0,
        }
        monkeypatch.setattr(claude, "agents", lambda: [agent])
        monkeypatch.setattr(claude, "history_titles", dict)
        rows = claude.list_sessions(
            project=__import__("pathlib").Path("/nowhere"), include_closed=False
        )
        assert [r["name"] for r in rows] == ["support-worker"]

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


def test_a_session_listed_twice_gets_one_card_and_unique_block_ids() -> None:
    from slackbridge.blocks import sessions_blocks

    sid = "29d4e18e-0000-0000-0000-000000000000"
    rows = [
        {"sid": sid, "short": "29d4e18e", "status": "closed", "title": "old"},
        {"sid": sid, "short": "29d4e18e", "status": "idle", "pid": 42, "title": "live"},
        {"sid": "other", "short": "other", "status": "idle", "pid": 7, "title": "x"},
    ]
    blocks = sessions_blocks(rows)
    ids = [b["block_id"] for b in blocks if "block_id" in b]
    assert len(ids) == len(set(ids))
    titles = " ".join(b["text"]["text"] for b in blocks if b.get("type") == "section")
    assert "live" in titles and "old" not in titles


def test_block_ids_stay_unique_when_short_ids_collide() -> None:
    from slackbridge.blocks import sessions_blocks

    rows = [
        {"sid": "aaaaaaaa-1", "short": "aaaaaaaa", "status": "idle", "pid": 1, "title": "one"},
        {"sid": "aaaaaaaa-2", "short": "aaaaaaaa", "status": "idle", "pid": 2, "title": "two"},
        {"short": "?", "status": "closed", "title": "no sid"},
        {"short": "?", "status": "closed", "title": "no sid either"},
    ]
    ids = [b["block_id"] for b in sessions_blocks(rows) if "block_id" in b]
    assert len(ids) == len(set(ids))
