"""Print an HTML file to PDF with a browser, because a browser is already here.

The obvious answers to "make a PDF" are LaTeX and LibreOffice, and on a machine
with neither, a third is usually sitting in a cache directory: **Chromium ships a
print engine**, and it is the same one that renders the page on screen. For a
document that was authored as HTML — a report, a plan, a table — that is not a
fallback, it is the correct tool. The layout you checked in a browser is the
layout that comes out.

This is deliberately *not* the answer for `dev pptx pdf`. Converting a deck needs
a slide layout engine; Chromium cannot open a `.pptx` at all. The two commands
solve different problems and neither substitutes for the other.

What the browser gets wrong unless told otherwise, all of it measured rather than
assumed:

* **It stamps every page with the URL and the date.** On by default, and it makes
  a clean document look like a printed web page. Off here, always.
* **Paper size is not a command-line flag.** `--print-to-pdf` has no option for
  it; the size comes from the page's own ``@page`` rule, and without one you get
  US Letter wherever you are. ``paper`` injects the rule.
* **Injecting that rule cannot mean copying the file somewhere else.** A copy in
  a temporary directory silently loses every relative ``<img src="images/…">``,
  and the PDF comes out with the text and none of the pictures — which looks
  like a broken document rather than a wrong working directory. The temporary
  file is written *beside the original* for that reason, and removed afterwards.
* **It can exit 0 having written nothing.** So the result is verified by reading
  the file back and checking the ``%PDF-`` magic.

Reads the source (never modified, except for a sibling temp file it deletes).
Writes only the destination.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from agentctl.errors import ApiError, ConfigError, NotFoundError, UsageError, ValidationError

PDF_MAGIC = b"%PDF-"

BROWSER_ENV = "CHROME_BINARY"
"""Point this at a Chromium-family binary to settle the question outright."""

BROWSER_NAMES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "brave",
    "brave-browser",
    "microsoft-edge",
)

PLAYWRIGHT_CACHES = (
    Path.home() / ".cache" / "ms-playwright",
    Path.home() / "Library" / "Caches" / "ms-playwright",
    Path(os.environ.get("LOCALAPPDATA", "/nonexistent")) / "ms-playwright",
)
"""Playwright's own download location on the three platforms.

Searched last, and the binary that was used is always named in the result. A
machine with no browser on `PATH` very often has one here, and refusing while a
usable engine sits in a well-known directory is unhelpful — but picking it up
*silently* would make the output depend on something the caller never mentioned.
"""

PAPER_SIZES = {
    "A4": "210mm 297mm",
    "A3": "297mm 420mm",
    "A5": "148mm 210mm",
    "Letter": "8.5in 11in",
    "Legal": "8.5in 14in",
    "Tabloid": "11in 17in",
}


def find_browser() -> tuple[str, str]:
    """Resolve a Chromium-family binary. Returns ``(path, how_it_was_found)``."""
    override = os.environ.get(BROWSER_ENV)
    if override:
        if not Path(override).is_file():
            raise ConfigError(
                f"{BROWSER_ENV} points at nothing: {override}",
                detail="unset it to search PATH instead",
            )
        return override, BROWSER_ENV

    for name in BROWSER_NAMES:
        found = shutil.which(name)
        if found:
            return found, "PATH"

    for cache in PLAYWRIGHT_CACHES:
        if not cache.is_dir():
            continue
        patterns = (
            "chromium-*/chrome-linux/chrome",
            "chromium-*/chrome-mac/*.app/Contents/MacOS/*",
            "chromium-*/chrome-win/chrome.exe",
        )
        for pattern in patterns:
            for candidate in sorted(cache.glob(pattern), reverse=True):
                if candidate.is_file():
                    return str(candidate), "playwright cache"

    raise ConfigError(
        "no Chromium-family browser found",
        detail=f"install chromium, or set {BROWSER_ENV} to a Chrome/Chromium/Edge binary",
    )


def page_rule(paper: str | None, margin: str | None) -> str:
    """The ``@page`` rule to inject, or an empty string when nothing was asked."""
    parts = []
    if paper:
        size = PAPER_SIZES.get(paper) or PAPER_SIZES.get(paper.title())
        if size is None:
            raise UsageError(
                f"unknown paper size: {paper}",
                detail=f"one of {', '.join(PAPER_SIZES)}, or omit for the page's own rule",
            )
        parts.append(f"size: {size};")
    if margin:
        if not re.fullmatch(r"[0-9.]+(mm|cm|in|pt|px)", margin):
            raise UsageError(
                f"margin is not a CSS length: {margin}", detail="e.g. 12mm, 0.5in, 36pt"
            )
        parts.append(f"margin: {margin};")
    return f"@page {{ {' '.join(parts)} }}" if parts else ""


def count_pages(pdf: Path) -> int:
    """Page count from the bytes. `/Type /Pages` is the tree node, not a leaf."""
    try:
        return len(re.findall(rb"/Type\s*/Page(?![sA-Za-z])", pdf.read_bytes()))
    except OSError:
        return 0


def render(
    source: Path,
    out: Path,
    *,
    paper: str | None = None,
    margin: str | None = None,
    timeout: float = 120.0,
    overwrite: bool = False,
) -> dict[str, object]:
    """Print ``source`` to ``out`` with a headless browser.

    Raises:
        NotFoundError: the source does not exist.
        ConfigError: no browser, or ``CHROME_BINARY`` points at nothing.
        UsageError: bad paper size or margin, or the output exists.
        ApiError: the browser failed or ran past the timeout.
        ValidationError: it produced nothing, or something that is not a PDF.
    """
    if not source.is_file():
        raise NotFoundError(f"not a file: {source}")
    if out.exists() and not overwrite:
        raise UsageError(f"already exists: {out}", detail="pass --overwrite to replace it")

    binary, how = find_browser()
    rule = page_rule(paper, margin)
    out.parent.mkdir(parents=True, exist_ok=True)

    target = source
    scratch: Path | None = None
    if rule:
        # Beside the original, never in a temp directory: relative asset paths
        # are resolved against the document's own location.
        scratch = source.with_name(f".agentctl-print-{uuid.uuid4().hex[:8]}-{source.name}")
        scratch.write_text(
            source.read_text(encoding="utf-8") + f"\n<style>{rule}</style>\n", encoding="utf-8"
        )
        target = scratch

    try:
        command = [
            binary,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            "--no-pdf-header-footer",
            f"--print-to-pdf={out}",
            target.resolve().as_uri(),
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, check=False, timeout=timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise ApiError(
                f"the browser did not finish within {timeout:.0f}s",
                detail="a page fetching remote fonts or large images is slow; raise --timeout",
            ) from exc
    finally:
        if scratch is not None:
            scratch.unlink(missing_ok=True)

    if not out.is_file():
        raise ValidationError(
            "the browser produced no PDF",
            detail=(completed.stderr or completed.stdout).strip()[:300] or "it wrote no file",
        )
    if out.read_bytes()[:5] != PDF_MAGIC:
        size = out.stat().st_size
        out.unlink(missing_ok=True)
        raise ValidationError(
            f"the output is not a PDF ({size} bytes)",
            detail="the browser wrote something else under a .pdf name",
        )

    return {
        "out": str(out),
        "bytes": out.stat().st_size,
        "pages": count_pages(out),
        "renderer": Path(binary).name,
        "found_via": how,
        "paper": paper or "the page's own @page rule",
    }
