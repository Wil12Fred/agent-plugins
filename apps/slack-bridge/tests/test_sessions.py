"""Dispatch policy, session listing and Codex transcript parsing — all offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from slackbridge import claude, codex, sessions


class TestDispatchPolicy:
    def test_stop_word_interrupts_instead_of_instructing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sessions, "stop", lambda sid: f"stopped {sid}")
        monkeypatch.setattr(
            claude, "dispatch", lambda *a, **k: pytest.fail("must not dispatch a control word")
        )
        assert sessions.dispatch("abc123 detener") == "stopped abc123"

    def test_close_word_terminates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sessions, "close", lambda sid: f"closed {sid}")
        assert sessions.dispatch("abc123 cerrar") == "closed abc123"

    def test_compact_word_injects_the_tui_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, str] = {}

        def fake_dispatch(sid: str, instruction: str, **_: object) -> tuple[int, str]:
            seen["sid"], seen["instruction"] = sid, instruction
            return 0, "ok"

        monkeypatch.setattr(claude, "dispatch", fake_dispatch)
        assert sessions.dispatch("abc123 compact") == "ok"
        assert seen["instruction"] == sessions.COMPACT_CMD

    def test_unknown_claude_id_falls_back_to_codex(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(claude, "dispatch", lambda *a, **k: (2, "no encontrada"))
        session = codex.CodexSession(
            sid="019ee964-0000-0000-0000-000000000000",
            path=Path("/tmp/x.jsonl"),
            cwd=None,
            started_at=None,
            model="gpt",
            originator=None,
            first_user="hola",
            last_response="la última de codex",
            mtime=0.0,
        )
        monkeypatch.setattr(codex, "resolve_session", lambda sid, pool=None: (session, None))
        monkeypatch.setattr(codex, "resume", lambda *a, **k: (0, "respuesta codex"))
        assert sessions.dispatch("019ee964 sigue") == "respuesta codex"

    def test_bare_id_on_codex_returns_the_last_response_without_running(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(claude, "dispatch", lambda *a, **k: (2, "no encontrada"))
        session = codex.CodexSession(
            sid="019ee964-0000-0000-0000-000000000000",
            path=Path("/tmp/x.jsonl"),
            cwd=None,
            started_at=None,
            model=None,
            originator=None,
            first_user="hola",
            last_response="la última de codex",
            mtime=0.0,
        )
        monkeypatch.setattr(codex, "resolve_session", lambda sid, pool=None: (session, None))
        monkeypatch.setattr(
            codex, "resume", lambda *a, **k: pytest.fail("a bare id must run nothing")
        )
        assert sessions.dispatch("019ee964") == "la última de codex"

    def test_empty_text_is_rejected(self) -> None:
        assert "Uso" in sessions.dispatch("   ")


class TestListing:
    def test_live_only_drops_rows_without_a_pid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = [
            {
                "engine": "claude",
                "sid": "a",
                "short": "a",
                "status": "busy",
                "pid": 111,
                "title": "vivo",
                "project": "/p",
                "started": "01-01 10:00",
            },
            {
                "engine": "claude",
                "sid": "b",
                "short": "b",
                "status": "idle",
                "pid": None,
                "title": "cerrado",
                "project": "",
                "started": "01-01 09:00",
            },
        ]
        monkeypatch.setattr(claude, "list_sessions", lambda **_: rows)
        monkeypatch.setattr(codex, "list_sessions", lambda **_: [])
        assert [r["sid"] for r in sessions.list_all(live_only=True)] == ["a"]

    def test_query_filters_on_title_and_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = [
            {
                "engine": "claude",
                "sid": "aaa",
                "short": "aaa",
                "status": "idle",
                "pid": None,
                "title": "migrar payments",
                "project": "/repo",
                "started": "",
            },
            {
                "engine": "codex",
                "sid": "bbb",
                "short": "bbb",
                "status": "idle",
                "pid": None,
                "title": "revisar lessons",
                "project": "/repo",
                "started": "",
            },
        ]
        monkeypatch.setattr(claude, "list_sessions", lambda **_: rows)
        monkeypatch.setattr(codex, "list_sessions", lambda **_: [])
        assert [r["sid"] for r in sessions.list_all(query="payments")] == ["aaa"]

    def test_codex_is_excluded_from_the_live_view(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Codex publishes no process<->session mapping, so it can never be 'live'."""
        monkeypatch.setattr(claude, "list_sessions", lambda **_: [])
        monkeypatch.setattr(
            codex, "list_sessions", lambda **_: pytest.fail("codex must not be queried for live")
        )
        assert sessions.list_all(live_only=True) == []


class TestCodexTranscript:
    def _write(self, path: Path, records: list[dict[str, object]]) -> Path:
        target = path / "rollout-2026-01-01T00-00-00-019ee964-1111-2222-3333-444455556666.jsonl"
        target.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
        return target

    def test_skips_injected_context_when_picking_the_title(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "019ee964-1111-2222-3333-444455556666",
                        "cwd": "/repo",
                        "model": "gpt-5",
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "<environment_context>ruido"},
                },
                {
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "arregla el bug de pagos"},
                },
                {"type": "event_msg", "payload": {"type": "agent_message", "message": "listo"}},
            ],
        )
        session = codex.parse_session(path)
        assert session is not None
        assert session.first_user == "arregla el bug de pagos"
        assert session.last_response == "listo"
        assert session.cwd == "/repo"

    def test_reads_response_item_records_too(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            [
                {
                    "type": "response_item",
                    "payload": {"type": "message", "role": "user", "content": [{"text": "hola"}]},
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"text": "respuesta final"}],
                    },
                },
            ],
        )
        session = codex.parse_session(path)
        assert session is not None
        assert session.sid == "019ee964-1111-2222-3333-444455556666"  # recovered from the filename
        assert session.last_response == "respuesta final"

    def test_task_complete_wins_over_an_earlier_agent_message(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            [
                {"type": "event_msg", "payload": {"type": "agent_message", "message": "parcial"}},
                {
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "last_agent_message": "final"},
                },
            ],
        )
        session = codex.parse_session(path)
        assert session is not None
        assert session.last_response == "final"

    def test_ambiguous_prefix_is_refused(self) -> None:
        pool = [
            codex.CodexSession("019ee9641", Path("/a"), None, None, None, None, "", "", 0.0),
            codex.CodexSession("019ee9642", Path("/b"), None, None, None, None, "", "", 0.0),
        ]
        session, error = codex.resolve_session("019ee964", pool)
        assert session is None
        assert error is not None and "ambiguous" in error


class TestClaudeTranscript:
    def test_encode_project_matches_claudes_folder_naming(self) -> None:
        assert claude.encode_project(Path("/home/w/Projects/Demo")) == "-home-w-Projects-Demo"

    def test_classify_issue_recognises_a_dead_login(self) -> None:
        message = claude.classify_issue("Please run /login to continue")
        assert message is not None and "re-login" in message

    def test_classify_issue_explains_no_conversation_found(self) -> None:
        message = claude.classify_issue("Error: No conversation found with session ID abc")
        assert message is not None and "abierta/viva" in message

    def test_classify_issue_returns_none_for_ordinary_output(self) -> None:
        assert claude.classify_issue("todo bien, tests en verde") is None


class TestSessionTokenScanning:
    """Only id-shaped tokens may resolve to a session.

    Legacy defect (``list.py::_first_session_token``): any token that prefix-matched a
    transcript filename counted, and the lookup globs ``<token>*.jsonl``. A one-letter word
    like "a" therefore matched an arbitrary session, so an ordinary Slack sentence could be
    dispatched somewhere random. Reproduced live on 2026-08-06.
    """

    @pytest.fixture
    def projects(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        project = tmp_path / "-home-w-repo"
        project.mkdir()
        (project / "abc12345-1111-2222-3333-444455556666.jsonl").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(claude, "PROJECTS", tmp_path)
        return tmp_path

    def test_prose_never_resolves_to_a_session(self, projects: Path) -> None:
        assert claude.first_session_token("Continuamos con la revisión de esto") is None

    def test_short_prefix_is_refused(self, projects: Path) -> None:
        assert claude.first_session_token("a arregla el bug") is None

    def test_eight_hex_chars_resolve(self, projects: Path) -> None:
        assert claude.first_session_token("abc12345 sigue") == "abc12345"

    def test_markdown_wrapped_id_resolves(self, projects: Path) -> None:
        assert claude.first_session_token("mira `abc12345`, sigue") == "abc12345"

    def test_full_uuid_resolves(self, projects: Path) -> None:
        full = "abc12345-1111-2222-3333-444455556666"
        assert claude.first_session_token(f"{full} sigue") == full


# --- the misdispatch incident, both halves ----------------------------------
def test_an_ordinary_word_is_not_a_session_id() -> None:
    from slackbridge.codex import SID_TOKEN_RE

    # Any unique prefix used to resolve, so hex-shaped Spanish words dispatched
    # the rest of a thread into whatever session they happened to hit.
    for word in ("de", "cafe", "bed", "dada", "Continuamos", "add", "face"):
        assert not SID_TOKEN_RE.match(word.lower()), word

    for real in ("019fd23b", "41b21910-dbdc-42ec", "b9a814e1"):
        assert SID_TOKEN_RE.match(real), real
