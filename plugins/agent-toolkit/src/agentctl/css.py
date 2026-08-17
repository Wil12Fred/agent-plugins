"""Re-hue a stylesheet without wrecking its contrast.

Rebranding a delivered site means moving every colour to a new hue while the
design keeps working. A naive hue rotation ruins that, because greys and
near-whites have a hue too: rotating them turns a page's neutrals into a tinted
mess, and rotating pure black or pure white does nothing at all.

So colours are treated in four classes, all in HSL, and **lightness is never
changed for saturated colours** — lightness is what carries contrast, and
contrast is what keeps text readable:

* saturated colours: hue rotated by ``delta``, saturation and lightness kept;
* near-white (``L >= white_threshold``): re-tinted to a very light cream, never
  pure white, so the new palette reads as warm rather than clinical;
* near-black (``L <= black_threshold``): a very dark warm tone, never ``#000``;
* mid greys: a slight tint at the same lightness so they harmonise.

The alpha channel of ``#RRGGBBAA`` is preserved verbatim.
"""

from __future__ import annotations

import colorsys
import re
from dataclasses import dataclass
from pathlib import Path

HEX_RE = re.compile(r"#([0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")

GRAY_SATURATION_MAX = 0.08
"""Below this saturation a colour is a grey, whatever its hue claims."""

NEAR_BLACK_LIGHTNESS = 0.10
MIN_BLACK_LIGHTNESS = 0.06
MAX_WHITE_LIGHTNESS = 0.955
WHITE_SATURATION = 0.85
BLACK_SATURATION = 0.35
GRAY_SATURATION = 0.07
BLACK_HUE_OFFSET = 12
"""Blacks are tinted slightly off the grey hue so they do not read as a flat wash."""


@dataclass(frozen=True)
class HueShift:
    """The transform, as a value so it can be tested and reported."""

    delta: float
    """Hue rotation in degrees for saturated colours."""

    gray_hue: float = 40
    """Hue used to tint greys, whites and blacks (default: warm gold)."""

    white_threshold: float = 0.93
    black_threshold: float = 0.13

    def apply(self, hex_body: str) -> str:
        """Shift one hex literal (without the ``#``) and return the new literal."""
        alpha = ""
        if len(hex_body) == 3:
            hex_body = "".join(char * 2 for char in hex_body)
        elif len(hex_body) == 8:
            alpha = hex_body[6:]
            hex_body = hex_body[:6]

        red, green, blue = (int(hex_body[i : i + 2], 16) / 255 for i in (0, 2, 4))
        hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)

        if lightness >= self.white_threshold:
            hue, saturation = self.gray_hue / 360, WHITE_SATURATION
            lightness = min(lightness, MAX_WHITE_LIGHTNESS)
        elif lightness <= NEAR_BLACK_LIGHTNESS or (
            lightness <= self.black_threshold and saturation < GRAY_SATURATION_MAX
        ):
            hue, saturation = (self.gray_hue - BLACK_HUE_OFFSET) / 360, BLACK_SATURATION
            lightness = max(lightness, MIN_BLACK_LIGHTNESS)
        elif saturation < GRAY_SATURATION_MAX:
            hue, saturation = self.gray_hue / 360, GRAY_SATURATION
        else:
            hue = (hue + self.delta / 360) % 1.0

        red, green, blue = colorsys.hls_to_rgb(hue, lightness, saturation)
        shifted = f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"
        return shifted + alpha.lower()


def collect(paths: list[Path]) -> list[Path]:
    """Every ``*.css`` under the given files/directories, recursively."""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.rglob("*.css")))
        elif path.suffix == ".css" and path.is_file():
            files.append(path)
    return files


def shift_text(text: str, shift: HueShift) -> tuple[str, dict[str, str]]:
    """Rewrite every hex literal in ``text``; also return the old→new mapping."""
    mapping: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        new = shift.apply(match.group(1))
        mapping[match.group(0).lower()] = new
        return new

    return HEX_RE.sub(replace, text), mapping


def shift_files(files: list[Path], shift: HueShift, *, write: bool) -> tuple[dict[str, str], int]:
    """Apply the shift across files.

    Args:
        write: actually rewrite the files. When ``False`` the mapping is
            computed and returned but nothing is touched — the stylesheets are
            rewritten **in place**, so the preview is the default.

    Returns:
        ``(mapping, changed_file_count)``.
    """
    mapping: dict[str, str] = {}
    changed = 0
    for path in files:
        text = path.read_text(encoding="utf-8")
        new_text, found = shift_text(text, shift)
        mapping.update(found)
        if new_text != text:
            changed += 1
            if write:
                path.write_text(new_text, encoding="utf-8")
    return mapping, changed
