"""The shared entry point, and the failure shapes it exists to prevent.

Each test names a defect that was live in the four CLIs before this module
existed. None of them was visible from inside a command: every command answered
correctly when it was invoked correctly.
"""

from __future__ import annotations

import json

import pytest
import typer

from opscore.cli import run
from opscore.errors import ConfigError
from opscore.output import Output, get_output, set_output


@pytest.fixture
def app() -> typer.Typer:
    """A two-command group.

    Two, not one: Typer collapses a single-command app into its root, and the
    command name is then parsed as a stray argument — a fixture that would make
    these tests measure argument parsing instead of dispatch.
    """
    application = typer.Typer()

    @application.callback()
    def root(json_mode: bool = typer.Option(False, "--json")) -> None:
        set_output(Output(json_mode=json_mode))

    @application.command("read")
    def read() -> None:
        get_output().result({"read": True})

    @application.command("fail")
    def fail() -> None:
        raise ConfigError("no cluster", detail="set CLUSTER")

    return application


def envelope(capsys: pytest.CaptureFixture[str]) -> dict:
    out = capsys.readouterr().out
    assert out.strip(), "stdout was empty — a caller parsing it learns nothing"
    return json.loads(out)


def test_a_usage_error_answers_with_an_envelope_under_json(
    app: typer.Typer, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rule: `--json` means stdout carries an envelope, on every path.

    Click's own handling prints usage to *stderr* and exits 2 with stdout
    **empty**. A script then cannot tell a mistyped flag from a crash, which is
    the worst shape an error can take. `cloudprobe` and `gpull` both did this.

    This is the case that proves the argv pre-read: the command name is unknown,
    so Click never reaches the root callback and nothing else could have
    configured the output. Verified by disabling the pre-read and watching only
    this test go red.
    """
    with pytest.raises(SystemExit) as exit_info:
        run(app, ["--json", "nope"])

    assert exit_info.value.code == 2
    payload = envelope(capsys)
    assert payload["ok"] is False
    assert "nope" in payload["message"]


def test_a_bad_option_on_a_valid_command_also_answers_as_an_envelope(
    app: typer.Typer, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other half of the usage-error case, and it takes a different route.

    Here the root callback *does* run — the command name parsed, so `--json` was
    honoured the ordinary way — and the failure happens while parsing that
    command's own options. Only the `standalone_mode=False` half is doing work;
    the pre-read is not involved.

    Written down because the first draft of this test claimed to prove the
    pre-read, and disabling the pre-read left it green: it was measuring the
    path that never needed it.
    """
    with pytest.raises(SystemExit) as exit_info:
        run(app, ["--json", "read", "--bogus"])

    assert exit_info.value.code == 2
    assert envelope(capsys)["error"] == "ValidationError"


def test_our_own_error_keeps_its_exit_code_and_its_fix(
    app: typer.Typer, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        run(app, ["--json", "fail"])

    assert exit_info.value.code == 2, "ConfigError's own code, not a generic 1"
    payload = envelope(capsys)
    assert payload["detail"] == "set CLUSTER"
    assert payload["command"] == "fail", "the failure path names the command too"


def test_a_command_that_succeeds_still_exits_zero(
    app: typer.Typer, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control. Without it every assertion above passes on a runner that
    fails everything."""
    with pytest.raises(SystemExit) as exit_info:
        run(app, ["--json", "read"])

    assert exit_info.value.code == 0
    assert envelope(capsys)["ok"] is True


def test_an_unexpected_exception_keeps_its_traceback(app: typer.Typer) -> None:
    """Rule: only *expected* failures are rendered as messages.

    Swallowing a real bug into a tidy envelope is how a crash gets reported as a
    configuration problem, and the traceback that would have found it is gone.
    """

    @app.command("boom")
    def boom() -> None:
        raise RuntimeError("a real bug")

    with pytest.raises(RuntimeError, match="a real bug"):
        run(app, ["boom"])
