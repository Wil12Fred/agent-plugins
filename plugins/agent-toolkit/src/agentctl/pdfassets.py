"""Pull the usable assets out of a brand manual PDF.

A client's brand manual (``OM_Studio_Manual_2026.pdf`` and friends) is the only
delivery of their palette swatches, logo lockups, interior photography and
graphic resources — all of it welded inside a PDF. Three different extractions
are needed, and picking the wrong one costs quality:

* :data:`Mode.PAGES` renders each page to a raster. Right for *composed*
  layouts — mockups, palette boards, type specimens — where the page as laid out
  is the asset.
* :data:`Mode.IMAGES` lifts the raster images **embedded** in the PDF at their
  native resolution. No re-rasterisation, so photography keeps its full quality;
  rendering the page instead would resample it to the render dpi.
* :data:`Mode.SVG` converts a page to SVG with its vector paths intact. This is
  how a logo comes out as a real SVG rather than a traced bitmap.

The modes are independent and can be combined in one run.

Requires poppler-utils (``pdftoppm``, ``pdfimages``, ``pdftocairo``,
``pdfinfo``). ImageMagick's ``identify`` is optional and only powers the size
filters; without it every extracted file is kept.

Reads the PDF (never modified). Writes only inside the output directory: the
numbered assets plus a ``manifest.csv`` describing each one.
"""

from __future__ import annotations

import csv
import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from agentctl.errors import ApiError, ConfigError, NotFoundError, ValidationError


class Mode(StrEnum):
    """Which extraction to run."""

    PAGES = "pages"
    IMAGES = "images"
    SVG = "svg"


class RasterFormat(StrEnum):
    PNG = "png"
    JPG = "jpg"


TOOL_FOR_MODE: dict[Mode, str] = {
    Mode.PAGES: "pdftoppm",
    Mode.IMAGES: "pdfimages",
    Mode.SVG: "pdftocairo",
}

RASTER_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".ppm", ".pbm", ".tif", ".tiff"})
POPPLER_HINT = "install poppler-utils (apt install poppler-utils)"


@dataclass(frozen=True)
class Filters:
    """Post-extraction filters, applied to raster output only."""

    min_width: int = 0
    min_height: int = 0
    drop_masks: bool = False
    """Drop grayscale images: in a manual those are almost always alpha masks,
    not artwork, and they outnumber the real photographs."""


@dataclass(frozen=True)
class ImageMeta:
    width: int
    height: int
    colorspace: str


def require_tool(name: str) -> str:
    """Resolve an external binary or explain how to get it."""
    path = shutil.which(name)
    if path is None:
        raise ConfigError(f"`{name}` not found: {POPPLER_HINT}")
    return path


def _run(command: list[str], *, keep_going: bool) -> None:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0 and not keep_going:
        raise ApiError(
            f"{Path(command[0]).name} failed",
            detail=completed.stderr.strip()[:400] or None,
        )


def _page_range(first: int | None, last: int | None) -> list[str]:
    args: list[str] = []
    if first is not None:
        args += ["-f", str(first)]
    if last is not None:
        args += ["-l", str(last)]
    return args


def identify(path: Path, *, timeout: float | None = None) -> ImageMeta | None:
    """Read pixel size and colorspace via ImageMagick, or ``None`` if absent.

    ``-ping`` stops after the header. Without it ImageMagick decodes the whole
    file to answer a question the header already contains, which on a large
    image is slow and on a video is unbounded — a 93 MB embedded mp4 handed to
    `identify` does not come back, and the caller looks hung rather than wrong.
    A caller that cannot vouch for what it is pointing at should also pass a
    ``timeout``: unknown dimensions are a worse answer than measured ones and a
    far better one than no answer at all.
    """
    binary = shutil.which("identify")
    if binary is None:
        return None
    try:
        completed = subprocess.run(
            [binary, "-ping", "-format", "%w %h %[colorspace]", f"{path}[0]"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    parts = completed.stdout.split()
    if len(parts) < 3:
        return None
    return ImageMeta(int(parts[0]), int(parts[1]), parts[2])


def page_count(pdf: Path) -> int:
    """Total pages, via ``pdfinfo``."""
    completed = subprocess.run(
        [require_tool("pdfinfo"), str(pdf)], capture_output=True, text=True, check=False
    )
    for line in completed.stdout.splitlines():
        if line.startswith("Pages:"):
            return int(line.split(":", 1)[1].strip())
    raise ApiError("pdfinfo did not report a page count", detail=completed.stderr[:200] or None)


def _extract_pages(
    pdf: Path,
    work: Path,
    *,
    dpi: int,
    fmt: RasterFormat,
    first: int | None,
    last: int | None,
    keep_going: bool,
) -> None:
    flag = "-png" if fmt is RasterFormat.PNG else "-jpeg"
    _run(
        [
            require_tool("pdftoppm"),
            flag,
            "-r",
            str(dpi),
            *_page_range(first, last),
            str(pdf),
            str(work / "page"),
        ],
        keep_going=keep_going,
    )


def _extract_images(
    pdf: Path, work: Path, *, first: int | None, last: int | None, keep_going: bool
) -> None:
    _run(
        [
            require_tool("pdfimages"),
            "-all",
            *_page_range(first, last),
            str(pdf),
            str(work / "img"),
        ],
        keep_going=keep_going,
    )


def _extract_svg(
    pdf: Path, work: Path, *, first: int | None, last: int | None, keep_going: bool
) -> None:
    """pdftocairo writes one SVG per invocation, so the range is looped."""
    tool = require_tool("pdftocairo")
    start = first or 1
    end = last if last is not None else page_count(pdf)
    for page in range(start, end + 1):
        _run(
            [
                tool,
                "-svg",
                "-f",
                str(page),
                "-l",
                str(page),
                str(pdf),
                str(work / f"vec-{page:04d}.svg"),
            ],
            keep_going=keep_going,
        )


def keep_file(path: Path, filters: Filters) -> bool:
    """Decide whether an extracted file survives the filters.

    SVG is always kept: a vector file has no pixel size to compare, and judging
    it by byte count would throw away the logos this mode exists to get.
    """
    suffix = path.suffix.lower()
    if suffix == ".svg":
        return path.stat().st_size > 0
    if suffix not in RASTER_SUFFIXES:
        return False
    meta = identify(path)
    if meta is None:
        # No ImageMagick: keep everything rather than silently discard assets.
        return True
    if filters.min_width and meta.width < filters.min_width:
        return False
    if filters.min_height and meta.height < filters.min_height:
        return False
    return not (filters.drop_masks and meta.colorspace.lower() == "gray")


def extract(
    pdf: Path,
    out_dir: Path,
    *,
    modes: list[Mode],
    prefix: str | None = None,
    dpi: int = 150,
    fmt: RasterFormat = RasterFormat.PNG,
    first: int | None = None,
    last: int | None = None,
    filters: Filters | None = None,
    keep_going: bool = False,
) -> list[dict[str, object]]:
    """Extract assets into ``out_dir`` and return the manifest rows.

    Output is renumbered sequentially (``<prefix>_1.png``, ``<prefix>_2.png``, …)
    after filtering, so the numbers are contiguous rather than reflecting which
    page each file came from — ``manifest.csv`` records the original name.

    Raises:
        NotFoundError: the PDF does not exist.
        ValidationError: nothing survived extraction and filtering.
    """
    if not pdf.is_file():
        raise NotFoundError(f"not a file: {pdf}")
    filters = filters or Filters()
    prefix = prefix or pdf.stem.replace(" ", "_")

    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / ".work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()

    try:
        for mode in modes:
            if mode is Mode.PAGES:
                _extract_pages(
                    pdf, work, dpi=dpi, fmt=fmt, first=first, last=last, keep_going=keep_going
                )
            elif mode is Mode.IMAGES:
                _extract_images(pdf, work, first=first, last=last, keep_going=keep_going)
            else:
                _extract_svg(pdf, work, first=first, last=last, keep_going=keep_going)

        produced = sorted(path for path in work.iterdir() if path.is_file())
        kept = [path for path in produced if keep_file(path, filters)]
        if not kept:
            raise ValidationError(
                "no output produced",
                detail="check the page range and the --min-width/--min-height filters",
            )

        rows: list[dict[str, object]] = []
        for index, source in enumerate(kept, start=1):
            target = out_dir / f"{prefix}_{index}{source.suffix.lower()}"
            original = source.name
            shutil.move(str(source), target)
            meta = identify(target)
            rows.append(
                {
                    "file": target.name,
                    "width": meta.width if meta else 0,
                    "height": meta.height if meta else 0,
                    "colorspace": meta.colorspace if meta else "",
                    "bytes": target.stat().st_size,
                    "extracted_from": original,
                }
            )

        manifest = out_dir / "manifest.csv"
        with manifest.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        return rows
    finally:
        shutil.rmtree(work, ignore_errors=True)
