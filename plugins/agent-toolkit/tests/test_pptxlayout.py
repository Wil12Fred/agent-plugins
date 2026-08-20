"""Laying a deck out from the geometry the file carries.

Everything here is arithmetic on numbers in the XML, so everything here is
testable offline — which is the argument for doing it this way rather than
shelling out to a layout engine. The cases are the ones where getting it wrong
produces a picture that looks plausible and is not what the deck says.

Each test names the rule it enforces.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from agentctl import pptxlayout as L


def _el(xml: str) -> ET.Element:
    return ET.fromstring(
        xml.replace("<p:", f"<{{{L.NS_P}}}").replace("</p:", f"</{{{L.NS_P}}}")
        if False
        else f'<root xmlns:p="{L.NS_P}" xmlns:a="{L.NS_A}" xmlns:r="{L.NS_R}">{xml}</root>'
    )


# --------------------------------------------------------------------------- #
# Units
# --------------------------------------------------------------------------- #


def test_emu_converts_to_points_at_the_real_ratio() -> None:
    """Rule: 914400 EMU to the inch, 72 points to the inch.

    A wrong constant does not crash — it produces a slide whose every element is
    proportionally misplaced, which reads as a broken renderer.
    """
    assert L.emu_to_pt(914400) == pytest.approx(72.0)
    assert L.emu_to_pt(12192000) == pytest.approx(960.0), "a 13.33in slide is 960pt"


def test_a_missing_measurement_falls_back_rather_than_raising() -> None:
    """The control: shapes legitimately omit values, and one must not kill a deck."""
    assert L.emu_to_pt(None, 5.0) == 5.0
    assert L.emu_to_pt("not a number", 5.0) == 5.0


# --------------------------------------------------------------------------- #
# Colour
# --------------------------------------------------------------------------- #


def test_a_scheme_colour_resolves_through_the_theme() -> None:
    """Rule: `schemeClr` is a name, not a colour.

    Left unresolved it renders as nothing, and a deck whose text is all
    `tx1` comes out invisible rather than obviously broken.
    """
    node = _el('<a:solidFill><a:schemeClr val="accent1"/></a:solidFill>')[0]
    assert L._colour(node, {"accent1": "#ea403e"}) == "#ea403e"


def test_an_explicit_colour_wins_and_alpha_is_kept() -> None:
    xml = '<a:solidFill><a:srgbClr val="FF0000"><a:alpha val="50000"/></a:srgbClr></a:solidFill>'
    node = _el(xml)[0]
    assert L._colour(node, {}).startswith("#FF0000")
    assert len(L._colour(node, {})) == 9, "eight hex digits means the alpha survived"


def test_a_colour_that_says_nothing_returns_none() -> None:
    """The control: without it, 'always return a colour' would pass the two above."""
    assert L._colour(None, {}) is None
    assert L._colour(_el("<a:noFill/>")[0], {}) is None


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #


def test_position_and_size_come_out_in_points() -> None:
    shape = _el(
        '<p:sp><p:spPr><a:xfrm><a:off x="914400" y="457200"/>'
        '<a:ext cx="1828800" cy="914400"/></a:xfrm></p:spPr></p:sp>'
    )[0]
    x, y, w, h, rot = L._xfrm(shape)
    assert (x, y, w, h) == pytest.approx((72.0, 36.0, 144.0, 72.0))
    assert rot == 0


def test_rotation_is_read_in_sixtieths_of_a_degree() -> None:
    """Rule: `rot` is 1/60000th of a degree, not degrees.

    Treated as degrees, a 45° shape is rotated 2.7 million degrees — which
    modulo 360 lands somewhere arbitrary and looks like a random bug.
    """
    shape = _el(
        '<p:sp><p:spPr><a:xfrm rot="2700000"><a:off x="0" y="0"/>'
        '<a:ext cx="100" cy="100"/></a:xfrm></p:spPr></p:sp>'
    )[0]
    assert L._xfrm(shape)[4] == pytest.approx(45.0)


def test_an_unplaced_shape_is_reported_as_unplaced() -> None:
    assert L._xfrm(_el("<p:sp><p:spPr/></p:sp>")[0]) is None


# --------------------------------------------------------------------------- #
# Connectors — the arrows a review deck points with
# --------------------------------------------------------------------------- #


def test_a_flipped_connector_runs_the_other_way() -> None:
    """Rule: `flipH`/`flipV` decide the direction, and direction is the content.

    Ignore them and every arrow in a review deck points the same way, which is
    worse than drawing no arrows — it asserts something false.
    """
    plain = _el('<p:cxnSp><p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="100" cy="100"/>'
                "</a:xfrm></p:spPr></p:cxnSp>")[0]
    flipped = _el('<p:cxnSp><p:spPr><a:xfrm flipV="1"><a:off x="0" y="0"/>'
                  '<a:ext cx="100" cy="100"/></a:xfrm></p:spPr></p:cxnSp>')[0]

    assert 'y1="0.00"' in L._connector(plain, 10, 10, {})
    assert 'y1="10.00"' in L._connector(flipped, 10, 10, {}), "flipV must swap the endpoints"


def test_an_arrowhead_is_drawn_only_when_the_line_has_one() -> None:
    with_head = _el('<p:cxnSp><p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="1" cy="1"/></a:xfrm>'
                    '<a:ln><a:tailEnd type="triangle"/></a:ln></p:spPr></p:cxnSp>')[0]
    without = _el('<p:cxnSp><p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="1" cy="1"/></a:xfrm>'
                  "</p:spPr></p:cxnSp>")[0]
    assert "marker-end" in L._connector(with_head, 5, 5, {})
    assert "marker-end" not in L._connector(without, 5, 5, {})


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #


def test_run_properties_survive() -> None:
    shape = _el(
        '<p:sp><p:txBody><a:p><a:pPr algn="ctr"/><a:r>'
        '<a:rPr sz="2400" b="1" i="1"><a:solidFill><a:srgbClr val="FF0000"/></a:solidFill></a:rPr>'
        "<a:t>Cambiar</a:t></a:r></a:p></p:txBody></p:sp>"
    )[0]
    out = L._text_html(shape, {}, 1.0)
    assert "font-size:24.00pt" in out and "font-weight:700" in out
    assert "font-style:italic" in out and "color:#FF0000" in out
    assert "text-align:center" in out


def test_autofit_shrink_is_applied() -> None:
    """Rule: `normAutofit` records the shrink PowerPoint already applied.

    Ignoring it overflows every box that was ever auto-shrunk — the text spills
    out of its shape and lands on top of the next one.
    """
    shape = _el(
        '<p:sp><p:txBody><a:bodyPr><a:normAutofit fontScale="50000"/></a:bodyPr>'
        "<a:p><a:r><a:t>x</a:t></a:r></a:p></p:txBody></p:sp>"
    )[0]
    assert f"font-size:{L.DEFAULT_TEXT_PT / 2:.2f}pt" in L._text_html(shape, {}, 1.0)


def test_a_shape_with_no_text_body_yields_nothing() -> None:
    assert L._text_html(_el("<p:sp><p:spPr/></p:sp>")[0], {}, 1.0) == ""


# --------------------------------------------------------------------------- #
# Groups and the unsupported
# --------------------------------------------------------------------------- #


def test_a_group_applies_its_child_space_transform() -> None:
    """Rule: a group states where it sits *and* the space its children use.

    `chOff`/`chExt` is the second half. Ignoring it places every grouped shape at
    the wrong scale and offset — and groups are how several shapes become one, so
    getting this wrong misplaces whole compositions rather than single elements.
    """
    tree = _el(
        '<p:grpSp><p:grpSpPr><a:xfrm><a:off x="914400" y="0"/><a:ext cx="914400" cy="914400"/>'
        '<a:chOff x="0" y="0"/><a:chExt cx="1828800" cy="1828800"/></a:xfrm></p:grpSpPr>'
        '<p:sp><p:spPr><a:xfrm><a:off x="914400" y="914400"/><a:ext cx="914400" cy="914400"/>'
        '</a:xfrm><a:solidFill><a:srgbClr val="FF0000"/></a:solidFill></p:spPr></p:sp>'
        "</p:grpSp>"
    )
    boxes, _ = L._walk(tree, {}, {}, {})

    assert len(boxes) == 1
    box = boxes[0]
    # child is at half the group's child-space, so half the group's real extent
    assert box.x == pytest.approx(72.0 + 36.0), "group offset plus scaled child offset"
    assert box.w == pytest.approx(36.0), "child extent halved by chExt/ext"


def test_an_unsupported_shape_is_drawn_and_named_not_dropped() -> None:
    """Rule: no silent loss.

    A chart that vanishes leaves a plausible-looking slide with a hole in it, and
    nothing says a hole is there. An outline labelled 'table / chart / SmartArt'
    is honest about both the space and the limitation.
    """
    tree = _el(
        '<p:graphicFrame><p:xfrm><a:off x="0" y="0"/><a:ext cx="914400" cy="914400"/></p:xfrm>'
        "</p:graphicFrame>"
    )
    boxes, skipped = L._walk(tree, {}, {}, {})
    assert skipped == ["table / chart / SmartArt"]
    assert boxes and "table / chart / SmartArt" in boxes[0].html


def test_a_picture_without_a_resolvable_image_is_skipped_rather_than_broken() -> None:
    """Rule: a dangling relationship must not become a broken-image icon."""
    tree = _el(
        '<p:pic><p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="1" cy="1"/></a:xfrm></p:spPr>'
        '<p:blipFill><a:blip r:embed="rIdMissing"/></p:blipFill></p:pic>'
    )
    boxes, _ = L._walk(tree, {}, {}, {})
    assert boxes == []


def test_z_order_follows_document_order() -> None:
    """Rule: later shapes paint over earlier ones, which is what the deck means."""
    tree = _el(
        '<p:sp><p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="1" cy="1"/></a:xfrm>'
        '<a:solidFill><a:srgbClr val="111111"/></a:solidFill></p:spPr></p:sp>'
        '<p:sp><p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="1" cy="1"/></a:xfrm>'
        '<a:solidFill><a:srgbClr val="222222"/></a:solidFill></p:spPr></p:sp>'
    )
    boxes, _ = L._walk(tree, {}, {}, {})
    assert [b.z for b in boxes] == [0, 1]
