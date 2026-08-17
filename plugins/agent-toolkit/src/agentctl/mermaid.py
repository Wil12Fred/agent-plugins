"""Render Mermaid diagrams to PNG/SVG using the Chrome already on this machine.

``@mermaid-js/mermaid-cli`` drives a headless browser. Left to itself its
puppeteer dependency downloads a private ~300MB Chromium on first run, per
temp install. Pointing it at the system ``google-chrome-stable`` through a
puppeteer config file avoids that entirely and makes the render reproducible.

Input is either a ``.mmd`` file or a Markdown file, in which case every
```` ```mermaid ```` fence becomes its own diagram — the docs in this repo keep
several per file, so one Markdown file yields ``<base>-01.png``,
``<base>-02.png``, … and a single-diagram file keeps the plain ``<base>.png``.

Reads the input files. Writes images into the output directory, nothing else.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from agentctl.errors import ApiError, ConfigError, NotFoundError

MERMAID_CLI = "@mermaid-js/mermaid-cli@10.9.1"
"""Pinned: newer majors changed both the CLI flags and the default theme."""

CHROME_CANDIDATES = ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser")

FENCE_RE = re.compile(r"^```mermaid[ \t]*$(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)

RENDER_WIDTH = 2400
RENDER_HEIGHT = 1800
RENDER_SCALE = 2
RENDER_TIMEOUT = 600


@dataclass(frozen=True)
class Rendered:
    source: Path
    output: Path
    index: int


def find_chrome() -> str:
    """Locate a system Chrome/Chromium, or explain that there is none."""
    for candidate in CHROME_CANDIDATES:
        path = shutil.which(candidate)
        if path:
            return path
    raise ConfigError(
        "no chrome/chromium on PATH",
        detail=f"mermaid-cli needs a browser; tried {', '.join(CHROME_CANDIDATES)}",
    )


def require_npx() -> str:
    path = shutil.which("npx")
    if path is None:
        raise ConfigError("npx not found: install Node.js")
    return path


def extract_blocks(text: str) -> list[str]:
    """Pull every ```mermaid fence out of a Markdown document, in order."""
    return [match.group(1).strip("\n") for match in FENCE_RE.finditer(text)]


def diagrams_in(path: Path) -> list[str]:
    """Diagram sources in one file: the whole file for .mmd, the fences for .md."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".mmd":
        return [text]
    return extract_blocks(text)


def collect_inputs(target: Path) -> list[Path]:
    """One file, or every .md/.mmd directly inside a directory (not recursive)."""
    if not target.exists():
        raise NotFoundError(f"not found: {target}")
    if target.is_file():
        return [target]
    return sorted(
        path
        for path in target.iterdir()
        if path.is_file() and path.suffix.lower() in {".md", ".mmd"}
    )


def _puppeteer_config(directory: Path) -> Path:
    """Write the config that pins mermaid-cli to the system browser."""
    config = directory / "puppeteer.json"
    config.write_text(
        json.dumps(
            {
                "executablePath": find_chrome(),
                "args": ["--no-sandbox", "--disable-setuid-sandbox"],
            }
        ),
        encoding="utf-8",
    )
    return config


def planned_outputs(target: Path, *, out_dir: Path | None = None, svg: bool = False) -> list[Path]:
    """The image paths a :func:`render` would write, without running anything.

    The CLI uses this to tell "creating derived files in a fresh directory"
    apart from "silently replacing images that are already committed" — only
    the second needs confirmation.
    """
    base_dir = target if target.is_dir() else target.parent
    destination = out_dir or (base_dir / "rendered")
    planned: list[Path] = []
    for source in collect_inputs(target):
        blocks = diagrams_in(source)
        for index in range(1, len(blocks) + 1):
            stem = source.stem
            png = destination / (f"{stem}.png" if len(blocks) == 1 else f"{stem}-{index:02d}.png")
            planned.append(png)
            if svg:
                planned.append(png.with_suffix(".svg"))
    return planned


def render(
    target: Path,
    *,
    out_dir: Path | None = None,
    svg: bool = False,
    background: str = "white",
) -> list[Rendered]:
    """Render every diagram found under ``target``.

    Args:
        target: a ``.md``/``.mmd`` file, or a directory of them.
        out_dir: destination; defaults to ``<input dir>/rendered``.
        svg: also emit an SVG (on a transparent background) beside each PNG.

    Returns:
        One :class:`Rendered` per image written.
    """
    inputs = collect_inputs(target)
    base_dir = target if target.is_dir() else target.parent
    destination = out_dir or (base_dir / "rendered")
    destination.mkdir(parents=True, exist_ok=True)

    npx = require_npx()
    results: list[Rendered] = []

    with tempfile.TemporaryDirectory(prefix="agentctl-mermaid-") as tmp:
        work = Path(tmp)
        config = _puppeteer_config(work)

        for source in inputs:
            blocks = diagrams_in(source)
            if not blocks:
                continue
            for index, block in enumerate(blocks, start=1):
                stem = source.stem
                mmd = work / f"{stem}-{index:02d}.mmd"
                mmd.write_text(block, encoding="utf-8")
                png = destination / (
                    f"{stem}.png" if len(blocks) == 1 else f"{stem}-{index:02d}.png"
                )
                _run_mmdc(npx, mmd, png, config, background=background)
                results.append(Rendered(source=source, output=png, index=index))
                if svg:
                    svg_path = png.with_suffix(".svg")
                    _run_mmdc(npx, mmd, svg_path, config, background="transparent")
                    results.append(Rendered(source=source, output=svg_path, index=index))
    return results


def _run_mmdc(npx: str, mmd: Path, out: Path, config: Path, *, background: str) -> None:
    command = [
        npx,
        "--yes",
        MERMAID_CLI,
        "-i",
        str(mmd),
        "-o",
        str(out),
        "-p",
        str(config),
        "-b",
        background,
    ]
    if out.suffix.lower() == ".png":
        command += ["-w", str(RENDER_WIDTH), "-H", str(RENDER_HEIGHT), "-s", str(RENDER_SCALE)]
    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=RENDER_TIMEOUT, check=False
    )
    if completed.returncode != 0 or not out.exists():
        raise ApiError(
            f"mermaid-cli failed rendering {mmd.name}",
            body=(completed.stderr or completed.stdout).strip()[:500] or None,
        )
