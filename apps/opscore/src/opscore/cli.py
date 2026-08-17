"""The entry point every CLI here shares.

Four CLIs wrote the same `main()` — load the `.env`, run the app, render our own
errors as messages — and each carried its own copy of the reasoning in a
docstring. Three of them were identical to the line; the fourth had drifted.

Consolidating them is worth one module on its own, but it is not why this exists.
It exists because the shared version can fix something none of the copies did:

**Click handles a usage error itself.** On a bad invocation it prints usage to
*stderr*, exits 2, and leaves **stdout empty** — under ``--json`` too. A caller
that parses stdout gets nothing back and cannot tell a mistyped flag from a
crash, which is the worst shape an error can take for a script. `cloudprobe` and
`gpull` both did this, and it was invisible from the inside: every command
answered correctly when invoked correctly.

``standalone_mode=False`` hands control back so the envelope is ours to write.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any

from opscore.errors import BridgeError, ValidationError
from opscore.output import get_output


def run(app: Any, argv: Sequence[str] | None = None) -> None:
    """Run `app`, rendering every expected failure as one envelope.

    Args:
        app: the Typer application.
        argv: arguments, defaulting to the real ones. Injectable for tests.

    Raises:
        SystemExit: always, with the exit code the failure carries — 0 on
            success, 2 for a usage error, otherwise the error's own code.
    """
    from opscore.env import load_env_file

    # Before parsing, because several options default from the environment;
    # reading it afterwards would be too late to change them.
    load_env_file()

    arguments = list(argv) if argv is not None else sys.argv[1:]
    _preset_output(arguments)

    try:
        app(args=arguments, standalone_mode=False)
    except BridgeError as exc:
        # Our own errors carry a message, a fix and an exit code. Anything else
        # is a real bug and keeps its traceback.
        get_output().failure(exc)
        raise SystemExit(exc.exit_code) from None
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        raise SystemExit(130) from None
    except Exception as exc:  # noqa: BLE001 - narrowed immediately below
        usage = _as_usage_error(exc)
        if usage is None:
            raise
        get_output().failure(ValidationError(usage, detail="run with --help for the usage"))
        raise SystemExit(2) from None
    raise SystemExit(0)


def _preset_output(argv: Sequence[str]) -> None:
    """Honour ``--json`` before Click has had a chance to parse it.

    The flag lives on the root callback, and Click resolves the command *before*
    running that callback — so on a bad invocation the callback never runs, the
    output object is never configured, and the usage error is rendered as prose
    even though the caller asked for JSON. A script then gets an empty stdout
    from the one case it most needs to parse.

    Reading argv directly here is duplication of a sort, and it is the cheap
    kind: the callback still runs on every valid invocation and sets the same
    values again. What this buys is that the *invalid* ones are answerable.
    """
    from opscore.output import Output, get_output, set_output

    if get_output().json_mode:
        return
    if "--json" in argv:
        set_output(Output(json_mode=True, quiet="--quiet" in argv))


def _as_usage_error(exc: BaseException) -> str | None:
    """The message, if this is Click complaining about the invocation.

    Click is reached through whichever module actually provides it — Typer
    vendors it as ``typer._click`` and no longer depends on the distribution, so
    matching on the top-level import alone silently matches nothing.
    """
    import importlib

    for name in ("click.exceptions", "typer._click.exceptions"):
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        if isinstance(exc, module.UsageError | module.ClickException):
            return str(getattr(exc, "format_message", lambda: exc)())
        if isinstance(exc, module.Abort):
            return "aborted"
    return None


def exit_code_of(exc: BaseException) -> int:
    """The exit code a failure should produce. Kept public for tests."""
    return exc.exit_code if isinstance(exc, BridgeError) else 1


__all__ = ["exit_code_of", "run"]
