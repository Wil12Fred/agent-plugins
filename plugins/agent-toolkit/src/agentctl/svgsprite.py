"""Fold a folder of SVG logo variants into one sprite.

Companion to :mod:`the project.devtools.pdfassets`. A brand pack ships each lockup as
its own file (``OM-Logo-01.svg`` … ``OM-Logo-13.svg``); a web app wants one
sprite it references by id (``<use href="#om-logo-01">``) instead of thirteen
requests.

Each source becomes a ``<symbol>`` carrying that file's ``viewBox``, with ids
derived from the filenames so they stay stable and guessable across rebuilds.

Reads the SVGs (never modified). Writes the sprite, and an HTML preview when
asked for one.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from agentctl.errors import ValidationError

SVG_NS = "http://www.w3.org/2000/svg"

SKIP_TAGS = frozenset({f"{{{SVG_NS}}}title", f"{{{SVG_NS}}}desc", f"{{{SVG_NS}}}metadata"})
"""Elements carrying no drawable content — copying them just bloats the sprite."""

_NON_SLUG = re.compile(r"[^a-zA-Z0-9]+")
_NUMBER = re.compile(r"[\d.]+")


def slugify(name: str) -> str:
    """Filename to a stable symbol id."""
    slug = _NON_SLUG.sub("-", name).strip("-").lower()
    return slug or "symbol"


def build_symbol(path: Path, symbol_id: str) -> ET.Element | None:
    """Turn one SVG file into a ``<symbol>``, or ``None`` if it draws nothing."""
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return None

    symbol = ET.Element(f"{{{SVG_NS}}}symbol", {"id": symbol_id})
    view_box = root.get("viewBox")
    if view_box:
        symbol.set("viewBox", view_box)
    else:
        # No viewBox means the symbol would not scale inside <use>; synthesise
        # one from width/height, which export tools usually do provide.
        width, height = root.get("width"), root.get("height")
        if width and height:
            w, h = _NUMBER.search(width), _NUMBER.search(height)
            if w and h:
                symbol.set("viewBox", f"0 0 {w.group()} {h.group()}")

    for child in root:
        if child.tag not in SKIP_TAGS:
            symbol.append(child)
    return symbol if len(symbol) else None


def combine(sources: list[Path], out: Path, *, id_prefix: str = "") -> list[str]:
    """Write a sprite containing one symbol per readable source.

    Returns:
        The symbol ids, in the order they appear in the sprite.

    Raises:
        ValidationError: no readable .svg input, or nothing drawable in them.
    """
    readable = sorted(p for p in sources if p.is_file() and p.suffix.lower() == ".svg")
    if not readable:
        raise ValidationError("no readable .svg inputs")

    # `register_namespace("", SVG_NS)` already makes ElementTree emit the
    # xmlns declaration for the namespaced tag. Passing xmlns in attrib as well
    # emitted it twice, and `<svg xmlns="..." xmlns="...">` is not well-formed
    # XML — a browser in XML mode or any strict consumer rejects the sprite.
    ET.register_namespace("", SVG_NS)
    sprite = ET.Element(f"{{{SVG_NS}}}svg", {"style": "display:none"})

    ids: list[str] = []
    for path in readable:
        symbol_id = f"{id_prefix}{'-' if id_prefix else ''}{slugify(path.stem)}"
        symbol = build_symbol(path, symbol_id)
        if symbol is None:
            continue
        sprite.append(symbol)
        ids.append(symbol_id)

    if not ids:
        raise ValidationError("no drawable symbol could be built from the inputs")

    out.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(sprite).write(out, encoding="unicode", xml_declaration=True)
    return ids


PREVIEW_STYLE = (
    "<!doctype html><meta charset=utf-8>"
    "<style>body{background:#9a9a9a;font:12px sans-serif;display:flex;flex-wrap:wrap;gap:12px}"
    "figure{margin:0;background:#fff;padding:8px;width:160px}"
    "svg{width:100%;height:120px}</style>"
)


def write_preview(sprite: Path, preview: Path, ids: list[str]) -> None:
    """Write an HTML page showing every symbol, on grey so white logos show."""
    tiles = "\n".join(
        f'  <figure><svg viewBox="0 0 100 100"><use href="#{symbol_id}"/></svg>'
        f"<figcaption>{symbol_id}</figcaption></figure>"
        for symbol_id in ids
    )
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.write_text(
        f"{PREVIEW_STYLE}\n{sprite.read_text(encoding='utf-8')}\n{tiles}\n", encoding="utf-8"
    )
