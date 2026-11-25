"""CLAUDE_HOME resolution: an empty or unset variable must fall back to ~/.claude."""

import importlib
from pathlib import Path

import pytest

import slackbridge.claude as claude


@pytest.mark.parametrize("value", [None, ""])
def test_unset_or_empty_claude_home_falls_back_to_home(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("CLAUDE_HOME", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_HOME", value)
    module = importlib.reload(claude)
    assert Path.home() / ".claude" == module.CLAUDE_HOME
    assert Path.home() / ".claude" / "vscode-inbox.jsonl" == module.VSCODE_INBOX


def test_explicit_claude_home_is_used(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    module = importlib.reload(claude)
    assert tmp_path / "vscode-inbox.jsonl" == module.VSCODE_INBOX
    monkeypatch.delenv("CLAUDE_HOME")
    importlib.reload(claude)
