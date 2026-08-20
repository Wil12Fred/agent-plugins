"""Lay a deck out as HTML, from the geometry the file already carries.

`export` gives a deck's content as a document. This gives its *arrangement*: one
page per slide, every shape where the slide puts it, at the size the slide gives
it. It is an approximation and says so — but for a review deck, where the whole
message is *this* annotation pointing at *that* screenshot, an approximation of
the layout carries meaning that a list of paragraphs cannot.

**Why this exists rather than a call to LibreOffice.** A faithful render needs a
slide layout engine, and installing one is a 420 MB decision somebody has to
make. Everything below is arithmetic on numbers already in the XML: each shape
records its own offset and extent in EMU, so placing them is unit conversion, not
layout. The engine is only needed for what the file does *not* say — text reflow,
autofit, theme effects — and those are exactly what this does not promise.

What it draws:

* **Pictures** at their recorded position and size.
* **Text boxes**, with per-run size, bold, italic, colour and alignment, and the
  fill and outline of the box itself.
* **Connectors** — the arrows a review deck points with — as SVG lines, honouring
  the flips that decide which way they run.
* **Groups**, recursively, applying the child-space transform. Skipping these
  loses whole compositions, because a group is how several shapes become one.
* **Backgrounds**, resolved slide → layout → master, including theme colours.

What it does not, and reports instead of dropping silently: tables, charts,
SmartArt and embedded objects are drawn as a labelled outline, so the space they
occupy is visible and named rather than blank. Rotation is applied; 3-D, shadows,
gradient fills and picture effects are not.

Rendering is left to :mod:`agentctl.htmlpdf`, which prints the result with a
browser. That split is the point: the hard part here is reading the file, and the
hard part there is driving the engine, and neither should know about the other.
"""

from __future__ import annotations

import html
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from agentctl.errors import UsageError

NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P, A, R = f"{{{NS_P}}}", f"{{{NS_A}}}", f"{{{NS_R}}}"

EMU_PER_POINT = 12700
"""914400 EMU to the inch, 72 points to the inch."""

DEFAULT_TEXT_PT = 18.0
"""PowerPoint's body default, used when nothing in the inheritance chain says.

Guessing a size is unavoidable — the value genuinely is not in the file when a
run inherits it — and 18pt is what PowerPoint itself falls back to.
"""

UNSUPPORTED = {
    f"{P}graphicFrame": "table / chart / SmartArt",
    f"{P}contentPart": "embedded content",
}


def emu_to_pt(value: str | int | None, default: float = 0.0) -> float:
    try:
        return int(value) / EMU_PER_POINT  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


@dataclass
class Box:
    """One drawn thing, already in points, already in page space."""

    kind: str
    x: float
    y: float
    w: float
    h: float
    rotation: float = 0.0
    html: str = ""
    z: int = 0


@dataclass
class SlideLayout:
    index: int
    part: str
    width: float
    height: float
    background: str
    boxes: list[Box] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Colour
# --------------------------------------------------------------------------- #


def _theme_colours(archive: zipfile.ZipFile) -> dict[str, str]:
    """The theme's colour scheme, as name -> `#rrggbb`.

    `dk1`/`lt1` are usually `sysClr` with a `lastClr` — the resolved value the
    file already carries, which is why it is read rather than guessed at.
    """
    names = [name for name in archive.namelist() if re.fullmatch(r"ppt/theme/theme\d+\.xml", name)]
    if not names:
        return {}
    scheme: dict[str, str] = {}
    root = ET.fromstring(archive.read(sorted(names)[0]))
    for entry in root.iter(f"{A}clrScheme"):
        for child in entry:
            key = child.tag.split("}")[-1]
            srgb = child.find(f"{A}srgbClr")
            sysclr = child.find(f"{A}sysClr")
            if srgb is not None:
                scheme[key] = f"#{srgb.get('val')}"
            elif sysclr is not None and sysclr.get("lastClr"):
                scheme[key] = f"#{sysclr.get('lastClr')}"
    # The colour map calls the same slots bg1/tx1/bg2/tx2 in shape markup.
    for alias, real in (("bg1", "lt1"), ("tx1", "dk1"), ("bg2", "lt2"), ("tx2", "dk2")):
        if real in scheme:
            scheme.setdefault(alias, scheme[real])
    return scheme


def _colour(node: ET.Element | None, theme: dict[str, str]) -> str | None:
    """Resolve a fill/colour container to CSS, or ``None`` if it says nothing."""
    if node is None:
        return None
    srgb = node.find(f".//{A}srgbClr")
    if srgb is not None:
        alpha = srgb.find(f"{A}alpha")
        if alpha is not None:
            try:
                opacity = int(alpha.get("val", "100000")) / 100000
                return f"#{srgb.get('val')}{int(opacity * 255):02x}"
            except (TypeError, ValueError):
                pass
        return f"#{srgb.get('val')}"
    sch = node.find(f".//{A}schemeClr")
    if sch is not None:
        return theme.get(sch.get("val", ""))
    return None


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #


def _default_size(shape: ET.Element) -> float:
    """Font size from the shape's own list style, when the runs do not carry one.

    Only the shape is consulted, not the layout and master chain. Walking that
    chain properly means resolving placeholder inheritance, and getting it
    half-right produces sizes that are confidently wrong; the flat default is at
    least uniformly approximate, which a reader can see and correct for.
    """
    for level in shape.iter(f"{A}lvl1pPr"):
        defr = level.find(f"{A}defRPr")
        if defr is not None and defr.get("sz"):
            return int(defr.get("sz", "1800")) / 100
    return DEFAULT_TEXT_PT


def _text_html(shape: ET.Element, theme: dict[str, str], scale: float) -> str:
    """The shape's paragraphs as HTML, preserving size, weight, colour, alignment."""
    body = shape.find(f"{P}txBody")
    if body is None:
        return ""
    fallback = _default_size(shape)

    # normAutofit records the shrink PowerPoint applied to make the text fit.
    # Ignoring it overflows every box that was ever auto-shrunk.
    autofit = body.find(f"{A}bodyPr/{A}normAutofit")
    if autofit is not None and autofit.get("fontScale"):
        fallback *= int(autofit.get("fontScale", "100000")) / 100000

    out: list[str] = []
    for para in body.findall(f"{A}p"):
        props = para.find(f"{A}pPr")
        align = {"ctr": "center", "r": "right", "just": "justify"}.get(
            (props.get("algn") if props is not None else None) or "", "left"
        )
        runs: list[str] = []
        for run in para.findall(f"{A}r"):
            rpr = run.find(f"{A}rPr")
            node = run.find(f"{A}t")
            text = html.escape((node.text or "") if node is not None else "")
            size = fallback
            style = []
            if rpr is not None:
                if rpr.get("sz"):
                    size = int(rpr.get("sz", "1800")) / 100
                if rpr.get("b") == "1":
                    style.append("font-weight:700")
                if rpr.get("i") == "1":
                    style.append("font-style:italic")
                if rpr.get("u") not in (None, "none"):
                    style.append("text-decoration:underline")
                colour = _colour(rpr.find(f"{A}solidFill"), theme)
                if colour:
                    style.append(f"color:{colour}")
            style.append(f"font-size:{size * scale:.2f}pt")
            runs.append(f'<span style="{";".join(style)}">{text}</span>')
        if para.find(f"{A}br") is not None and not runs:
            out.append("<p>&nbsp;</p>")
        elif runs:
            out.append(f'<p style="text-align:{align}">{"".join(runs)}</p>')
    return "".join(out)


# --------------------------------------------------------------------------- #
# Shapes
# --------------------------------------------------------------------------- #


def _xfrm(element: ET.Element) -> tuple[float, float, float, float, float] | None:
    """`(x, y, w, h, rotation_degrees)` in points, or None when unplaced."""
    # `graphicFrame` puts its transform in the presentation namespace, not the
    # drawing one — a table or a chart otherwise reads as unplaced and vanishes.
    node = element.find(f".//{A}xfrm")
    if node is None:
        node = element.find(f".//{P}xfrm")
    if node is None:
        return None
    off, ext = node.find(f"{A}off"), node.find(f"{A}ext")
    if off is None or ext is None:
        return None
    rot = int(node.get("rot", "0")) / 60000  # 60000ths of a degree
    return (
        emu_to_pt(off.get("x")),
        emu_to_pt(off.get("y")),
        emu_to_pt(ext.get("cx")),
        emu_to_pt(ext.get("cy")),
        rot,
    )


def _connector(element: ET.Element, w: float, h: float, theme: dict[str, str]) -> str:
    """A connector as an SVG line, with the flips that decide its direction.

    `flipH`/`flipV` are not decoration: without them every arrow in the deck
    points the same way, and in a review deck the direction is the content.
    """
    node = element.find(f".//{A}xfrm")
    flip_h = (node.get("flipH") == "1") if node is not None else False
    flip_v = (node.get("flipV") == "1") if node is not None else False
    x1, x2 = (w, 0) if flip_h else (0, w)
    y1, y2 = (h, 0) if flip_v else (0, h)

    line = element.find(f".//{A}ln")
    stroke = _colour(line.find(f"{A}solidFill") if line is not None else None, theme) or "#444444"
    width = emu_to_pt(line.get("w") if line is not None else None, 1.0) or 1.0
    arrow = line is not None and line.find(f"{A}tailEnd") is not None
    head = (
        '<defs><marker id="a" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">'
        f'<path d="M0,0 L6,3 L0,6 z" fill="{stroke}"/></marker></defs>'
        if arrow
        else ""
    )
    marker = ' marker-end="url(#a)"' if arrow else ""
    return (
        f'<svg width="100%" height="100%" viewBox="0 0 {max(w, 1):.2f} {max(h, 1):.2f}" '
        f'preserveAspectRatio="none" style="overflow:visible">{head}'
        f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
        f'stroke="{stroke}" stroke-width="{width:.2f}"{marker}/></svg>'
    )


def _walk(
    parent: ET.Element,
    rels: dict[str, str],
    media: dict[str, str],
    theme: dict[str, str],
    *,
    dx: float = 0.0,
    dy: float = 0.0,
    sx: float = 1.0,
    sy: float = 1.0,
    depth: int = 0,
) -> tuple[list[Box], list[str]]:
    """Collect the drawable boxes under one tree, in z-order.

    `dx/dy/sx/sy` carry a group's transform. A group declares both where it sits
    on the slide (`off`/`ext`) and the coordinate space its children were authored
    in (`chOff`/`chExt`); ignoring the second places every grouped shape at the
    wrong scale, which looks like a rendering bug rather than a missing transform.
    """
    boxes: list[Box] = []
    skipped: list[str] = []

    for z, element in enumerate(parent):
        tag = element.tag
        if tag in UNSUPPORTED:
            geom = _xfrm(element)
            label = UNSUPPORTED[tag]
            if geom:
                x, y, w, h, rot = geom
                boxes.append(
                    Box(
                        "unsupported",
                        x * sx + dx,
                        y * sy + dy,
                        w * sx,
                        h * sy,
                        rot,
                        f'<div class="unsupported">{html.escape(label)}</div>',
                        z,
                    )
                )
            skipped.append(label)
            continue

        if tag == f"{P}grpSp":
            geom = _xfrm(element)
            child = element.find(f"{P}grpSpPr/{A}xfrm")
            if geom is None or child is None:
                continue
            x, y, w, h, _ = geom
            ch_off, ch_ext = child.find(f"{A}chOff"), child.find(f"{A}chExt")
            cw = emu_to_pt(ch_ext.get("cx") if ch_ext is not None else None, w) or w
            chh = emu_to_pt(ch_ext.get("cy") if ch_ext is not None else None, h) or h
            gsx, gsy = (w / cw if cw else 1.0), (h / chh if chh else 1.0)
            cox = emu_to_pt(ch_off.get("x") if ch_off is not None else None)
            coy = emu_to_pt(ch_off.get("y") if ch_off is not None else None)
            inner, inner_skipped = _walk(
                element,
                rels,
                media,
                theme,
                dx=x * sx + dx - cox * gsx * sx,
                dy=y * sy + dy - coy * gsy * sy,
                sx=gsx * sx,
                sy=gsy * sy,
                depth=depth + 1,
            )
            boxes.extend(inner)
            skipped.extend(inner_skipped)
            continue

        if tag not in (f"{P}sp", f"{P}pic", f"{P}cxnSp"):
            continue
        geom = _xfrm(element)
        if geom is None:
            continue
        x, y, w, h, rot = geom
        px, py, pw, ph = x * sx + dx, y * sy + dy, w * sx, h * sy

        if tag == f"{P}pic":
            blip = element.find(f".//{A}blip")
            rid = blip.get(f"{R}embed") if blip is not None else None
            src = media.get(rels.get(rid or "", ""), "")
            if not src:
                continue
            img = f'<img src="{html.escape(src)}" alt="">'
            boxes.append(Box("pic", px, py, pw, ph, rot, img, z))
            continue

        if tag == f"{P}cxnSp":
            boxes.append(Box("cxn", px, py, pw, ph, rot, _connector(element, pw, ph, theme), z))
            continue

        sppr = element.find(f"{P}spPr")
        style = []
        fill = _colour(sppr.find(f"{A}solidFill") if sppr is not None else None, theme)
        if fill:
            style.append(f"background:{fill}")
        line = sppr.find(f"{A}ln") if sppr is not None else None
        stroke = _colour(line.find(f"{A}solidFill") if line is not None else None, theme)
        line_pt = emu_to_pt(line.get("w") if line is not None else None, 1.0)
        if stroke:
            style.append(
                f"border:{max(line_pt, 0.75):.2f}pt solid {stroke}"
            )
        prst = sppr.find(f"{A}prstGeom") if sppr is not None else None
        shape_kind = prst.get("prst") if prst is not None else None
        if shape_kind in ("ellipse", "roundRect"):
            style.append("border-radius:50%" if shape_kind == "ellipse" else "border-radius:8pt")
        body_html = _text_html(element, theme, (sx + sy) / 2)
        if not body_html and not fill and not stroke:
            continue
        markup = f'<div class="tx" style="{";".join(style)}">{body_html}</div>'
        boxes.append(Box("sp", px, py, pw, ph, rot, markup, z))

    return boxes, skipped


# --------------------------------------------------------------------------- #
# Assembling the document
# --------------------------------------------------------------------------- #


def _rels(archive: zipfile.ZipFile, part: str) -> dict[str, str]:
    directory, _, name = part.rpartition("/")
    rels = f"{directory}/_rels/{name}.rels"
    if rels not in archive.namelist():
        return {}
    root = ET.fromstring(archive.read(rels))
    base = posixpath.dirname(part)
    out: dict[str, str] = {}
    for rel in root.findall("{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"):
        if rel.get("TargetMode") == "External":
            continue
        rid, target = rel.get("Id"), rel.get("Target")
        if rid and target:
            out[rid] = posixpath.normpath(posixpath.join(base, target))
    return out


def _background(
    archive: zipfile.ZipFile, part: str, theme: dict[str, str], media: dict[str, str]
) -> str:
    """Resolve the slide's background through slide → layout → master.

    A slide almost never declares its own: it inherits, and treating "absent" as
    "white" turns every dark deck inside out.
    """
    chain = [part]
    rels = _rels(archive, part)
    layout = next((t for t in rels.values() if "slideLayouts/" in t), None)
    if layout:
        chain.append(layout)
        master = next((t for t in _rels(archive, layout).values() if "slideMasters/" in t), None)
        if master:
            chain.append(master)

    for candidate in chain:
        if candidate not in archive.namelist():
            continue
        root = ET.fromstring(archive.read(candidate))
        bg = root.find(f"{P}cSld/{P}bg")
        if bg is None:
            continue
        blip = bg.find(f".//{A}blip")
        if blip is not None:
            src = media.get(_rels(archive, candidate).get(blip.get(f"{R}embed", ""), ""), "")
            if src:
                return f"background-image:url('{html.escape(src)}');background-size:cover"
        colour = _colour(bg.find(f".//{A}solidFill"), theme)
        if colour:
            return f"background:{colour}"
    return "background:#ffffff"


def build(
    deck: Path,
    out_dir: Path,
    *,
    title: str | None = None,
    max_width: int = 1600,
    overwrite: bool = False,
) -> dict[str, object]:
    """Write `index.html` plus `images/` — one page per slide, shapes in place.

    Raises:
        UsageError: the destination is not empty and ``overwrite`` was not given.
    """
    from agentctl.pptxdeck import (  # local: avoids a cycle, both modules read the same archive
        MEASURABLE_SUFFIXES,
        _downscale,
        _open,
        slide_order,
    )

    if out_dir.exists() and any(out_dir.iterdir()) and not overwrite:
        raise UsageError(f"not empty: {out_dir}", detail="pass --overwrite to replace its contents")

    images = out_dir / "images"
    images.mkdir(parents=True, exist_ok=True)

    with _open(deck) as archive:
        theme = _theme_colours(archive)
        pres = ET.fromstring(archive.read("ppt/presentation.xml"))
        size = pres.find(f"{P}sldSz")
        width = emu_to_pt(size.get("cx") if size is not None else None, 720.0)
        height = emu_to_pt(size.get("cy") if size is not None else None, 540.0)

        media: dict[str, str] = {}
        for index, name in enumerate(
            sorted(n for n in archive.namelist() if n.startswith("ppt/media/")), start=1
        ):
            suffix = Path(name).suffix.lower()
            if suffix not in MEASURABLE_SUFFIXES:
                continue
            target = images / f"m{index}{suffix}"
            target.write_bytes(archive.read(name))
            if max_width:
                _downscale(target, max_width)
            media[name] = f"images/{target.name}"

        slides: list[SlideLayout] = []
        for index, part in enumerate(slide_order(archive), start=1):
            root = ET.fromstring(archive.read(part))
            tree = root.find(f"{P}cSld/{P}spTree")
            rels = _rels(archive, part)
            drawn: list[Box] = []
            missed: list[str] = []
            if tree is not None:
                drawn, missed = _walk(tree, rels, media, theme)
            slides.append(
                SlideLayout(
                    index=index,
                    part=part,
                    width=width,
                    height=height,
                    background=_background(archive, part, theme, media),
                    boxes=drawn,
                    skipped=missed,
                )
            )

    (out_dir / "index.html").write_text(
        _document(slides, title or deck.stem, width, height), encoding="utf-8"
    )
    skipped_total = [s for slide in slides for s in slide.skipped]
    return {
        "out_dir": str(out_dir),
        "index": str(out_dir / "index.html"),
        "slides": len(slides),
        "shapes": sum(len(s.boxes) for s in slides),
        "images": len(media),
        "slide_pt": [round(width, 1), round(height, 1)],
        "approximated": sorted(set(skipped_total)),
        "approximated_count": len(skipped_total),
    }


def _document(slides: list[SlideLayout], title: str, width: float, height: float) -> str:
    pages = []
    for slide in slides:
        parts = []
        for box in sorted(slide.boxes, key=lambda b: b.z):
            transform = f";transform:rotate({box.rotation:.2f}deg)" if box.rotation else ""
            parts.append(
                f'<div class="b" style="left:{box.x:.2f}pt;top:{box.y:.2f}pt;'
                f'width:{box.w:.2f}pt;height:{box.h:.2f}pt{transform}">{box.html}</div>'
            )
        pages.append(
            f'<div class="slide" style="{slide.background}">'
            f'<span class="num">{slide.index}</span>{"".join(parts)}</div>'
        )
    return f"""<title>{html.escape(title)}</title>
<style>
  @page {{ size: {width:.2f}pt {height:.2f}pt; margin: 0; }}
  * {{ box-sizing: border-box; -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
  body {{ margin: 0; background: #6b6b6b;
         font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; }}
  .slide {{ position: relative; width: {width:.2f}pt; height: {height:.2f}pt;
           overflow: hidden; margin: 0 auto; break-after: page; }}
  .b {{ position: absolute; }}
  .b img {{ width: 100%; height: 100%; object-fit: fill; display: block; }}
  .tx {{ width: 100%; height: 100%; display: flex; flex-direction: column;
        justify-content: center; padding: 2pt 4pt; overflow: hidden; }}
  .tx p {{ margin: 0 0 .25em; line-height: 1.22; }}
  .unsupported {{ width: 100%; height: 100%; border: 1pt dashed #b00; color: #b00;
                 font-size: 9pt; display: flex; align-items: center;
                 justify-content: center; background: rgba(255,0,0,.04); }}
  .num {{ position: absolute; right: 6pt; bottom: 4pt; font-size: 7pt;
         color: rgba(0,0,0,.35); z-index: 999; }}
  @media screen {{ .slide {{ margin: 12pt auto; box-shadow: 0 2px 14px rgba(0,0,0,.45); }} }}
</style>
{"".join(pages)}"""
