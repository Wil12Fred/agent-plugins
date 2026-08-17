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

from agentctl import css, mermaid, pdfassets, svgsprite
from agentctl import detect as detect_module
from agentctl import rules as rules_module
from agentctl import strays as strays_module
from agentctl.errors import ValidationError

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


@app.command("rules")
def rules_command(
    path: Annotated[Path, typer.Argument(help="Repository to inspect.")] = Path("."),
    as_json: Annotated[bool, typer.Option("--json", help="Emit the JSON envelope.")] = False,
    check: Annotated[
        bool, typer.Option("--check", help="Measure the checkable rules, not only list them.")
    ] = False,
) -> None:
    """Report the rules this repository declared for itself.

    `detect` answers what practices a repository has *adopted*; this answers what
    it has *decided*. They are different questions, and only the second can carry
    something like "every identifier and comment is English, except text quoted
    into a ticket" — a rule no directory listing reveals.

    Rules come from `.agent-rules.toml`. Prose documents that carry rules
    informally (`AGENTS.md`, a constitution, `CONTRIBUTING.md`) are listed
    separately, to be **read** rather than parsed: inventing a structured rule
    out of prose is how an auditor ends up enforcing something nobody wrote.

    With `--check` the measurable rules are measured, and the count of files each
    one weighed is reported alongside. That count is the point — "no violations"
    over zero files is not a pass, and without the number the two are
    indistinguishable.

    Exits 7 when `--check` finds a violation.
    """
    declaration = rules_module.load(path)

    lines = [str(path.resolve())]
    if declaration.declaration_file:
        lines.append(f"declared in: {declaration.declaration_file}")
    else:
        lines.append("declared in: nothing — this repository has not been given rules")
    lines.append("")
    for rule in declaration.rules:
        mark = "measurable" if rule.checkable else "read-only"
        lines.append(f"  [{mark}] {rule.name} ({rule.kind}) — {rule.rule}")
        if rule.exempt:
            lines.append(f"             except: {', '.join(rule.exempt)}")
    if declaration.prose_sources:
        lines.append("")
        lines.append(f"  prose to read: {', '.join(declaration.prose_sources)}")

    payload = declaration.as_dict()
    if check:
        result = rules_module.check(path, declaration)
        payload["check"] = result.as_dict()
        lines.append("")
        for name, count in result.checked.items():
            lines.append(f"  {name}: {count} file(s) weighed")
        for name in result.unmeasurable:
            lines.append(f"  {name}: not measurable by this tool — read it")
        for violation in result.violations:
            lines.append(f"  ! {violation.path}: {violation.detail}")
        _emit(payload, as_json=as_json, human="\n".join(lines))
        if result.violations:
            raise typer.Exit(7)
        return

    _emit(payload, as_json=as_json, human="\n".join(lines))


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


# --------------------------------------------------------------------------- #
# `agentctl dev` — the utilities that keep coming up
#
# None of these is about verification, which is the rest of this CLI's theme.
# They are here because they are genuinely general — a PDF is a PDF — and
# because the alternative was each project keeping its own copy of "extract the
# pages of a brand manual" and "render a Mermaid file without a browser".
# --------------------------------------------------------------------------- #

dev_app = typer.Typer(
    name="dev", help="General utilities: PDF, SVG, CSS, Mermaid.", no_args_is_help=True
)
app.add_typer(dev_app, name="dev")

pdf_app = typer.Typer(name="pdf", help="PDF asset extraction.", no_args_is_help=True)
css_app = typer.Typer(name="css", help="Stylesheet colour transforms.", no_args_is_help=True)
mermaid_app = typer.Typer(name="mermaid", help="Mermaid diagram rendering.", no_args_is_help=True)
dev_app.add_typer(pdf_app, name="pdf")
dev_app.add_typer(css_app, name="css")
dev_app.add_typer(mermaid_app, name="mermaid")


def _emit_result(data: Any, human: str | None = None, **_: Any) -> None:
    """The devtools commands were written against a richer output object.

    This keeps their call sites unchanged rather than rewriting four command
    bodies — a port should move code, not rephrase it, or the diff stops being
    reviewable and the behaviour stops being the thing that was tested.
    """
    _emit(
        data if isinstance(data, dict) else {"result": data},
        as_json=False,
        human=human if human is not None else json.dumps(data, indent=2, default=str),
    )


@pdf_app.command("extract")
def pdf_extract(
    pdf: Annotated[Path, typer.Argument(help="Source PDF (read-only).")],
    out_dir: Annotated[Path, typer.Option("--out-dir", help="Destination folder.")],
    mode: Annotated[
        list[pdfassets.Mode] | None,
        typer.Option("--mode", help="pages | images | svg (repeatable; default: pages)."),
    ] = None,
    prefix: Annotated[
        str | None, typer.Option("--prefix", help="Output filename prefix (default: PDF stem).")
    ] = None,
    fmt: Annotated[
        pdfassets.RasterFormat, typer.Option("--format", help="Raster format for --mode pages.")
    ] = pdfassets.RasterFormat.PNG,
    dpi: Annotated[int, typer.Option("--dpi", help="Render resolution for --mode pages.")] = 150,
    first: Annotated[int | None, typer.Option("--first", help="First page (1-based).")] = None,
    last: Annotated[int | None, typer.Option("--last", help="Last page (1-based).")] = None,
    min_width: Annotated[int, typer.Option("--min-width", help="Drop narrower images.")] = 0,
    min_height: Annotated[int, typer.Option("--min-height", help="Drop shorter images.")] = 0,
    drop_masks: Annotated[
        bool, typer.Option("--drop-masks", help="Drop grayscale images (usually alpha masks).")
    ] = False,
    keep_going: Annotated[
        bool, typer.Option("--keep-going", help="Continue when one page fails to convert.")
    ] = False,
) -> None:
    """Extract a PDF's pages, embedded images, and/or vector pages.

    The three modes answer different needs and are not interchangeable:
    ``pages`` rasterises composed layouts, ``images`` lifts embedded photography
    at native resolution (no quality loss), ``svg`` preserves vector paths so a
    logo comes out as a real SVG. Combine them by repeating ``--mode``.

    Needs poppler-utils; ImageMagick is optional and only enables the size
    filters.
    """
    rows = pdfassets.extract(
        pdf,
        out_dir,
        modes=list(mode) if mode else [pdfassets.Mode.PAGES],
        prefix=prefix,
        dpi=dpi,
        fmt=fmt,
        first=first,
        last=last,
        filters=pdfassets.Filters(
            min_width=min_width, min_height=min_height, drop_masks=drop_masks
        ),
        keep_going=keep_going,
    )
    _emit_result(rows, human=str(f"{len(rows)} asset(s) → {out_dir} (manifest.csv written)"))


@pdf_app.command("combine-svg")
def pdf_combine_svg(
    svg: Annotated[list[Path], typer.Argument(help="Source .svg files.")],
    out: Annotated[Path, typer.Option("--out", help="Destination sprite path.")],
    id_prefix: Annotated[str, typer.Option("--id-prefix", help="Prefix for every symbol id.")] = "",
    preview: Annotated[
        Path | None, typer.Option("--preview", help="Also write an HTML preview page.")
    ] = None,
) -> None:
    """Combine SVG files into one sprite of ``<symbol>`` elements.

    Ids come from the filenames, so ``<use href="#om-logo-01">`` keeps working
    across rebuilds. ``--preview`` renders every symbol on grey, which is the
    only way to see whether a white logo survived the extraction.
    """
    ids = svgsprite.combine(list(svg), out, id_prefix=id_prefix)
    if preview:
        svgsprite.write_preview(out, preview, ids)
    _emit_result(
        {
            "sprite": str(out),
            "preview": str(preview) if preview else None,
            "symbols": ids,
        }
    )


# --- css ---------------------------------------------------------------------


@css_app.command("hue-shift")
def css_hue_shift(
    paths: Annotated[list[Path], typer.Argument(help="CSS files or directories.")],
    delta: Annotated[
        float, typer.Option("--delta", help="Hue rotation in degrees for saturated colours.")
    ],
    gray_hue: Annotated[
        float, typer.Option("--gray-hue", help="Hue used to tint greys (default: warm gold).")
    ] = 40,
    white_threshold: Annotated[
        float, typer.Option("--white-threshold", help="L at/above which a colour is white-ish.")
    ] = 0.93,
    black_threshold: Annotated[
        float, typer.Option("--black-threshold", help="L at/below which a colour is black-ish.")
    ] = 0.13,
    write: Annotated[
        bool,
        typer.Option(
            # `--apply` is the workspace's name for "stop previewing and act";
            # `--write` is kept as an alias so existing invocations survive.
            "--apply",
            "--write",
            help="Actually rewrite the files (default: preview only).",
        ),
    ] = False,
    confirm: Annotated[
        bool, typer.Option("--confirm-prod-write", help="Confirm the in-place rewrite.")
    ] = False,
) -> None:
    """Rotate every hex colour in a stylesheet while preserving contrast.

    Lightness is left alone for saturated colours — that is what carries
    contrast. Greys, near-whites and near-blacks are re-tinted instead of
    rotated, because rotating a neutral either does nothing (pure black/white)
    or produces a tinted mess.

    Previews by default: the rewrite is **in place** and the originals are not
    backed up, so ``--apply`` has to be explicit (``--write`` is an alias).
    """
    files = css.collect(list(paths))
    if not files:
        raise ValidationError("no .css files found under the given paths")
    shift = css.HueShift(
        delta=delta,
        gray_hue=gray_hue,
        white_threshold=white_threshold,
        black_threshold=black_threshold,
    )
    if write:
        # `--apply` is the gate, and it defaults off: the command previews unless
        # you say otherwise. It rewrites in place with no backups, so it says
        # exactly what it is about to touch before doing it — a preview that
        # reads the same as a write is how eight stylesheets get mangled.
        typer.echo(
            f"rewriting colours in {len(files)} file(s) in place, without backups",
            err=True,
        )

    mapping, changed = css.shift_files(files, shift, write=write)
    if not write:
        typer.echo(
            f"preview only: {len(files)} file(s) unchanged — pass --apply to rewrite them",
            err=True,
        )
    _emit_result(
        {
            "files": len(files),
            "files_changed": changed if write else 0,
            "written": write,
            "colors": dict(sorted(mapping.items())),
        },
        human="\n".join(
            [f"{old} -> {new}" for old, new in sorted(mapping.items())]
            + [f"{len(mapping)} unique colours across {len(files)} file(s)"]
        ),
    )


# --- clipboard ---------------------------------------------------------------


@mermaid_app.command("render")
def mermaid_render(
    target: Annotated[Path, typer.Argument(help="A .md/.mmd file, or a directory of them.")],
    out_dir: Annotated[
        Path | None, typer.Option("--out", help="Destination (default: <input dir>/rendered).")
    ] = None,
    svg: Annotated[bool, typer.Option("--svg", help="Also emit a transparent SVG.")] = False,
    confirm: Annotated[
        bool, typer.Option("--confirm-prod-write", help="Confirm replacing existing images.")
    ] = False,
) -> None:
    """Render Mermaid diagrams to PNG (and optionally SVG).

    Every ```` ```mermaid ```` fence in a Markdown file becomes its own image;
    a file with several yields ``<base>-01.png``, ``<base>-02.png``, … The
    system Chrome is reused through a puppeteer config, so mermaid-cli does not
    download its own 300MB Chromium.

    Writing new images into a fresh ``rendered/`` directory needs no
    confirmation — nothing is lost. **Replacing** images that already exist
    does, because several of this repo's committed diagrams are rendered
    exactly this way and a re-render with a stale source would overwrite them
    with no trace.
    """
    existing = [
        path for path in mermaid.planned_outputs(target, out_dir=out_dir, svg=svg) if path.is_file()
    ]
    if existing:
        # Say what is about to be replaced. Rendering into a directory that
        # already holds images is the normal case, and silently overwriting one
        # somebody hand-edited is the failure worth naming out loud.
        typer.echo(
            f"overwriting {len(existing)} already-rendered image(s) in {existing[0].parent}",
            err=True,
        )
    rendered = mermaid.render(target, out_dir=out_dir, svg=svg)
    _emit_result(
        [{"source": str(r.source), "output": str(r.output)} for r in rendered],
        human=str(f"{len(rendered)} image(s)"),
    )


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
