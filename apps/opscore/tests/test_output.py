# --- the command name in the envelope ---------------------------------------
#
# Every one of these is about a defect that shipped: all four CLIs emitted
# `"command": ""` because the resolver seam existed and nobody used it.


def test_the_resolver_is_the_default_so_a_cli_cannot_forget_it() -> None:
    """Rule: naming the command must not be something each CLI opts into.

    It was opt-in for the whole life of the repository and the take-up was zero.
    """
    from dataclasses import fields

    from opscore.output import Output, click_command_path

    resolver = next(f for f in fields(Output) if f.name == "command_resolver")
    assert resolver.default is click_command_path


def test_outside_a_cli_the_resolver_answers_empty_rather_than_raising() -> None:
    """`opscore` is used by libraries with no CLI; it must not require click."""
    from opscore.output import click_command_path

    assert click_command_path() == ""


def test_the_resolver_finds_the_click_that_typer_actually_vendors() -> None:
    """Rule: look for Typer's vendored click, not only the top-level package.

    Typer stopped depending on `click` as a distribution and vendors it as
    `typer._click`. A resolver that knew only `import click` returned "" inside
    a perfectly working CLI — which is exactly the answer it exists to prevent,
    reintroduced one import line lower down.
    """
    import importlib

    import typer

    app = typer.Typer()
    seen: list[str] = []

    @app.command("read")
    def read() -> None:
        from opscore.output import click_command_path

        seen.append(click_command_path())

    @app.command("write")
    def write() -> None:
        """A second command, so Typer keeps this a group.

        With one command Typer collapses it into the root and `read` is parsed
        as a stray argument — which would have made this test measure argument
        parsing rather than command resolution.
        """

    runner = importlib.import_module("typer.testing").CliRunner()
    result = runner.invoke(app, ["read"])

    assert result.exit_code == 0, result.output
    assert seen == ["read"], "the running command was not resolved"


def test_a_failure_envelope_names_the_command_too() -> None:
    """Rule: the error path names the command, and it is the path that matters.

    The context stack has unwound by the time the entry point renders the
    failure, so the error records its own path at construction — inside the
    command body, which is the only moment it is knowable.
    """
    import importlib

    import typer

    from opscore.errors import NotFoundError

    app = typer.Typer()

    @app.command("read")
    def read() -> None:
        raise NotFoundError("nope")

    @app.command("write")
    def write() -> None:
        """Keeps the app a group; see the note in the test above."""

    captured: list[str] = []
    runner = importlib.import_module("typer.testing").CliRunner()
    try:
        runner.invoke(app, ["read"], catch_exceptions=False)
    except NotFoundError as exc:
        captured.append(exc.command_path)

    assert captured == ["read"]


def test_an_error_raised_outside_a_cli_carries_an_empty_path_not_a_wrong_one() -> None:
    """The control. Without it every assertion above passes on a function that
    always returns the same string."""
    from opscore.errors import ValidationError

    assert ValidationError("no cli here").command_path == ""
