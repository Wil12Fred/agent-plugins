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


clip_app = typer.Typer(
    name="clipboard", help="Put text on this machine's clipboard.", no_args_is_help=True
)
app.add_typer(clip_app, name="clipboard")


@clip_app.command("copy")
def clipboard_copy(
    text: Annotated[str | None, typer.Argument(help="Text to copy. Omit to read stdin.")] = None,
    file: Annotated[
        Path | None, typer.Option("--file", help="Read the text from this file instead.")
    ] = None,
    hold_seconds: Annotated[
        int,
        typer.Option("--hold-seconds", help="X11 fallback only: release after N seconds."),
    ] = 30,
    backend: Annotated[str, typer.Option("--backend", help="auto | klipper | x11.")] = "auto",
    display: Annotated[
        str | None, typer.Option("--display", help="X display to own. Defaults to $DISPLAY.")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the JSON envelope.")] = False,
) -> None:
    """Copy text to the clipboard, and verify it landed.

    Handing a value to a human through the clipboard is the one operation where
    "the command exited 0" is worthless evidence. On X11 a selection lives only
    while the owning process does, so setting it and exiting leaves the
    clipboard **empty** — and the failure is invisible until somebody pastes.
    It has happened: an SSH key handed over this way arrived as a blank entry.

    So the backend is chosen by what is actually running, KDE's Klipper first
    because it takes ownership itself and keeps the entry in history, and the
    write is read back wherever a backend can be read back.

    Non-ASCII is written through the raw UTF-8 targets rather than the
    convenience API, which mangles accents — the difference between "Aquí está"
    and "Aqu est".
    """
    from agentctl import clipboard as clipboard_module

    if file is not None:
        payload = file.read_text(encoding="utf-8")
    elif text is not None:
        payload = text
    else:
        payload = sys.stdin.read()
    if not payload:
        raise typer.BadParameter("nothing to copy")

    chosen = clipboard_module.copy(
        payload,
        backend=clipboard_module.Backend(backend),
        hold_seconds=hold_seconds,
        display=display,
    )
    _emit(
        {"characters": len(payload), "backend": str(chosen)},
        as_json=as_json,
        human=f"copied {len(payload)} characters via {chosen}",
    )


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


android_app = typer.Typer(
    name="android", help="Drive an Android app on an emulator, over adb.", no_args_is_help=True
)
app.add_typer(android_app, name="android")


def _emulator(serial: str) -> Any:
    from agentctl.android import Emulator

    return Emulator(serial=serial)


SerialOption = Annotated[str, typer.Option("--serial", help="adb serial of the target.")]


@android_app.command("boot")
def android_boot(
    avd: Annotated[str, typer.Option("--avd", help="AVD to boot. Defaults to $ANDROID_AVD.")] = "",
    serial: SerialOption = "emulator-5554",
    timeout: Annotated[int, typer.Option("--timeout", help="Seconds to wait for boot.")] = 240,
) -> None:
    """Boot a headless emulator and wait until it is actually usable.

    Headless, no snapshot, software GPU — the combination that boots reliably
    with no display attached. It waits for `sys.boot_completed` rather than for
    the process to exist: an emulator answers adb long before it can be tapped,
    and every command issued in that gap fails in a way that looks like the app.
    """
    from agentctl.android import DEFAULT_AVD

    pid = _emulator(serial).boot(avd or DEFAULT_AVD, timeout=timeout)
    typer.echo(f"booted (pid {pid})")


@android_app.command("shot")
def android_shot(
    out: Annotated[Path, typer.Argument(help="Where to write the PNG.")],
    serial: SerialOption = "emulator-5554",
) -> None:
    """Screenshot the device.

    **The PNG shows every unmasked field in clear.** Anything typed is never
    echoed by this tool, but a screenshot taken right after typing is not
    redacted — do not paste one into a ticket without looking at it first.
    """
    typer.echo(str(_emulator(serial).screenshot(out)))


@android_app.command("tap")
def android_tap(
    x: Annotated[int, typer.Argument(help="X, in AVD-native coordinates.")],
    y: Annotated[int, typer.Argument(help="Y, in AVD-native coordinates.")],
    shown_width: Annotated[
        int, typer.Option("--shown-width", help="Width the screenshot was rendered at.")
    ] = 0,
    shown_height: Annotated[int, typer.Option("--shown-height", help="Rendered height.")] = 0,
    serial: SerialOption = "emulator-5554",
) -> None:
    """Tap a point. Pass `--shown-*` when reading a downscaled screenshot.

    Coordinates are AVD-native. Reading them off a screenshot a tool rendered
    smaller and tapping as-is misses every target by the same ratio, which looks
    like the app ignoring you rather than like arithmetic.
    """
    from agentctl.android import scale_point

    if shown_width and shown_height:
        x, y = scale_point(x, y, shown=(shown_width, shown_height))
    _emulator(serial).tap(x, y)
    typer.echo(f"tapped {x},{y}")


@android_app.command("text")
def android_text(
    value: Annotated[str, typer.Argument(help="Text to type into the focused field.")],
    serial: SerialOption = "emulator-5554",
) -> None:
    """Type into the focused field. The value is never echoed.

    Spaces and shell metacharacters are escaped for the *device's* shell, which
    would otherwise eat them — that corrupted a password once, and the app
    answered "wrong credentials", which is indistinguishable from a real one.
    """
    _emulator(serial).type_text(value)
    typer.echo(f"typed {len(value)} characters")


@android_app.command("logcat")
def android_logcat(
    grep: Annotated[str | None, typer.Option("--grep", help="Only lines containing this.")] = None,
    tail: Annotated[int, typer.Option("--tail", help="How many lines.")] = 200,
    serial: SerialOption = "emulator-5554",
) -> None:
    """Dump the device log."""
    typer.echo(_emulator(serial).logcat(grep=grep, tail=tail))


@android_app.command("install")
def android_install(
    apk: Annotated[Path, typer.Argument(help="APK to install.")],
    serial: SerialOption = "emulator-5554",
) -> None:
    """Install an APK, replacing any existing copy."""
    typer.echo(_emulator(serial).install(apk))
