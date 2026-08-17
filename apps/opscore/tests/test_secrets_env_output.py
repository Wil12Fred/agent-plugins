"""The rest of the shared foundation: redaction, the `.env`, the JSON envelope.

Four packages depend on these and none of them had a test. The redaction one is
the load-bearing case: a secret rendered into a transcript outlives the session
that printed it, and every "never printed" claim in this repository ultimately
rests on this function.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opscore.env import load_env_file, project_root
from opscore.errors import BridgeError, ConfigError
from opscore.output import Output
from opscore.secrets import redact


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #


def test_an_unset_secret_says_so_rather_than_printing_nothing() -> None:
    """ "" and "<unset>" read very differently in a log."""
    assert redact(None) == "<unset>"
    assert redact("") == "<unset>"


def test_a_short_secret_reveals_nothing_at_all() -> None:
    """Rule: a hint that is a large fraction of a short secret is not a hint.

    The purpose is to say *which* credential is loaded, never to reproduce part
    of one — and four characters of a six-character value reproduces most of it.
    """
    assert redact("s3cret") == "<set>"


def test_a_long_secret_shows_only_its_ends() -> None:
    token = "xoxb-" + "a" * 40
    shown = redact(token)

    assert token not in shown
    assert shown.startswith("xoxb")
    assert "…" in shown


@pytest.mark.parametrize("secret", ["s3cret", "xoxb-" + "b" * 40, "x" * 12])
def test_no_redaction_ever_contains_the_whole_secret(secret: str) -> None:
    """The property that matters, stated once over every shape."""
    assert secret not in redact(secret)


# --------------------------------------------------------------------------- #
# The `.env`
# --------------------------------------------------------------------------- #


def test_a_real_environment_variable_beats_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule: the file fills in what is missing; it never overrides.

    Without this, `FOO=bar mytool run` would be silently ignored whenever a
    `.env` existed — and the override is how you test against a second account
    without editing a file.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("SHARED=from-file\n", encoding="utf-8")
    monkeypatch.setenv("SHARED", "from-environment")

    load_env_file(env_file)

    import os

    assert os.environ["SHARED"] == "from-environment"


def test_a_missing_value_is_taken_from_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: without it, 'never loads anything' would pass the test above."""
    env_file = tmp_path / ".env"
    env_file.write_text("ONLY_IN_FILE=yes\n", encoding="utf-8")
    monkeypatch.delenv("ONLY_IN_FILE", raising=False)

    assert load_env_file(env_file) == 1

    import os

    assert os.environ["ONLY_IN_FILE"] == "yes"


def test_comments_and_blank_lines_are_not_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("# a comment\n\nREAL=1\nnot-an-assignment\n", encoding="utf-8")
    monkeypatch.delenv("REAL", raising=False)

    assert load_env_file(env_file) == 1


def test_quotes_are_stripped_from_a_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule: `.env` files are written both ways and a literal quote in a token
    fails authentication in a way that reads like a wrong token."""
    env_file = tmp_path / ".env"
    env_file.write_text('QUOTED="value"\n', encoding="utf-8")
    monkeypatch.delenv("QUOTED", raising=False)

    load_env_file(env_file)

    import os

    assert os.environ["QUOTED"] == "value"


def test_a_missing_env_file_is_not_an_error(tmp_path: Path) -> None:
    """Rule: every containerised deployment exports its variables another way."""
    assert load_env_file(tmp_path / "nope.env") == 0


def test_the_project_root_is_found_by_walking_up(tmp_path: Path) -> None:
    """Rule: a tool runs from wherever the operator started it, not from a
    fixed layout."""
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)

    assert project_root(deep) == tmp_path.resolve()


def test_no_marker_anywhere_answers_the_starting_point(tmp_path: Path) -> None:
    """Rule: return something usable rather than climbing to `/`."""
    assert project_root(tmp_path) == tmp_path.resolve()


# --------------------------------------------------------------------------- #
# The envelope
# --------------------------------------------------------------------------- #


def test_json_mode_emits_exactly_one_envelope(capsys: pytest.CaptureFixture[str]) -> None:
    Output(json_mode=True).result({"a": 1})

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["data"] == {"a": 1}


def test_a_failure_carries_the_error_class_and_the_detail(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Rule: a caller branches on `error`, and a human needs the fix.

    An envelope that says only "it failed" makes both of them guess.
    """
    Output(json_mode=True).failure(ConfigError("no site", detail="set JIRA_SITE"))

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"] == "ConfigError"
    assert "JIRA_SITE" in json.dumps(payload)


def test_a_failure_after_a_result_does_not_emit_a_second_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Rule: two envelopes on stdout make it unparseable.

    A gate that printed its findings with `ok=False` has already said
    everything; the exit code carries the failure.
    """
    out = Output(json_mode=True)
    out.result({"findings": 3}, ok=False, message="3 findings")
    out.failure(BridgeError("boom"))

    stdout = capsys.readouterr().out
    assert stdout.count('"ok"') == 1, stdout


def test_human_mode_prints_the_human_text_not_json(capsys: pytest.CaptureFixture[str]) -> None:
    Output(json_mode=False).result({"a": 1}, human="one thing")
    assert "one thing" in capsys.readouterr().out


def test_every_error_carries_its_exit_code() -> None:
    """Rule: the exit code is the contract for a script that cannot read prose."""
    assert ConfigError("x").exit_code == 2
    assert BridgeError("x").exit_code == 1
