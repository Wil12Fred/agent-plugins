"""``agentctl`` — measure what an agent would otherwise have to judge.

Two commands so far, and both exist because they were prose inside a subagent's
prompt before they were code. A model asked "does this repository do
spec-driven development?" answers confidently either way; a filesystem check
answers with a path or with nothing.

Every command is usable three ways, and all three are first class:

- a person gets a readable summary on stdout;
- a script gets `--json`: exactly one envelope, and an exit code that means
  something;
- an agent gets an MCP tool, derived from this command tree by `agentctl mcp`.

Nothing here writes. There is no `--force`, no `--apply`, and no flag that would
need one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from agentctl import detect as detect_module
from agentctl import strays as strays_module

app = typer.Typer(
    name="agentctl",
    help="What a repository has adopted, and what leaked out of its code directory.",
    no_args_is_help=True,
    add_completion=False,
)

EXIT_FINDINGS = 7
"""Something was found that a human should look at. Not an error — a result."""


def _emit(payload: dict[str, Any], *, as_json: bool, human: str) -> None:
    """One envelope on stdout, or the human summary. Never both."""
    if as_json:
        typer.echo(json.dumps({"ok": True, "data": payload}, indent=2))
    else:
        typer.echo(human)


@app.command("detect")
def detect_command(
    path: Annotated[Path, typer.Argument(help="Repository to inspect.")] = Path("."),
    as_json: Annotated[bool, typer.Option("--json", help="Emit the JSON envelope.")] = False,
    only: Annotated[
        list[str] | None,
        typer.Option("--only", help="Restrict to these practice names. Repeatable."),
    ] = None,
) -> None:
    """Report which practices this repository has adopted, with the evidence.

    Run this before auditing anything. A rule the project never took on is not a
    finding, and reporting one is how an audit teaches people to ignore it —
    including on the day it is right.

    Every verdict carries the path that produced it, so a reader who disagrees
    can open the evidence rather than argue with a judgement. Nothing is inferred
    from a language or a framework: a practice is adopted when something that
    could only exist because somebody adopted it exists.
    """
    result = detect_module.detect(path, only=only)
    lines = [f"{result.root}", f"code roots: {', '.join(result.code_roots) or 'none found'}", ""]
    for practice in result.practices:
        mark = "yes" if practice.adopted else " no"
        evidence = f"  ({', '.join(practice.evidence)})" if practice.evidence else ""
        lines.append(f"  [{mark}] {practice.name}{evidence}")
    lines.append("")
    lines.append(f"{len(result.adopted)} adopted, {len(result.not_adopted)} not applicable")
    _emit(result.as_dict(), as_json=as_json, human="\n".join(lines))


@app.command("strays")
def strays_command(
    path: Annotated[Path, typer.Argument(help="Repository to inspect.")] = Path("."),
    as_json: Annotated[bool, typer.Option("--json", help="Emit the JSON envelope.")] = False,
    include_declared: Annotated[
        bool, typer.Option("--include-declared", help="Also list the declared exceptions.")
    ] = False,
) -> None:
    """Find executables sitting outside the code directory.

    Each one has exactly three honest outcomes — port it into the code directory
    as a tested command, promote it into a test suite, or declare it as a
    deliberate one-off *with its reason recorded*. "Leave it where it is" is not
    one of them, and the absence is the point: an undeclared script and a
    declared one look identical on disk, and only one is a decision.

    Checks `.py`, `.sh`, `.mjs`, `.js` and friends rather than one extension.
    Reading only Python is how a shell script survived for months pointing at a
    machine its author no longer owned — nothing had failed, because nothing had
    looked.

    Exits 7 when anything is undeclared.
    """
    found = strays_module.find(path)
    open_items = strays_module.undeclared(found)
    shown = found if include_declared else open_items

    if not detect_module.code_roots(path):
        message = (
            "no conventional code directory found, so 'outside the code' has no meaning here — "
            "not measured"
        )
        _emit(
            {"code_roots": [], "strays": [], "undeclared": 0, "measured": False},
            as_json=as_json,
            human=message,
        )
        return

    lines = [f"{len(open_items)} undeclared, {len(found) - len(open_items)} declared", ""]
    for stray in shown:
        state = "declared" if stray.declared else "UNDECLARED"
        reason = f" — {stray.reason}" if stray.reason else ""
        lines.append(f"  [{state}] {stray.path} ({stray.lines} lines){reason}")
    _emit(
        {
            "code_roots": list(detect_module.code_roots(path)),
            "strays": [s.as_dict() for s in shown],
            "undeclared": len(open_items),
            "measured": True,
        },
        as_json=as_json,
        human="\n".join(lines),
    )
    if open_items:
        raise typer.Exit(EXIT_FINDINGS)


@app.command("mcp")
def mcp_command(
    path: Annotated[
        Path | None,
        typer.Option("--root", help="Repository the tools default to. Defaults to the cwd."),
    ] = None,
) -> None:
    """Serve these commands as MCP tools over stdio.

    Every tool is read-only, because every command is. There is no
    `--allow-writes` counterpart to grant, which is the cheapest safety property
    a server can have: not "writes are guarded" but "there are none".
    """
    from agentctl.mcp_server import serve

    serve(default_root=path or Path.cwd())


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
