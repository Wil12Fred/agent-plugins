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
from typer import _click as click

from agentctl import (
    css,
    drive,
    htmlpdf,
    mermaid,
    pdfassets,
    pptxdeck,
    pptxlayout,
    pptxsplit,
    svgsprite,
)
from agentctl import detect as detect_module
from agentctl import portable as portable_module
from agentctl import rules as rules_module
from agentctl import strays as strays_module
from agentctl.errors import AgentctlError, NotFoundError, UsageError, ValidationError

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


@app.command("portable")
def portable_command(
    path: Annotated[Path, typer.Argument(help="Repository to sweep.")] = Path("."),
    target: Annotated[
        Path | None,
        typer.Option("--target", help="Checkout of the shared repository, to detect duplicates."),
    ] = None,
    expect_language: Annotated[
        str | None,
        typer.Option("--expect-language", help="Language the target requires, e.g. english."),
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option("--exclude", help="Glob to skip, for already-extracted trees. Repeatable."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the JSON envelope.")] = False,
) -> None:
    """Find code that is not about this project, so it can be shared instead of rewritten.

    A repository accumulates two kinds of code and stops telling them apart:
    what the business does, and mechanism that would work anywhere. The second
    is invisible because it lives in the same directories as the first and
    nobody re-reads a helper that already works.

    The test is mechanical, not editorial — how many of a file's lines mention
    the project, using the vocabulary declared in `.agent-rules.toml`. "Does
    this feel reusable?" is a question that gets a confident answer either way;
    "how many of these 121 lines name the company" has one answer, and a reader
    who disagrees can open the file and count.

    Pass `--target` at a checkout of the shared repository and anything already
    there is reported as a **duplicate**, ranked above every opportunity.
    Extraction by copying instead of moving is the half that goes wrong, and it
    goes wrong silently: both copies work, so nothing fails, and the divergence
    surfaces when a fix in one does not appear in the other.

    Exits 7 when a duplicate is found. Opportunities alone exit 0 — they are
    not defects and should not fail anybody's pipeline.
    """
    result = portable_module.survey(
        path,
        target=target,
        expect_language=expect_language,
        exclude=exclude or (),
    )
    header = [
        f"{path.resolve()}",
        f"measured against {len(result.vocabulary.terms)} project term(s) "
        f"from {result.vocabulary.source}",
        f"{result.scanned} code file(s) weighed, {len(result.candidates)} candidate(s)",
        "",
    ]
    body = portable_module.summarise(result.candidates) or ["  nothing portable found"]
    _emit(result.as_dict(), as_json=as_json, human="\n".join(header + body))
    if result.duplicates:
        raise typer.Exit(7)


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
    name="dev", help="General utilities: PDF, PPTX, SVG, CSS, Mermaid.", no_args_is_help=True
)
app.add_typer(dev_app, name="dev")

pdf_app = typer.Typer(name="pdf", help="PDF asset extraction.", no_args_is_help=True)
css_app = typer.Typer(name="css", help="Stylesheet colour transforms.", no_args_is_help=True)
mermaid_app = typer.Typer(name="mermaid", help="Mermaid diagram rendering.", no_args_is_help=True)
pptx_app = typer.Typer(
    name="pptx", help="PowerPoint decks: read, extract, edit.", no_args_is_help=True
)
dev_app.add_typer(pdf_app, name="pdf")
dev_app.add_typer(css_app, name="css")
dev_app.add_typer(mermaid_app, name="mermaid")
dev_app.add_typer(pptx_app, name="pptx")


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


@pdf_app.command("from-html")
def pdf_from_html(
    source: Annotated[Path, typer.Argument(help="Source .html (read-only).")],
    out: Annotated[Path, typer.Option("--out", help="Destination .pdf.")],
    paper: Annotated[
        str | None,
        typer.Option(
            "--paper", help="A4 | A3 | A5 | Letter | Legal | Tabloid. Default: the page's own."
        ),
    ] = None,
    margin: Annotated[
        str | None, typer.Option("--margin", help="CSS length, e.g. 12mm. Default: the page's own.")
    ] = None,
    timeout: Annotated[float, typer.Option("--timeout", help="Seconds to allow.")] = 120.0,
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Replace an existing output file.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the JSON envelope.")] = False,
) -> None:
    """Print an HTML file to PDF with a headless browser.

    The usual answers to "make a PDF" are LaTeX and LibreOffice. On a machine
    with neither, a third is often already in a cache directory: Chromium ships a
    print engine, and it is the same one that rendered the page you checked. For
    a document authored as HTML this is not a fallback, it is the right tool.

    Not a substitute for `dev pptx pdf` — a browser cannot open a `.pptx` at all.

    Three things it gets wrong unless told: it stamps every page with the URL and
    date (disabled here, always); paper size is not a flag but an `@page` rule,
    so without `--paper` you get US Letter wherever you are; and it can exit 0
    having written nothing, so the result is checked for the `%PDF-` magic rather
    than trusted.

    Finds a browser via `CHROME_BINARY`, then `PATH`, then Playwright's download
    cache — and always reports which, because a silently-chosen engine makes the
    output depend on something nobody mentioned.
    """
    result = htmlpdf.render(
        source, out, paper=paper, margin=margin, timeout=timeout, overwrite=overwrite
    )
    megabytes = result["bytes"] / 1e6 if isinstance(result["bytes"], int) else 0.0
    _emit(
        result,
        as_json=as_json,
        human=(
            f"{result['pages']} page(s), {megabytes:.1f} MB → {out}\n"
            f"  {result['renderer']} (via {result['found_via']}) · {result['paper']}"
        ),
    )


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


# --- pptx --------------------------------------------------------------------
#
# A .pptx is a zip of XML, so all four commands are stdlib. The library people
# reach for (`python-pptx`) is the right answer for *authoring* slides and the
# wrong one to make every installing project carry for reading and find/replace.


def _replacement_pairs(replace: list[str] | None, mapping: Path | None) -> list[tuple[str, str]]:
    """Build the old→new list from `--replace old=new` and/or `--map file.json`.

    The split is on the **first** `=`, so the replacement may contain one and the
    text being matched may not. `--map` is the way out of that, and the way to
    pass text a shell would mangle.
    """
    pairs: list[tuple[str, str]] = []
    for item in replace or []:
        old, sep, new = item.partition("=")
        if not sep or not old:
            raise UsageError(
                f"not an old=new pair: {item!r}",
                detail="use --map for text containing '=' on the left",
            )
        pairs.append((old, new))
    if mapping is not None:
        if not mapping.is_file():
            raise NotFoundError(f"not a file: {mapping}")
        loaded = json.loads(mapping.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValidationError(
                f"{mapping} is not a JSON object", detail='expected {"old": "new", ...}'
            )
        pairs.extend((str(key), str(value)) for key, value in loaded.items())
    return pairs


@pptx_app.command("inspect")
def pptx_inspect(
    deck: Annotated[Path, typer.Argument(help="Source .pptx (read-only).")],
    as_json: Annotated[bool, typer.Option("--json", help="Emit the JSON envelope.")] = False,
    show_text: Annotated[
        bool, typer.Option("--text/--no-text", help="Include each slide's text.")
    ] = True,
) -> None:
    """Report a deck's slides, their text and notes, and where its images live.

    Slides come out in **display order**, which is not the order of the part
    names: `slide7.xml` is the seventh slide that was created, and any deck that
    has been reordered disagrees with itself. The order is read from
    `presentation.xml`.

    Media is reported in three groups, and the split is the useful part. Artwork
    on the slides is what an extraction gets; artwork on the layouts or the
    master is where a logo almost always is, and looking for it slide by slide
    is how you conclude a deck has none; unreferenced files are carried in the
    archive and drawn nowhere.
    """
    result = pptxdeck.inspect(deck)
    lines = [str(deck), f"{len(result.slides)} slide(s)", ""]
    for slide in result.slides:
        head = f"  {slide.index:>3}. {Path(slide.part).name}"
        marks = []
        if slide.media:
            marks.append(f"{len(slide.media)} image(s)")
        if slide.notes:
            marks.append("notes")
        lines.append(f"{head}{'  — ' + ', '.join(marks) if marks else ''}")
        if show_text:
            lines.extend(f"        {line}" for line in slide.text)
    lines += [
        "",
        f"media: {len(result.media)} file(s) — "
        f"{len(result.slide_media)} on slides, "
        f"{len(result.chrome_media)} on layouts/master, "
        f"{len(result.unreferenced_media)} unreferenced",
    ]
    _emit(result.as_dict(), as_json=as_json, human="\n".join(lines))


@pptx_app.command("extract")
def pptx_extract(
    deck: Annotated[Path, typer.Argument(help="Source .pptx (read-only).")],
    out_dir: Annotated[Path, typer.Option("--out-dir", help="Destination folder.")],
    prefix: Annotated[
        str | None, typer.Option("--prefix", help="Output filename prefix (default: deck stem).")
    ] = None,
    slide: Annotated[
        list[int] | None,
        typer.Option("--slide", help="Restrict to these slides, by display position. Repeatable."),
    ] = None,
    min_bytes: Annotated[
        int, typer.Option("--min-bytes", help="Drop files smaller than this (icons, spacers).")
    ] = 0,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the JSON envelope.")] = False,
) -> None:
    """Extract the deck's images at native resolution, with a manifest.

    Bytes are copied straight out of the archive — no decode and no re-encode —
    so a photograph keeps exactly the resolution and compression it was embedded
    with. `manifest.csv` records which slides use each file and what it was
    called inside the deck, which is the only handle `replace-image` accepts.

    Without `--slide`, everything any part references is extracted, layouts and
    master included. With it, only what those slides use: a run filtered to one
    slide should not quietly include the logo that sits on all forty.
    """
    rows = pptxdeck.extract_media(
        deck,
        out_dir,
        prefix=prefix,
        slides=list(slide) if slide else None,
        min_bytes=min_bytes,
    )
    _emit(
        {"out_dir": str(out_dir), "assets": rows},
        as_json=as_json,
        human=f"{len(rows)} image(s) → {out_dir} (manifest.csv written)",
    )


@pptx_app.command("export")
def pptx_export(
    deck: Annotated[Path, typer.Argument(help="Source .pptx (read-only).")],
    out_dir: Annotated[Path, typer.Option("--out-dir", help="Destination folder.")],
    title: Annotated[
        str | None, typer.Option("--title", help="H1 for index.md (default: deck stem).")
    ] = None,
    prefix: Annotated[
        str | None, typer.Option("--prefix", help="Image filename prefix (default: deck stem).")
    ] = None,
    images_dir: Annotated[
        Path | None,
        typer.Option("--images-dir", help="Put images here instead of <out-dir>/images."),
    ] = None,
    max_width: Annotated[
        int, typer.Option("--max-width", help="Shrink images wider than this. 0 = keep native.")
    ] = 0,
    colors: Annotated[
        int, typer.Option("--colors", help="Quantize to N colours (e.g. 256). 0 = leave alone.")
    ] = 0,
    include_media: Annotated[
        bool, typer.Option("--include-media", help="Also write video and other non-image parts.")
    ] = False,
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Replace the contents of a non-empty folder.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the JSON envelope.")] = False,
) -> None:
    """Turn a deck into a readable folder: `index.md`, `images/`, `manifest.csv`.

    A `.pptx` is a delivery format and a terrible archive format — opaque to
    grep, to review and to any diff. This writes the same content as markdown
    with the images beside it, so a deck handed over as a requirement can live
    in a repository and be read like anything else in it.

    `--max-width` matters more than it looks. A deck's artwork is embedded at
    native resolution: one 27-slide review deck held 213 MB of it, which is fine
    inside the deck and impossible in a repository. Only images wider than the
    bound are rewritten, so nothing already small is re-encoded. Needs
    ImageMagick; without it the images are written at full size rather than
    dropped, and the manifest still records what they are.

    `--images-dir` sends the images elsewhere and links to them relatively. The
    markdown is the half worth committing and the images are usually the half
    that must not be — they are recoverable from wherever the deck came from,
    and they are the entire weight. Doing that split by hand means rewriting
    every link in the file.

    `--colors` is the cheaper half of the same problem and usually the one to
    reach for first: the same deck went from 20 MB to 7.4 MB at `--colors 256`
    **without losing a pixel of resolution**, and resolution is what keeps an
    annotation readable. Photographs are where quantizing shows — look at the
    output before committing to a low count.

    Video is left out unless `--include-media` — one embedded clip was 94 MB on
    its own. Whatever is skipped is named in the result, never silently.
    """
    result = pptxdeck.export_folder(
        deck,
        out_dir,
        prefix=prefix,
        title=title,
        images_dir=images_dir,
        max_width=max_width,
        colors=colors,
        include_media=include_media,
        overwrite=overwrite,
    )
    skipped = result["skipped_media"]
    megabytes = result["bytes"] / 1e6 if isinstance(result["bytes"], int) else 0.0
    human = (
        f"{result['slides']} slide(s), {result['images']} image(s), {megabytes:.1f} MB → {out_dir}"
    )
    if isinstance(skipped, list) and skipped:
        human += f"\n  skipped (not an image): {', '.join(skipped)}"
    _emit(result, as_json=as_json, human=human)


@pptx_app.command("replace-text")
def pptx_replace_text(
    deck: Annotated[Path, typer.Argument(help="Source .pptx (never modified).")],
    out: Annotated[Path, typer.Option("--out", help="Destination .pptx.")],
    replace: Annotated[
        list[str] | None,
        typer.Option("--replace", help="`old=new`, split on the first `=`. Repeatable."),
    ] = None,
    mapping: Annotated[
        Path | None, typer.Option("--map", help='JSON object of {"old": "new"}.')
    ] = None,
    keep_runs: Annotated[
        bool,
        typer.Option("--keep-runs", help="Preserve mid-paragraph formatting; skips split matches."),
    ] = False,
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Replace an existing output file.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the JSON envelope.")] = False,
) -> None:
    """Find and replace text across a deck's slides, into a new file.

    Matching sees the whole paragraph, not one run at a time. PowerPoint splits a
    paragraph at every formatting change and at boundaries nothing on screen
    reveals, so `Total: 42` is routinely three runs and a per-run search misses
    text that is plainly visible.

    The change is still written into the individual runs whenever that gives the
    same result, so formatting survives. Only a match that really crosses a run
    boundary flattens the paragraph onto its first run's formatting, and the
    report names those edits; `--keep-runs` skips them rather than pay for it.

    Matching is exact and case-sensitive. Only the slides are touched: not the
    speaker notes, not the layouts, not the master. Every other byte of the
    archive is copied through unchanged. Editing in place is refused.
    """
    applied = pptxdeck.replace_text(
        deck,
        out,
        _replacement_pairs(replace, mapping),
        keep_runs=keep_runs,
        overwrite=overwrite,
    )
    collapsed = sum(1 for item in applied if item.collapsed_runs > 1)
    lines = [f"{len(applied)} replacement(s) → {out}"]
    lines += [f"  slide {item.slide}: {item.before!r} → {item.after!r}" for item in applied]
    if collapsed and not keep_runs:
        lines.append(
            f"  note: {collapsed} paragraph(s) spanned several runs and now use the first one's"
            " formatting — re-run with --keep-runs to refuse that"
        )
    _emit(
        {"out": str(out), "replacements": [item.as_dict() for item in applied]},
        as_json=as_json,
        human="\n".join(lines),
    )


@pptx_app.command("replace-image")
def pptx_replace_image(
    deck: Annotated[Path, typer.Argument(help="Source .pptx (never modified).")],
    out: Annotated[Path, typer.Option("--out", help="Destination .pptx.")],
    media: Annotated[
        str, typer.Option("--media", help="Archive name, e.g. `image3.png`, from `inspect`.")
    ],
    image: Annotated[Path, typer.Option("--with", help="Replacement file, same extension.")],
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Replace an existing output file.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the JSON envelope.")] = False,
) -> None:
    """Swap one embedded image, copying the rest of the archive verbatim.

    The extension has to match the part being replaced. That is not tidiness:
    `[Content_Types].xml` declares the type per extension, so a PNG written over
    `image3.jpeg` gives a deck PowerPoint calls corrupt, with nothing said at
    write time. Convert first, then replace.

    The shape's frame lives in the slide and does not move, so an image with a
    different aspect ratio arrives stretched. Match the aspect ratio of what you
    extracted, or edit the slide XML by hand — `unpack` is there for that.
    """
    result = pptxdeck.replace_media(deck, out, media=media, image=image, overwrite=overwrite)
    _emit(
        result,
        as_json=as_json,
        human=f"{result['replaced']} ← {image} ({result['bytes']} bytes) → {out}",
    )


@pptx_app.command("split")
def pptx_split(
    deck: Annotated[Path, typer.Argument(help="Source .pptx (read-only).")],
    out: Annotated[Path, typer.Option("--out", help="Destination .pptx.")],
    slides: Annotated[str, typer.Option("--slides", help='Display positions, e.g. "1,3,5-7".')],
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Replace an existing output file.")
    ] = False,
    stub_media_over: Annotated[
        int | None,
        typer.Option(
            "--stub-media-over",
            help="Empty any embedded media larger than this many MB. "
            "Google Drive converts a .pptx to Slides only up to 100 MB.",
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the JSON envelope.")] = False,
) -> None:
    """Cut a deck down to the slides you name, dropping everything else.

    A review deck covering twenty tasks is nineteen tasks of noise to whoever
    picks up the twentieth. This writes a real `.pptx` holding only the slides
    given, and the pruning is where the weight goes: the kept set is computed by
    following relationships, so media nothing references is dropped. On a real
    deck, 223 MB became 1.9 MB for four slides.

    Numbers are **display positions** — what a person reads off the deck — and an
    out-of-range one is refused rather than clipped, because a range that
    silently shrinks omits exactly the slide somebody meant to include.
    """
    wanted = pptxsplit.parse_slides(slides, 10**6)
    result = pptxsplit.split(
        deck,
        out,
        wanted,
        overwrite=overwrite,
        stub_media_over=None if stub_media_over is None else stub_media_over * 1_048_576,
    )
    before = result["source_bytes"] / 1e6 if isinstance(result["source_bytes"], int) else 0.0
    after = result["bytes"] / 1e6 if isinstance(result["bytes"], int) else 0.0
    stubbed = result.get("stubbed") or []
    note = f", {len(stubbed)} media vaciado(s)" if stubbed else ""
    _emit(
        result,
        as_json=as_json,
        human=f"{len(wanted)} slide(s) → {out}  ({after:.1f} MB, from {before:.0f} MB{note})",
    )


@pptx_app.command("html")
def pptx_html(
    deck: Annotated[Path, typer.Argument(help="Source .pptx (read-only).")],
    out_dir: Annotated[Path, typer.Option("--out-dir", help="Destination folder.")],
    title: Annotated[str | None, typer.Option("--title", help="Page title.")] = None,
    max_width: Annotated[
        int, typer.Option("--max-width", help="Shrink images wider than this. 0 = native.")
    ] = 1600,
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Replace the contents of a non-empty folder.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the JSON envelope.")] = False,
) -> None:
    """Lay the deck out as HTML — one page per slide, shapes where the slide puts them.

    `export` gives a deck's content; this gives its *arrangement*. For a review
    deck, where the message is *this* annotation pointing at *that* screenshot,
    the arrangement is the content, and a list of paragraphs loses it.

    **No LibreOffice.** Every shape records its own offset and extent in EMU, so
    placing them is unit conversion rather than layout — pictures, text with its
    per-run size and colour, the connectors a reviewer points with, groups, and
    the background inherited from the layout or master.

    It is an approximation and names its own limits: tables, charts and SmartArt
    are drawn as a labelled outline rather than dropped, and the count comes back
    in the result. Text reflow and placeholder inheritance are the parts a real
    engine does and this does not, so a text box can overflow where PowerPoint
    would have shrunk it.

    Pair with `dev pdf from-html` to get a PDF; the `@page` size is already set to
    the slide, so one slide is one page.
    """
    result = pptxlayout.build(deck, out_dir, title=title, max_width=max_width, overwrite=overwrite)
    approximated = result["approximated"]
    page = result["slide_pt"]
    size = f"{page[0]}×{page[1]}pt" if isinstance(page, list) else "unknown"
    human = (
        f"{result['slides']} slide(s), {result['shapes']} shape(s), {result['images']} image(s) "
        f"→ {out_dir}\n  page {size}"
    )
    if isinstance(approximated, list) and approximated:
        names = ", ".join(approximated)
        human += f"\n  drawn as an outline ({result['approximated_count']}): {names}"
    _emit(result, as_json=as_json, human=human)


@pptx_app.command("pdf")
def pptx_pdf(
    deck: Annotated[Path, typer.Argument(help="Source .pptx (read-only).")],
    out: Annotated[Path, typer.Option("--out", help="Destination .pdf.")],
    timeout: Annotated[
        float, typer.Option("--timeout", help="Seconds to allow LibreOffice.")
    ] = 600.0,
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Replace an existing output file.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the JSON envelope.")] = False,
) -> None:
    """Render a deck to PDF, faithfully, through LibreOffice.

    The only command here that is not stdlib, because it is the only one that
    needs a layout engine rather than a parser. `export` gives you the deck's
    *content* as markdown; this gives you what the slides *look like*, which is
    what you want when the deck is a design review and the layout is the message.

    **Needs LibreOffice** and refuses by name when it is absent. There is no
    pure-Python fallback on offer: rendering a slide means resolving the theme,
    the fonts and the embedded objects, and a substitute would produce a document
    that is not what the deck looks like — which is worse than refusing, because
    the reader cannot tell by looking.

    A large deck is minutes, not seconds: LibreOffice decodes every embedded
    asset, so a 200 MB deck with video earns the default ten-minute allowance.
    """
    result = pptxdeck.to_pdf(deck, out, timeout=timeout, overwrite=overwrite)
    megabytes = result["bytes"] / 1e6 if isinstance(result["bytes"], int) else 0.0
    _emit(
        result,
        as_json=as_json,
        human=f"{result['pages']} page(s), {megabytes:.1f} MB → {out}  [{result['renderer']}]",
    )


@pptx_app.command("unpack")
def pptx_unpack(
    deck: Annotated[Path, typer.Argument(help="Source .pptx (read-only).")],
    out_dir: Annotated[Path, typer.Option("--out-dir", help="Destination folder (recreated).")],
    as_json: Annotated[bool, typer.Option("--json", help="Emit the JSON envelope.")] = False,
) -> None:
    """Explode the archive so the XML can be read and edited by hand.

    The escape hatch. The other commands are named operations on a deck; this is
    for what none of them covers. Nothing in this tool hides the format, so
    dropping to the XML is a step down rather than a different tool — repack
    with `cd <dir> && zip -r ../new.pptx .` and keep `[Content_Types].xml` in it.
    """
    names = pptxdeck.unpack(deck, out_dir)
    slides = [name for name in names if name.startswith("ppt/slides/slide")]
    _emit(
        {"out_dir": str(out_dir), "parts": names},
        as_json=as_json,
        human=f"{len(names)} part(s) → {out_dir} ({len(slides)} slide xml)",
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
    """Console-script entry point: dispatch, then render our own failures.

    Without the `AgentctlError` arm a refusal reaches the terminal as a
    syntax-highlighted traceback pointing at this file, which reads as *this
    tool is broken* rather than *configure it*. `agentctl portable` on a
    repository with no declared vocabulary did exactly that, and its message
    already said precisely what to add.

    `standalone_mode=False` is the other half: Click otherwise handles a usage
    error itself, printing to stderr and exiting 2 with an **empty stdout** —
    under `--json` too, so a caller parsing stdout cannot tell a mistyped flag
    from a crash.

    Anything that is not ours keeps its traceback. Swallowing a real bug into a
    tidy message is how a crash gets reported as a configuration problem.
    """
    try:
        app(standalone_mode=False)
    except AgentctlError as exc:
        typer.secho(f"error {exc}", err=True, fg=typer.colors.RED)
        sys.exit(exc.exit_code)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        sys.exit(130)
    except click.exceptions.Abort:  # pragma: no cover - interactive only
        sys.exit(130)
    except click.exceptions.Exit as exc:
        sys.exit(exc.exit_code)
    except click.exceptions.ClickException as exc:
        typer.secho(f"error {exc.format_message()}", err=True, fg=typer.colors.RED)
        typer.secho("run with --help for the usage", err=True)
        sys.exit(2)


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


# --- drive --------------------------------------------------------------------

drive_app = typer.Typer(
    name="drive", help="Deliver task packets to a Google Drive folder.", no_args_is_help=True
)
app.add_typer(drive_app, name="drive")


@drive_app.command("deliver")
def drive_deliver(
    spec: Annotated[Path, typer.Argument(help="The plan, as JSON.")],
    work_dir: Annotated[
        Path, typer.Option("--work-dir", help="Where the split decks are written locally.")
    ] = Path(".agentctl-deliver"),
    confirm: Annotated[
        bool, typer.Option("--confirm-write", help="Actually create folders and upload.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the JSON envelope.")] = False,
) -> None:
    """Build `<ticket>/<task>/` in a Drive folder and fill it from a plan.

    One JSON document says which slides of which decks belong to which task, and
    what else goes with them. Each task gets a folder holding a `.pptx` cut to
    exactly its slides, plus the files listed beside it — so whoever picks the
    task up finds that task, not the whole deck.

    ```json
    {
      "ticketName": "OPER-1010",
      "drivePath": "https://drive.google.com/drive/folders/<id>",
      "tasks": [
        {"taskName": "T1 — slogan",
         "sources": [{"pptPath": "deck.pptx", "pages": [3, 9, 21]}],
         "attachments": ["logo.png"]}
      ]
    }
    ```

    **Nothing is written without `--confirm-write`.** The dry run still splits
    every deck, so a plan naming a slide that does not exist fails before
    anything reaches a shared drive rather than halfway through.

    Safe to run twice: folders are found before they are created, and a file
    already there is updated rather than duplicated — Drive will happily keep two
    files with one name in a folder, which is how a reader opens the stale one.

    Needs a Drive-scoped token, from `GOOGLE_OAUTH_TOKEN` or `gcloud`. A plain
    `gcloud auth login` does not grant it; the refusal names the flag.
    """
    plan = drive.load_plan(spec)
    result = drive.deliver(plan, work_dir, confirm=confirm)

    lines = [f"{result['ticket']} — {result['tasks']} task(s), {result['files']} file(s)"]
    tree = result["tree"]
    if isinstance(tree, list):
        for entry in tree:
            if "task" in entry:
                lines.append(f"  {entry['task']}/")
                for spec_row in entry.get("files", []):
                    size = spec_row.get("bytes", 0)
                    size_mb = size / 1e6 if isinstance(size, int) else 0.0
                    origin = f"  ← {spec_row['from']} {spec_row.get('slides', '')}".rstrip()
                    lines.append(f"      {spec_row['name']}  ({size_mb:.1f} MB){origin}")
    if not result["confirmed"]:
        lines.append("\n  DRY RUN — nothing was written. Pass --confirm-write.")
    elif result["url"]:
        lines.append(f"\n  {result['url']}")
    _emit(result, as_json=as_json, human="\n".join(lines))
