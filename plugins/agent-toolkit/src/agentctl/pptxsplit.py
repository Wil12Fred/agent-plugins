"""Cut a deck down to the slides one task needs, and drop everything else.

A review deck arrives as one file covering twenty tasks. Handing that to whoever
does task three means handing them nineteen tasks of noise and, here, 213 MB of
it. This produces a real `.pptx` containing only the slides that task names.

The cut has to be transitive or it produces a file PowerPoint refuses to open. A
slide is not self-contained: it points at a layout, which points at a master,
which points at a theme; it points at its own media, its notes, its embedded
objects. So the kept set is computed by **following relationships from the parts
that survive**, and everything unreachable is dropped — which is also where the
size goes, because a deck's weight is media and a three-slide subset references
almost none of it.

Three parts have to be rewritten rather than copied, and skipping any one leaves
a file that opens as corrupt or shows slides that are no longer there:

* ``ppt/presentation.xml`` — the ``<p:sldIdLst>`` still lists every slide.
* ``ppt/_rels/presentation.xml.rels`` — still points at the dropped ones.
* ``[Content_Types].xml`` — still declares an override per dropped part.

Slide numbers are **display positions**, the same ones :func:`agentctl.pptxdeck.inspect`
reports, because that is what a person reads off the deck. The part names keep
their original numbering inside the new file; nothing renumbers, so an image that
was ``image12.png`` still is.
"""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from agentctl.errors import UsageError

NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
NS_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
P, R = f"{{{NS_P}}}", f"{{{NS_R}}}"

ALWAYS_KEEP = ("[Content_Types].xml", "_rels/.rels")


def parse_slides(spec: str, total: int) -> list[int]:
    """`"1,3,5-7"` to `[1, 3, 5, 6, 7]`, validated against the deck.

    Out-of-range numbers are refused rather than clipped: a range that silently
    shrinks produces a deck missing exactly the slide somebody meant to include,
    and nothing says so.
    """
    wanted: list[int] = []
    for chunk in (part.strip() for part in spec.split(",") if part.strip()):
        match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", chunk)
        if match:
            lo, hi = int(match.group(1)), int(match.group(2))
            if lo > hi:
                raise UsageError(f"backwards range: {chunk}", detail="write it low-high, e.g. 5-7")
            wanted.extend(range(lo, hi + 1))
        elif chunk.isdigit():
            wanted.append(int(chunk))
        else:
            raise UsageError(f"not a slide number or range: {chunk!r}", detail='e.g. "1,3,5-7"')

    out = sorted(set(wanted))
    if not out:
        raise UsageError("no slides selected", detail='e.g. --slides "1,3,5-7"')
    bad = [n for n in out if n < 1 or n > total]
    if bad:
        raise UsageError(
            f"no such slide: {', '.join(str(n) for n in bad)}",
            detail=f"the deck has {total}, numbered from 1",
        )
    return out


def _rels_part(part: str) -> str:
    directory, _, name = part.rpartition("/")
    return f"{directory}/_rels/{name}.rels" if directory else f"_rels/{name}.rels"


def _targets(archive: zipfile.ZipFile, part: str) -> list[str]:
    """The internal parts ``part`` points at. External targets are URLs, not parts."""
    rels = _rels_part(part)
    if rels not in archive.namelist():
        return []
    root = ET.fromstring(archive.read(rels))
    base = posixpath.dirname(part)
    out = []
    for rel in root.findall(f"{{{NS_PKG}}}Relationship"):
        if rel.get("TargetMode") == "External":
            continue
        target = rel.get("Target")
        if target:
            out.append(posixpath.normpath(posixpath.join(base, target)))
    return out


def _reachable(archive: zipfile.ZipFile, roots: list[str], drop: set[str]) -> set[str]:
    """Every part reachable from ``roots``, never entering ``drop``.

    Breadth-first with a seen set, because layouts and masters point back at each
    other and a naive walk does not terminate.
    """
    names = set(archive.namelist())
    keep: set[str] = set()
    queue = [r for r in roots if r in names and r not in drop]
    while queue:
        part = queue.pop()
        if part in keep or part not in names or part in drop:
            continue
        keep.add(part)
        rels = _rels_part(part)
        if rels in names:
            keep.add(rels)
        queue.extend(t for t in _targets(archive, part) if t not in keep and t not in drop)
    return keep


#: A one-byte file, written where an oversized media part used to be.
#: Deleting the part instead would leave the slide's relationship dangling and
#: PowerPoint calls that corrupt; keeping the name and emptying the bytes lets
#: the file open with the media simply absent.
_STUB = b"\0"


def split(
    deck: Path,
    out: Path,
    slides: list[int],
    *,
    overwrite: bool = False,
    stub_media_over: int | None = None,
) -> dict[str, object]:
    """Write a deck containing only ``slides``, in display order.

    Args:
        stub_media_over: replace any ``ppt/media`` part larger than this many
            bytes with a stub. A review packet inherits whatever the slide
            embeds, and one slide of a client deck can carry an 89 MB video —
            which pushed three packets past **Google Drive's 100 MB limit for
            converting a .pptx to Slides**, so the reviewer could not open the
            very file made for them. The video is usually delivered separately
            anyway; what the reviewer needs is the slide's instructions.

    Raises:
        UsageError: the output exists, or would overwrite the source.
        ValidationError: the source is not a presentation.
    """
    from agentctl.pptxdeck import _open, slide_order

    if out.resolve() == deck.resolve():
        raise UsageError("the output is the source", detail="write to a new file")
    if out.exists() and not overwrite:
        raise UsageError(f"already exists: {out}", detail="pass --overwrite to replace it")

    with _open(deck) as archive:
        order = slide_order(archive)
        wanted = parse_slides(",".join(str(n) for n in slides), len(order))
        keep_slides = [order[n - 1] for n in wanted]
        drop_slides = {p for p in order if p not in keep_slides}
        drop = set(drop_slides) | {_rels_part(p) for p in drop_slides}

        presentation = "ppt/presentation.xml"
        roots = [presentation, *ALWAYS_KEEP]
        roots += [n for n in archive.namelist() if n.startswith("docProps/")]
        keep = _reachable(archive, roots, drop)
        keep.update(ALWAYS_KEEP)

        rels_xml = _prune_presentation_rels(archive, drop_slides)
        pres_xml = _prune_presentation(archive, rels_xml.dropped_ids)
        types_xml = _prune_content_types(archive, keep)

        out.parent.mkdir(parents=True, exist_ok=True)
        original_bytes = deck.stat().st_size
        stubbed: list[dict[str, object]] = []
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as written:
            for info in archive.infolist():
                if info.filename not in keep:
                    continue
                if (
                    stub_media_over is not None
                    and info.filename.startswith("ppt/media/")
                    and info.file_size > stub_media_over
                ):
                    stubbed.append({"part": info.filename, "bytes": info.file_size})
                    written.writestr(info.filename, _STUB)
                    continue
                if info.filename == presentation:
                    written.writestr(info, pres_xml)
                elif info.filename == _rels_part(presentation):
                    written.writestr(info, rels_xml.xml)
                elif info.filename == "[Content_Types].xml":
                    written.writestr(info, types_xml)
                else:
                    written.writestr(info, archive.read(info.filename))

    return {
        "out": str(out),
        "slides": wanted,
        "parts": len(keep),
        "bytes": out.stat().st_size,
        "source_bytes": original_bytes,
        "stubbed": stubbed,
    }


class _PrunedRels:
    __slots__ = ("xml", "dropped_ids")

    def __init__(self, xml: bytes, dropped_ids: set[str]) -> None:
        self.xml = xml
        self.dropped_ids = dropped_ids


def _prune_presentation_rels(archive: zipfile.ZipFile, drop_slides: set[str]) -> _PrunedRels:
    part = "ppt/_rels/presentation.xml.rels"
    ET.register_namespace("", NS_PKG)
    root = ET.fromstring(archive.read(part))
    dropped: set[str] = set()
    for rel in list(root):
        target = rel.get("Target") or ""
        resolved = posixpath.normpath(posixpath.join("ppt", target))
        if resolved in drop_slides:
            dropped.add(rel.get("Id") or "")
            root.remove(rel)
    return _PrunedRels(ET.tostring(root, encoding="UTF-8", xml_declaration=True), dropped)


def _prune_presentation(archive: zipfile.ZipFile, dropped_ids: set[str]) -> bytes:
    """Remove the `<p:sldId>` entries whose relationship is gone.

    Editing the raw XML rather than re-serialising the tree: `presentation.xml`
    carries namespace prefixes and attributes that a round trip through
    ElementTree rewrites, and PowerPoint is not forgiving about the result.
    """
    xml = archive.read("ppt/presentation.xml").decode("utf-8")
    for rid in dropped_ids:
        if not rid:
            continue
        xml = re.sub(rf'<p:sldId\b[^>]*r:id="{re.escape(rid)}"\s*/>', "", xml)
    return xml.encode("utf-8")


def _prune_content_types(archive: zipfile.ZipFile, keep: set[str]) -> bytes:
    ET.register_namespace("", NS_CT)
    root = ET.fromstring(archive.read("[Content_Types].xml"))
    for override in list(root):
        name = override.get("PartName")
        if name and name.lstrip("/") not in keep:
            root.remove(override)
    out: bytes = ET.tostring(root, encoding="UTF-8", xml_declaration=True)
    return out
