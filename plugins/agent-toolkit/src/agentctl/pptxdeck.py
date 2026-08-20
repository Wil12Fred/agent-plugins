"""Read, extract from and edit a ``.pptx`` — no rendering engine, no dependency.

A presentation is a ZIP of XML parts, so everything below is stdlib. That is a
deliberate choice rather than a shortcut: this file ships inside a plugin that
is installed by copying its directory, so every dependency it takes is one the
installing project inherits. Reading a deck, pulling its artwork out and
swapping its strings need none.

The library that would otherwise be here (``python-pptx``) is still the right
answer for *authoring* — adding slides, creating shapes, moving things on the
canvas. It is not needed for any of the four operations below, and the moment
it is, it belongs in an extra rather than in the base install.

Four operations, and they answer different questions:

* :func:`inspect` — what is in the deck: the slides in **display order**, the
  text of each, its speaker notes, and which media it references.
* :func:`extract_media` — the artwork at native resolution, attributed to the
  slide that uses it, with a ``manifest.csv``. No re-encoding, so a photograph
  comes out exactly as it went in.
* :func:`replace_text` — find/replace across the deck, writing a new file.
* :func:`replace_media` — swap one image's bytes, leaving every other byte of
  the archive as it was.

Three things about the format that cost time if you assume otherwise, and that
this module handles rather than documents-and-leaves:

**``slide7.xml`` is not the seventh slide.** Part names are assigned when a
slide is created and never renumbered, so any deck that has been reordered has
filenames that disagree with the running order. The order lives in
``ppt/presentation.xml``'s ``<p:sldIdLst>``, resolved through the relationship
ids — which is where :func:`slide_order` reads it from.

**``ppt/media/`` is not the slides' artwork.** It is *every* part's artwork,
including the layouts', the master's and the theme's. A logo that appears on
every slide is usually referenced by the master and by no slide at all, so a
flat listing of that directory over-reports, and per-slide attribution
under-reports unless the layouts are counted separately. :func:`inspect`
reports both, and names the parts that refer to each file.

**A sentence is not a run.** PowerPoint splits a paragraph into runs at every
formatting change, at a spellcheck boundary, and sometimes for no reason a
reader can see — so ``"Total: 42"`` can be three runs, and a find/replace that
works one run at a time silently misses text that is plainly on the screen.
:func:`replace_text` therefore matches against the joined paragraph, but still
writes the change *into the runs* whenever that gives the same answer, so
formatting survives. Only a match that truly crosses a boundary collapses the
paragraph onto its first run, which the report names and ``keep_runs=True``
refuses outright. Deciding on the run count instead would have flattened five
paragraphs of a real deck that needed nothing of the sort.

Reads the source (never modified). Writes only where told to.
"""

from __future__ import annotations

import csv
import os.path
import posixpath
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape, unescape

from agentctl.errors import NotFoundError, UsageError, ValidationError
from agentctl.pdfassets import identify

# --------------------------------------------------------------------------- #
# The format
# --------------------------------------------------------------------------- #

NS_PRESENTATION = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS_DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"

MEDIA_DIR = "ppt/media/"
PRESENTATION_PART = "ppt/presentation.xml"
CONTENT_TYPES_PART = "[Content_Types].xml"

MEASURABLE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
)
"""What may be handed to ImageMagick for its dimensions.

A deck's media directory is not only images: an embedded video, an EMF drawing
or an OLE object all live there. Asking `identify` for the size of a 93 MB mp4
makes it decode the video, which is why this is an allow-list rather than a
"skip the ones we know are bad" list — the next format nobody thought of fails
the safe way.
"""

IDENTIFY_TIMEOUT = 10.0
"""Backstop for a raster pathological enough to be slow anyway. Reporting 0×0
for one file beats a command that never returns."""

_ENTITIES = {"&apos;": "'", "&quot;": '"'}
_ESCAPES = {"'": "&apos;", '"': "&quot;"}

# `<a:p>` cannot nest, so a non-greedy span is exact rather than approximate.
# The self-closing `<a:p/>` form is not matched on purpose: it holds no text.
_PARAGRAPH = re.compile(r"<a:p\b[^>]*>.*?</a:p>", re.DOTALL)
_RUN_TEXT = re.compile(r"(<a:t(?:\s[^>]*)?>)(.*?)(</a:t>)", re.DOTALL)


@dataclass(frozen=True)
class Slide:
    """One slide, in display order."""

    index: int
    """1-based position on screen — not the number in the part name."""

    part: str
    """Zip path, e.g. ``ppt/slides/slide3.xml``. Kept because it is the only
    stable handle: two slides can move, the part name never changes."""

    text: tuple[str, ...]
    notes: tuple[str, ...]
    media: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "part": self.part,
            "text": list(self.text),
            "notes": list(self.notes),
            "media": list(self.media),
        }


@dataclass(frozen=True)
class Deck:
    """What :func:`inspect` measured."""

    path: Path
    slides: tuple[Slide, ...]
    media: tuple[str, ...]
    """Every part under ``ppt/media/``, whether or not anything refers to it."""

    references: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """media part -> the parts that reference it. A file with an empty tuple is
    carried in the archive and drawn nowhere."""

    @property
    def slide_media(self) -> tuple[str, ...]:
        seen: list[str] = []
        for slide in self.slides:
            for item in slide.media:
                if item not in seen:
                    seen.append(item)
        return tuple(seen)

    @property
    def chrome_media(self) -> tuple[str, ...]:
        """Media referenced only by a layout, the master or the theme.

        This is where a client's logo almost always is, and looking for it in
        the slides is how you conclude a deck has no logo.
        """
        on_slides = set(self.slide_media)
        return tuple(
            item for item in self.media if item not in on_slides and self.references.get(item)
        )

    @property
    def unreferenced_media(self) -> tuple[str, ...]:
        return tuple(item for item in self.media if not self.references.get(item))

    def as_dict(self) -> dict[str, object]:
        return {
            "file": str(self.path),
            "slides": [slide.as_dict() for slide in self.slides],
            "media": {
                "total": len(self.media),
                "on_slides": list(self.slide_media),
                "on_layouts_or_master": list(self.chrome_media),
                "unreferenced": list(self.unreferenced_media),
            },
            "references": {key: list(value) for key, value in self.references.items()},
        }


@dataclass(frozen=True)
class Replacement:
    """One applied substitution, and what it cost."""

    slide: int
    part: str
    before: str
    after: str
    collapsed_runs: int
    """How many runs were merged to make this edit — **0** when none were.

    Non-zero means the match crossed a run boundary, so formatting that varied
    inside the paragraph is now uniform. It is not the paragraph's run count: a
    multi-run paragraph edited inside one of its runs reports 0."""

    def as_dict(self) -> dict[str, object]:
        return {
            "slide": self.slide,
            "part": self.part,
            "before": self.before,
            "after": self.after,
            "collapsed_runs": self.collapsed_runs,
        }


# --------------------------------------------------------------------------- #
# Opening
# --------------------------------------------------------------------------- #


def _open(path: Path) -> zipfile.ZipFile:
    """Open a deck, or say which of the two failures happened.

    A ``.ppt`` renamed to ``.pptx`` is the common one, and ``BadZipFile`` on its
    own reads as a corrupt download rather than as the wrong format.
    """
    if not path.is_file():
        raise NotFoundError(f"not a file: {path}")
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ValidationError(
            f"not an Office Open XML file: {path}",
            detail="a pre-2007 binary .ppt is not a zip; convert it first",
        ) from exc
    names = set(archive.namelist())
    if CONTENT_TYPES_PART not in names or PRESENTATION_PART not in names:
        archive.close()
        raise ValidationError(
            f"not a presentation: {path}",
            detail=f"the archive has no {PRESENTATION_PART}; a .docx or .xlsx would look like this",
        )
    return archive


def _rels_part(part: str) -> str:
    directory, _, name = part.rpartition("/")
    return f"{directory}/_rels/{name}.rels" if directory else f"_rels/{name}.rels"


def _owner_of_rels(rels_part: str) -> str:
    directory, _, name = rels_part.rpartition("/")
    return posixpath.normpath(posixpath.join(directory, "..", name.removesuffix(".rels")))


def _read_rels(archive: zipfile.ZipFile, part: str) -> dict[str, str]:
    """Relationship id -> the target part, resolved to an archive path.

    External targets (``TargetMode="External"``) are dropped: they are URLs, and
    resolving one against a directory produces a path that exists nowhere.
    """
    rels = _rels_part(part)
    if rels not in archive.namelist():
        return {}
    root = ET.fromstring(archive.read(rels))
    base = posixpath.dirname(part)
    resolved: dict[str, str] = {}
    for relationship in root.findall(f"{{{NS_PACKAGE_REL}}}Relationship"):
        if relationship.get("TargetMode") == "External":
            continue
        rid = relationship.get("Id")
        target = relationship.get("Target")
        if rid and target:
            resolved[rid] = posixpath.normpath(posixpath.join(base, target))
    return resolved


def slide_order(archive: zipfile.ZipFile) -> list[str]:
    """The slide parts in the order the deck presents them.

    Read from ``<p:sldIdLst>`` rather than from the part names, which are
    assigned at creation and never renumbered — so in any deck that has been
    reordered, sorting ``slide*.xml`` gives the order the slides were *made* in.

    Falls back to numeric part order only when the list is missing, which means
    a hand-assembled archive rather than anything PowerPoint wrote.
    """
    rels = _read_rels(archive, PRESENTATION_PART)
    root = ET.fromstring(archive.read(PRESENTATION_PART))
    listed = root.find(f"{{{NS_PRESENTATION}}}sldIdLst")
    ordered: list[str] = []
    if listed is not None:
        for entry in listed.findall(f"{{{NS_PRESENTATION}}}sldId"):
            target = rels.get(entry.get(f"{{{NS_OFFICE_REL}}}id", ""))
            if target:
                ordered.append(target)
    if ordered:
        return ordered

    def number(part: str) -> int:
        digits = re.search(r"(\d+)", posixpath.basename(part))
        return int(digits.group(1)) if digits else 0

    return sorted(
        (name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
        key=number,
    )


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def paragraph_texts(xml: bytes) -> list[str]:
    """The visible text of a part, one string per paragraph.

    Runs are joined, because a run boundary is a formatting artefact and not a
    word boundary — reporting them separately turns one sentence into four
    fragments and makes the output unsearchable. Empty paragraphs are dropped.
    """
    root = ET.fromstring(xml)
    lines: list[str] = []
    for paragraph in root.iter(f"{{{NS_DRAWING}}}p"):
        joined = "".join(run.text or "" for run in paragraph.iter(f"{{{NS_DRAWING}}}t"))
        if joined.strip():
            lines.append(joined)
    return lines


def _media_of(archive: zipfile.ZipFile, part: str) -> list[str]:
    return sorted({t for t in _read_rels(archive, part).values() if t.startswith(MEDIA_DIR)})


def _notes_of(archive: zipfile.ZipFile, part: str) -> list[str]:
    for target in _read_rels(archive, part).values():
        if target.startswith("ppt/notesSlides/"):
            return paragraph_texts(archive.read(target))
    return []


def _reference_map(archive: zipfile.ZipFile) -> dict[str, list[str]]:
    """media part -> every part that references it, across the whole archive."""
    references: dict[str, list[str]] = {
        name: [] for name in archive.namelist() if name.startswith(MEDIA_DIR)
    }
    for name in archive.namelist():
        if not name.endswith(".rels"):
            continue
        owner = _owner_of_rels(name)
        for target in _read_rels(archive, owner).values():
            if target in references and owner not in references[target]:
                references[target].append(owner)
    return references


def inspect(path: Path) -> Deck:
    """Measure a deck: slides in display order, their text, notes and media.

    Raises:
        NotFoundError: the file does not exist.
        ValidationError: it is not an Office Open XML presentation.
    """
    with _open(path) as archive:
        references = _reference_map(archive)
        slides = tuple(
            Slide(
                index=index,
                part=part,
                text=tuple(paragraph_texts(archive.read(part))),
                notes=tuple(_notes_of(archive, part)),
                media=tuple(_media_of(archive, part)),
            )
            for index, part in enumerate(slide_order(archive), start=1)
        )
        return Deck(
            path=path,
            slides=slides,
            media=tuple(sorted(references)),
            references={key: tuple(value) for key, value in references.items()},
        )


# --------------------------------------------------------------------------- #
# Extracting
# --------------------------------------------------------------------------- #


def extract_media(
    path: Path,
    out_dir: Path,
    *,
    prefix: str | None = None,
    slides: list[int] | None = None,
    min_bytes: int = 0,
) -> list[dict[str, object]]:
    """Write the deck's media into ``out_dir`` and return the manifest rows.

    Bytes are copied out of the archive untouched — no decode, no re-encode — so
    a photograph keeps the resolution and the compression it was embedded with.

    ``slides`` restricts the extraction to the media those slides reference, by
    display position. Layout and master artwork is not reachable that way, which
    is the point: a run filtered to slide 1 should not silently include the logo
    that sits on all forty.

    Each row records the slides that use the file and the part it was named in
    the archive, because the output is renumbered and that name is the only way
    back. Pixel dimensions come from ImageMagick when it is installed and are
    reported as 0 when it is not, rather than the file being dropped.

    Raises:
        ValidationError: nothing survived the filters.
    """
    deck = inspect(path)
    if slides:
        positions = set(slides)
        unknown = positions - {slide.index for slide in deck.slides}
        if unknown:
            raise UsageError(
                f"no such slide: {', '.join(str(number) for number in sorted(unknown))}",
                detail=f"the deck has {len(deck.slides)} slide(s), numbered from 1",
            )
        wanted = [
            name
            for name in deck.media
            if any(name in slide.media for slide in deck.slides if slide.index in positions)
        ]
    else:
        wanted = [name for name in deck.media if deck.references.get(name)]

    users: dict[str, list[int]] = {
        name: [slide.index for slide in deck.slides if name in slide.media] for name in wanted
    }
    prefix = prefix or path.stem.replace(" ", "_")
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, object]] = []
    with _open(path) as archive:
        index = 0
        for name in wanted:
            payload = archive.read(name)
            if len(payload) < min_bytes:
                continue
            index += 1
            target = out_dir / f"{prefix}_{index}{Path(name).suffix.lower()}"
            target.write_bytes(payload)
            meta = (
                identify(target, timeout=IDENTIFY_TIMEOUT)
                if target.suffix.lower() in MEASURABLE_SUFFIXES
                else None
            )
            rows.append(
                {
                    "file": target.name,
                    "slides": " ".join(str(number) for number in users.get(name, [])),
                    "width": meta.width if meta else 0,
                    "height": meta.height if meta else 0,
                    "colorspace": meta.colorspace if meta else "",
                    "bytes": len(payload),
                    "extracted_from": name,
                }
            )

    if not rows:
        raise ValidationError(
            "no media extracted",
            detail="check --slide and --min-bytes; a deck can legitimately have no images",
        )
    manifest = out_dir / "manifest.csv"
    with manifest.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return rows


# --------------------------------------------------------------------------- #
# Editing
# --------------------------------------------------------------------------- #


def _guard_output(source: Path, out: Path, *, overwrite: bool) -> None:
    """Refuse the two ways an edit destroys its own input.

    In-place is refused outright rather than offered behind a flag: the source
    is the only copy of what the deck said before, and a find/replace that
    matched the wrong thing is not visible until somebody opens the file.
    """
    if out.resolve() == source.resolve():
        raise UsageError(
            "the output is the source",
            detail="write to a new file; this tool never edits a deck in place",
        )
    if out.exists() and not overwrite:
        raise UsageError(f"already exists: {out}", detail="pass --overwrite to replace it")


def _apply_to_paragraph(
    paragraph: str, pairs: list[tuple[str, str]], *, keep_runs: bool
) -> tuple[str, str, str, int] | None:
    """Rewrite one paragraph, or ``None`` if no pair matched.

    Two strategies, cheapest first, because they cost different things:

    1. **Inside each run.** Formatting survives untouched. Cannot see a match
       that straddles a run boundary.
    2. **Across the joined paragraph.** Sees everything, and writing the result
       back means the paragraph takes its first run's formatting.

    The second is only used when the first did not produce the wanted text — a
    distinction worth the code, because a paragraph having several runs does not
    mean the *match* crossed them. On a real 27-slide deck every one of five
    edits sat inside a single run of a multi-run paragraph, so collapsing on the
    run count alone would have flattened five paragraphs for nothing.

    Returns the new XML, the before/after text, and how many runs were
    collapsed — zero when strategy 1 was enough.
    """
    runs = list(_RUN_TEXT.finditer(paragraph))
    if not runs:
        return None
    before = "".join(unescape(run.group(2), _ENTITIES) for run in runs)
    wanted = before
    for old, new in pairs:
        wanted = wanted.replace(old, new)
    if wanted == before:
        return None

    # Strategy 1. Reversed, so each splice leaves the earlier offsets valid.
    rebuilt = paragraph
    for run in reversed(runs):
        text = unescape(run.group(2), _ENTITIES)
        updated = text
        for old, new in pairs:
            updated = updated.replace(old, new)
        if updated != text:
            rebuilt = rebuilt[: run.start(2)] + escape(updated, _ESCAPES) + rebuilt[run.end(2) :]
    per_run = "".join(unescape(m.group(2), _ENTITIES) for m in _RUN_TEXT.finditer(rebuilt))

    if per_run == wanted:
        return rebuilt, before, per_run, 0
    if keep_runs:
        return (rebuilt, before, per_run, 0) if per_run != before else None

    # Strategy 2: the match crosses a boundary. Everything goes into run one.
    pieces: list[str] = [paragraph[: runs[0].start(2)], escape(wanted, _ESCAPES)]
    for previous, run in zip(runs, runs[1:], strict=False):
        pieces.append(paragraph[previous.end(2) : run.start(2)])
    pieces.append(paragraph[runs[-1].end(2) :])
    return "".join(pieces), before, wanted, len(runs)


def replace_text(
    path: Path,
    out: Path,
    pairs: list[tuple[str, str]],
    *,
    keep_runs: bool = False,
    overwrite: bool = False,
) -> list[Replacement]:
    """Find/replace across every slide, writing the result to ``out``.

    Matching sees the whole **paragraph**, because PowerPoint splits one at every
    formatting change and at boundaries nothing on screen reveals — so
    ``"Total: 42"`` is routinely three runs and a per-run search misses text that
    is plainly there.

    The edit is still made inside the individual runs whenever that produces the
    same result, so formatting survives; only a match that genuinely crosses a
    boundary forces the paragraph onto its first run's formatting, and the
    :class:`Replacement` says so with a non-zero ``collapsed_runs``.
    ``keep_runs=True`` refuses that last case rather than paying for it.

    Notes, layouts and the master are not touched — only ``ppt/slides/slideN.xml``.

    Every other part of the archive is copied through byte for byte.

    Raises:
        UsageError: the output would overwrite the source, or already exists.
        ValidationError: no pair matched anything.
    """
    if not pairs:
        raise UsageError("nothing to replace", detail="pass at least one old=new pair")
    _guard_output(path, out, overwrite=overwrite)

    applied: list[Replacement] = []
    out.parent.mkdir(parents=True, exist_ok=True)
    with _open(path) as archive:
        positions = {part: index for index, part in enumerate(slide_order(archive), start=1)}
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as written:
            for info in archive.infolist():
                payload = archive.read(info.filename)
                if info.filename in positions:
                    xml = payload.decode("utf-8")
                    rebuilt: list[str] = []
                    cursor = 0
                    for match in _PARAGRAPH.finditer(xml):
                        outcome = _apply_to_paragraph(match.group(0), pairs, keep_runs=keep_runs)
                        if outcome is None:
                            continue
                        new_xml, before, after, runs = outcome
                        rebuilt.append(xml[cursor : match.start()])
                        rebuilt.append(new_xml)
                        cursor = match.end()
                        applied.append(
                            Replacement(
                                slide=positions[info.filename],
                                part=info.filename,
                                before=before,
                                after=after,
                                collapsed_runs=runs,
                            )
                        )
                    if rebuilt:
                        rebuilt.append(xml[cursor:])
                        payload = "".join(rebuilt).encode("utf-8")
                written.writestr(info, payload)

    if not applied:
        out.unlink(missing_ok=True)
        raise ValidationError(
            "no text matched",
            detail="run `inspect` to see the deck's text; matching is exact and case-sensitive",
        )
    return applied


def replace_media(
    path: Path,
    out: Path,
    *,
    media: str,
    image: Path,
    overwrite: bool = False,
) -> dict[str, object]:
    """Swap one media part's bytes, copying the rest of the archive verbatim.

    ``media`` is the archive name — ``image3.png``, or the full
    ``ppt/media/image3.png``. Take it from :func:`inspect` or from the
    ``extracted_from`` column of an extraction manifest; there is no way to
    address an image by what it looks like.

    The extension must match the part being replaced. It is not a formality:
    ``[Content_Types].xml`` declares the type per extension, so a PNG written
    over ``image3.jpeg`` produces a file PowerPoint reports as corrupt, having
    given no indication at write time.

    The shape's position and size live in the slide and do not move, so an image
    with a different aspect ratio arrives stretched into the old frame.

    Raises:
        NotFoundError: no such media part, or no such replacement file.
        ValidationError: the extensions differ.
        UsageError: the output would overwrite the source, or already exists.
    """
    if not image.is_file():
        raise NotFoundError(f"not a file: {image}")
    _guard_output(path, out, overwrite=overwrite)

    with _open(path) as archive:
        names = archive.namelist()
        target = media if media.startswith(MEDIA_DIR) else f"{MEDIA_DIR}{media}"
        if target not in names:
            available = sorted(name for name in names if name.startswith(MEDIA_DIR))
            raise NotFoundError(
                f"no such media part: {target}",
                detail=f"the deck has {', '.join(available) or 'none'}",
            )
        if Path(target).suffix.lower() != image.suffix.lower():
            # A file with no extension renders as an empty string here, which
            # reads as a tool that lost the value rather than a file that has none.
            given = image.suffix or "no extension"
            raise ValidationError(
                f"extension mismatch: {given} cannot replace {Path(target).suffix}",
                detail="[Content_Types].xml declares the type per extension; convert first",
            )

        payload = image.read_bytes()
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as written:
            for info in archive.infolist():
                written.writestr(
                    info, payload if info.filename == target else archive.read(info.filename)
                )

    return {
        "out": str(out),
        "replaced": target,
        "source": str(image),
        "bytes": len(payload),
    }


def _quantize(path: Path, colors: int) -> bool:
    """Reduce a raster to a palette, in place. Returns whether it happened.

    Screenshots, diagrams and gradients — what a review deck is made of — carry
    far fewer distinct colours than their 24-bit encoding reserves, so this is
    usually a large saving at no visible cost: on a real 27-slide deck it took
    the exported images from 20 MB to 7.4 MB **at the same resolution**, which
    is the trade worth making when the alternative is shrinking them until the
    annotations stop being readable.

    Photographs are the case where it shows. Judge the output before committing
    to a low count rather than trusting the ratio.
    """
    binary = shutil.which("magick") or shutil.which("convert")
    if binary is None:
        return False
    completed = subprocess.run(
        [binary, str(path), "-colors", str(colors), str(path)],
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0


def _downscale(path: Path, max_width: int) -> tuple[int, int] | None:
    """Shrink a raster in place to ``max_width``, returning its new size.

    ImageMagick's ``>`` qualifier only ever shrinks, so an image already narrower
    than the bound is left byte-identical rather than re-encoded — re-encoding a
    small PNG to "resize" it loses quality for nothing.

    Returns ``None`` when ImageMagick is absent or refuses the file. The caller
    keeps the original in that case: an asset at the wrong size is recoverable,
    an asset that was silently dropped is not.
    """
    binary = shutil.which("magick") or shutil.which("convert")
    if binary is None:
        return None
    completed = subprocess.run(
        [binary, str(path), "-resize", f"{max_width}x{max_width}>", str(path)],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    meta = identify(path, timeout=IDENTIFY_TIMEOUT)
    return (meta.width, meta.height) if meta else None


def _markdown(deck: Deck, images: dict[str, str], title: str) -> str:
    """One section per slide, in display order, with the images it references.

    The text is what the slide says; the images are how it says it. Splitting
    them into separate files would make the result unreadable, which is the
    whole point of exporting a deck to markdown rather than to a directory of
    PNGs somebody has to open one at a time.
    """
    lines = [f"# {title}", ""]
    lines.append(
        f"> Exported from `{deck.path.name}` — {len(deck.slides)} slide(s), "
        f"{len(images)} image(s). Slides are numbered by **display order**."
    )
    lines.append("")
    for slide in deck.slides:
        lines.append(f"## Slide {slide.index}")
        lines.append("")
        if slide.text:
            lines.extend(slide.text)
            lines.append("")
        else:
            lines.append("*(no text)*")
            lines.append("")
        shown = [images[name] for name in slide.media if name in images]
        for position, name in enumerate(shown, start=1):
            lines.append(f"![slide {slide.index}, image {position}]({name})")
            lines.append("")
        if slide.notes:
            lines.append("**Speaker notes**")
            lines.append("")
            lines.extend(f"> {line}" for line in slide.notes)
            lines.append("")

    # Artwork on the master or a layout belongs to no slide, so the loop above
    # can never show it — and a file present in `images/` that the document
    # never mentions reads as clutter rather than as the brand's logo, which is
    # usually exactly what it is.
    on_slides = {images[name] for slide in deck.slides for name in slide.media if name in images}
    loose = [ref for ref in images.values() if ref not in on_slides]
    if loose:
        lines.append("## Images not placed on any slide")
        lines.append("")
        lines.append(
            "These come from the master or a layout — the recurring furniture of the deck, "
            "which is where a logo usually lives."
        )
        lines.append("")
        for ref in loose:
            lines.append(f"![{Path(ref).name}]({ref})")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def export_folder(
    path: Path,
    out_dir: Path,
    *,
    prefix: str | None = None,
    title: str | None = None,
    images_dir: Path | None = None,
    max_width: int = 0,
    colors: int = 0,
    include_media: bool = False,
    overwrite: bool = False,
) -> dict[str, object]:
    """Turn a deck into a readable folder: one markdown file, plus its images.

    A `.pptx` is a delivery format and a terrible archive format — it is opaque
    to grep, to review and to any diff. This writes the same content as
    ``index.md`` with the images beside it, so a deck handed over as a
    requirement can live in a repository and be read like anything else.

    ``max_width`` bounds the images. It matters more than it looks: a deck's
    embedded artwork is at native resolution, which for a 27-slide review deck
    measured 213 MB — usable in the deck and impossible in a repository. Only
    images wider than the bound are touched.

    ``colors`` quantizes them, which is the cheaper half of the same problem:
    the deck above went from 20 MB to 7.4 MB at 256 colours **without losing a
    pixel of resolution**, and resolution is what makes an annotation readable.
    Reach for it before reaching for a smaller ``max_width``.

    ``images_dir`` puts the images somewhere else, with ``index.md`` linking to
    them relatively. The markdown is the part worth keeping under version
    control and the images are usually the part that must not be — they are
    recoverable from wherever the deck came from, and they are the entire weight.
    Splitting them by hand means rewriting every link, which is why this is an
    option rather than advice.

    ``include_media`` adds video and other non-image parts. Off by default for
    the same reason: one embedded clip was 94 MB on its own.

    Raises:
        UsageError: the destination exists and ``overwrite`` was not given.
    """
    deck = inspect(path)
    if out_dir.exists() and any(out_dir.iterdir()) and not overwrite:
        raise UsageError(
            f"not empty: {out_dir}", detail="pass --overwrite to replace its contents"
        )

    prefix = prefix or path.stem.replace(" ", "_")
    target_dir = images_dir if images_dir is not None else out_dir / "images"
    target_dir.mkdir(parents=True, exist_ok=True)
    # Relative to `out_dir`, because that is where `index.md` is read from.
    # `os.path.relpath` rather than `Path.relative_to`, which refuses to walk up
    # and so cannot express the case this option exists for.
    link_base = Path(os.path.relpath(target_dir.resolve(), out_dir.resolve())).as_posix()

    wanted = [name for name in deck.media if deck.references.get(name)]
    if not include_media:
        wanted = [name for name in wanted if Path(name).suffix.lower() in MEASURABLE_SUFFIXES]

    rows: list[dict[str, object]] = []
    written: dict[str, str] = {}
    total_bytes = 0
    with _open(path) as archive:
        for index, name in enumerate(wanted, start=1):
            target = target_dir / f"{prefix}_{index}{Path(name).suffix.lower()}"
            target.write_bytes(archive.read(name))
            measurable = target.suffix.lower() in MEASURABLE_SUFFIXES
            before = identify(target, timeout=IDENTIFY_TIMEOUT) if measurable else None
            after = _downscale(target, max_width) if (measurable and max_width) else None
            if measurable and colors:
                _quantize(target, colors)
            written[name] = f"{link_base}/{target.name}"
            total_bytes += target.stat().st_size
            rows.append(
                {
                    "file": written[name],
                    "slides": " ".join(
                        str(s.index) for s in deck.slides if name in s.media
                    ),
                    "width": (after[0] if after else (before.width if before else 0)),
                    "height": (after[1] if after else (before.height if before else 0)),
                    "original_width": before.width if before else 0,
                    "original_height": before.height if before else 0,
                    "bytes": target.stat().st_size,
                    "extracted_from": name,
                }
            )

    out_dir.mkdir(parents=True, exist_ok=True)
    index_md = out_dir / "index.md"
    index_md.write_text(
        _markdown(deck, written, title or path.stem), encoding="utf-8"
    )

    manifest = out_dir / "manifest.csv"
    if rows:
        with manifest.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    return {
        "out_dir": str(out_dir),
        "images_dir": str(target_dir),
        "index": str(index_md),
        "slides": len(deck.slides),
        "images": len(rows),
        "bytes": total_bytes,
        "skipped_media": [
            name
            for name in deck.media
            if deck.references.get(name) and name not in written
        ],
    }


def unpack(path: Path, out_dir: Path) -> list[str]:
    """Explode the archive into ``out_dir`` — the escape hatch.

    Everything above is a named operation on a deck. This is for the case none
    of them covers, where reading the XML by hand is the honest answer; nothing
    in this module hides the format, so working at that level is a step down
    rather than a different tool.
    """
    with _open(path) as archive:
        if out_dir.exists():
            shutil.rmtree(out_dir)
        archive.extractall(out_dir)
        return sorted(archive.namelist())
