"""The general utilities: the parts that are arithmetic rather than a subprocess.

`pdf extract` and `mermaid render` shell out to real binaries, so what is
testable offline is the logic around them — the colour maths, the SVG assembly,
and the shapes that must be refused. That is also where the bugs are: a wrong
subprocess call fails loudly, a wrong hue does not.

Each test names the rule it enforces.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from agentctl import css, svgsprite
from agentctl.errors import ValidationError


# --------------------------------------------------------------------------- #
# Hue shifting
# --------------------------------------------------------------------------- #


def test_a_full_rotation_returns_the_colour_it_started_from() -> None:
    """Rule: 360° is identity. The cheapest possible check that the maths closes.

    Note the `#`: `apply` returns a literal, not a body. Comparing against the
    bare hex is the mistake this assertion is written to stop repeating.
    """
    assert css.HueShift(delta=360).apply("3366cc").lower() == "#3366cc"
    assert css.HueShift(delta=0).apply("3366cc").lower() == "#3366cc"


def test_a_shift_changes_a_saturated_colour() -> None:
    """The control: without it, 'returns its input' would pass the test above."""
    assert css.HueShift(delta=120).apply("3366cc").lower() != "3366cc"


def test_a_three_digit_hex_is_expanded_before_shifting() -> None:
    """Rule: `#abc` is a real stylesheet colour and must not be read as one byte."""
    assert len(css.HueShift(delta=90).apply("abc")) >= 6


def test_grey_is_tinted_rather_than_rotated() -> None:
    """Rule: rotating a grey does nothing — its hue is undefined.

    A transform that silently no-ops on every neutral leaves half a stylesheet
    unchanged and looks like it worked.
    """
    warm = css.HueShift(delta=0, gray_hue=40).apply("808080")
    assert warm.lower() != "808080"


def test_shifting_text_reports_what_it_changed() -> None:
    """Rule: the mapping is the evidence. A count alone cannot be checked."""
    text, mapping = css.shift_text(".a{color:#3366cc}", css.HueShift(delta=120))

    assert mapping, "a shift that changed something must say what"
    for before, after in mapping.items():
        assert before in ".a{color:#3366cc}" or before.lstrip("#") in "3366cc"
        assert after in text


def test_a_stylesheet_with_no_colours_is_left_alone() -> None:
    text, mapping = css.shift_text(".a{margin:0}", css.HueShift(delta=120))
    assert text == ".a{margin:0}"
    assert mapping == {}


# --------------------------------------------------------------------------- #
# SVG sprites
# --------------------------------------------------------------------------- #


def _svg(path: Path, body: str = '<rect width="10" height="10"/>') -> Path:
    path.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">{body}</svg>',
        encoding="utf-8",
    )
    return path


def test_a_symbol_id_is_slugged_from_the_filename() -> None:
    """Rule: the id ends up in markup, so spaces, case and dots cannot survive.

    `slugify` takes whatever it is given — `combine` passes the *stem*, so the
    extension never reaches an id in practice. Passing a full filename here
    documents that the function itself does not strip one.
    """
    assert svgsprite.slugify("Brand Lockup 02") == "brand-lockup-02"
    assert svgsprite.slugify("Brand Lockup 02.svg") == "brand-lockup-02-svg"


def test_combining_produces_one_symbol_per_source(tmp_path: Path) -> None:
    sources = [_svg(tmp_path / f"icon-{i}.svg") for i in range(3)]
    out = tmp_path / "sprite.svg"

    ids = svgsprite.combine(sources, out)

    assert len(ids) == 3
    root = ET.fromstring(out.read_text(encoding="utf-8"))
    symbols = root.findall(".//{http://www.w3.org/2000/svg}symbol")
    assert len(symbols) == 3


def test_the_viewbox_travels_with_the_symbol(tmp_path: Path) -> None:
    """Rule: a symbol without its viewBox renders at the wrong size, silently.

    Nothing errors — the sprite is valid SVG and every icon is wrong.
    """
    out = tmp_path / "sprite.svg"
    svgsprite.combine([_svg(tmp_path / "a.svg")], out)

    root = ET.fromstring(out.read_text(encoding="utf-8"))
    symbol = root.find(".//{http://www.w3.org/2000/svg}symbol")
    assert symbol is not None
    assert symbol.get("viewBox") == "0 0 10 10"


def test_an_id_prefix_is_applied_to_every_symbol(tmp_path: Path) -> None:
    out = tmp_path / "sprite.svg"
    ids = svgsprite.combine([_svg(tmp_path / "a.svg")], out, id_prefix="brand")
    assert ids == ["brand-a"], "the separator is supplied by combine, not by the caller"


def test_a_file_that_is_not_svg_is_refused(tmp_path: Path) -> None:
    """Rule: refuse rather than emit a sprite with a hole in it."""
    bad = tmp_path / "not.svg"
    bad.write_text("this is not xml", encoding="utf-8")

    with pytest.raises((ValidationError, ET.ParseError)):
        svgsprite.combine([bad], tmp_path / "sprite.svg")
