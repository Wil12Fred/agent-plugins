"""Resolving "which session is this thread about, and what should it be sent?"."""

from __future__ import annotations

import pytest

from slackbridge import claude, codex
from slackbridge.replies import ThreadBatch, resolve_instruction

KNOWN = "019ee964-1111-2222-3333-444455556666"


@pytest.fixture(autouse=True)
def known_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make exactly one session id resolvable, without touching ~/.claude."""

    def fake_first_token(text: str) -> str | None:
        for token in (text or "").split():
            cleaned = token.strip("`*_<>()[]:.,…")
            if cleaned and KNOWN.startswith(cleaned) and len(cleaned) >= 8:
                return cleaned
        return None

    monkeypatch.setattr(claude, "first_session_token", fake_first_token)
    monkeypatch.setattr(codex, "first_session_token", lambda text, sessions=None: None)


def batch(parent: str = "", texts: list[str] | None = None, bridge: str = "") -> ThreadBatch:
    return ThreadBatch(
        channel="C_TARGET",
        thread_ts="1000.0001",
        parent_text=parent,
        texts=texts or [],
        bridge_text=bridge,
    )


class TestResolveInstruction:
    def test_no_replies_resolves_to_nothing(self) -> None:
        assert resolve_instruction(batch()) is None

    def test_parent_names_the_session_so_all_replies_are_the_instruction(self) -> None:
        result = resolve_instruction(
            batch(parent=f"{KNOWN}\n\nrespuesta previa", texts=["sigue", "y luego commit"])
        )
        assert result is not None
        assert result.sid == KNOWN
        assert result.text == "sigue\ny luego commit"

    def test_bare_session_id_reply_means_show_last_response(self) -> None:
        result = resolve_instruction(batch(texts=[KNOWN]))
        assert result is not None
        assert result.sid == KNOWN
        assert result.text == ""

    def test_leading_session_id_sends_the_rest_as_the_instruction(self) -> None:
        result = resolve_instruction(batch(texts=[f"{KNOWN} corre los tests"]))
        assert result is not None
        assert result.sid == KNOWN
        assert result.text == "corre los tests"

    def test_markdown_wrapped_id_in_the_parent_is_still_found(self) -> None:
        result = resolve_instruction(batch(parent=f"`{KNOWN}` terminó", texts=["dale"]))
        assert result is not None
        assert result.sid == KNOWN

    def test_unknown_first_token_is_used_verbatim(self) -> None:
        """Legacy fallback: an unresolvable id still reaches dispatch, which reports it."""
        result = resolve_instruction(batch(texts=["deadbeef arregla el bug"]))
        assert result is not None
        assert result.sid == "deadbeef"
        assert result.text == "arregla el bug"

    def test_id_inside_a_later_reply_pulls_the_other_replies_in(self) -> None:
        result = resolve_instruction(batch(texts=["contexto extra", f"{KNOWN} resume"]))
        assert result is not None
        assert result.sid == KNOWN
        assert result.text == "resume\ncontexto extra"
