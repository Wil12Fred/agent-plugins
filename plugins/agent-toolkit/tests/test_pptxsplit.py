"""Cutting a deck down to the slides one task needs.

The cases here are the ones that produce a file which *looks* written and does
not open, or opens and shows the wrong thing. Both are only visible in
PowerPoint, which is exactly why they need a test.

Each test names the rule it enforces.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest

# The three-slide fixture, whose part names disagree with its display order on
# purpose — which is the property this module has to get right.
from test_pptx import build_deck

from agentctl import pptxdeck, pptxsplit
from agentctl.errors import UsageError


@pytest.fixture
def deck(tmp_path: Path) -> Path:
    return build_deck(tmp_path / "deck.pptx")


def test_a_range_expands_and_sorts() -> None:
    assert pptxsplit.parse_slides("5-7,1,3", 10) == [1, 3, 5, 6, 7]


def test_a_duplicate_is_collapsed() -> None:
    assert pptxsplit.parse_slides("2,2,2", 5) == [2]


def test_an_out_of_range_slide_is_refused_not_clipped() -> None:
    """Rule: clipping omits exactly the slide somebody meant to include.

    And nothing says so — the cut is produced, it is short, and the omission is
    found by whoever opens it looking for their instruction.
    """
    with pytest.raises(UsageError, match="no such slide"):
        pptxsplit.parse_slides("2,9", 3)


def test_a_backwards_range_is_refused(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="backwards"):
        pptxsplit.parse_slides("7-5", 10)


def test_nonsense_is_refused_with_an_example() -> None:
    with pytest.raises(UsageError, match="slide number or range"):
        pptxsplit.parse_slides("first two", 10)


def test_the_cut_keeps_display_order_not_part_names(deck: Path, tmp_path: Path) -> None:
    """Rule: the numbers are what a person reads off the deck.

    The fixture presents its slides 3, 1, 2. Asking for position 1 must give the
    slide the audience sees first — `slide3.xml` — not `slide1.xml`.
    """
    out = tmp_path / "cut.pptx"
    pptxsplit.split(deck, out, [1], overwrite=True)

    result = pptxdeck.inspect(out)
    assert len(result.slides) == 1
    assert result.slides[0].part == "ppt/slides/slide3.xml"


def test_the_presentation_no_longer_lists_the_dropped_slides(deck: Path, tmp_path: Path) -> None:
    """Rule: `<p:sldIdLst>` is the deck's table of contents.

    Left alone it still names every slide, and the file opens as corrupt — the
    zip is fine, so nothing before PowerPoint notices.
    """
    out = tmp_path / "cut.pptx"
    pptxsplit.split(deck, out, [1, 2], overwrite=True)
    with zipfile.ZipFile(out) as archive:
        xml = archive.read("ppt/presentation.xml").decode()
        rels = archive.read("ppt/_rels/presentation.xml.rels").decode()
    # `<p:sldIdLst>` contains the substring `<p:sldId`, so the naive count is
    # always one too high — which is a plausible-looking off-by-one.
    assert len(re.findall(r"<p:sldId\b(?!Lst)", xml)) == 2
    assert rels.count("slides/slide") == 2


def test_content_types_loses_the_dropped_overrides(deck: Path, tmp_path: Path) -> None:
    """Rule: an override for a part that is not in the package is invalid."""
    out = tmp_path / "cut.pptx"
    pptxsplit.split(deck, out, [1], overwrite=True)
    with zipfile.ZipFile(out) as archive:
        names = set(archive.namelist())
        types = archive.read("[Content_Types].xml").decode()
    for declared in ("slide1.xml", "slide2.xml", "slide3.xml"):
        if f"/ppt/slides/{declared}" in types:
            assert f"ppt/slides/{declared}" in names, f"{declared} declared but absent"


def test_media_nothing_references_is_dropped(deck: Path, tmp_path: Path) -> None:
    """Rule: the pruning is the point — it is where the size goes.

    The fixture's slide 2 (display) uses `image1.png`; `image2.png` belongs to
    another slide and must not survive the cut. On a real deck this is 223 MB
    against 1.9 MB.
    """
    out = tmp_path / "cut.pptx"
    pptxsplit.split(deck, out, [2], overwrite=True)
    with zipfile.ZipFile(out) as archive:
        media = {n for n in archive.namelist() if n.startswith("ppt/media/")}
    assert "ppt/media/image1.png" in media
    assert "ppt/media/image2.png" not in media


def test_the_layout_and_its_artwork_survive(deck: Path, tmp_path: Path) -> None:
    """The control for the test above.

    Pruning by 'is it on a kept slide' rather than by reachability throws away
    the layout, the master and the theme — and the result does not open at all.
    """
    out = tmp_path / "cut.pptx"
    pptxsplit.split(deck, out, [2], overwrite=True)
    with zipfile.ZipFile(out) as archive:
        names = archive.namelist()
    assert any("slideLayout" in n for n in names), "the layout is reachable and must stay"
    assert "ppt/media/logo.png" in names, "and so is the artwork it references"


def test_writing_over_the_source_is_refused(deck: Path) -> None:
    with pytest.raises(UsageError, match="the output is the source"):
        pptxsplit.split(deck, deck, [1])


def test_an_existing_output_is_refused_unless_overwrite(deck: Path, tmp_path: Path) -> None:
    out = tmp_path / "cut.pptx"
    out.write_bytes(b"previous")
    with pytest.raises(UsageError, match="already exists"):
        pptxsplit.split(deck, out, [1])
    assert pptxsplit.split(deck, out, [1], overwrite=True)["slides"] == [1]


def test_oversized_media_is_emptied_not_removed(tmp_path: Path) -> None:
    """The part survives with one byte in it.

    Deleting it would leave the slide's relationship pointing at nothing, and
    PowerPoint calls that corrupt — so the file would not open at all, which is
    a worse failure than the one being fixed.
    """
    deck = build_deck(tmp_path / "in.pptx")
    # The video, because that is the real case: an 89 MB mp4 on one slide is
    # what pushed three review packets past Drive's 100 MB conversion limit.
    heavy = _fatten(deck, "ppt/media/media1.mp4", 5_000_000)
    out = tmp_path / "out.pptx"

    result = pptxsplit.split(deck, out, [1, 2, 3], stub_media_over=1_000_000)

    with zipfile.ZipFile(out) as archive:
        assert heavy in archive.namelist(), "the part must stay, or the deck is corrupt"
        assert archive.read(heavy) == b"\0"
    assert [s["part"] for s in result["stubbed"]] == [heavy]
    assert out.stat().st_size < 1_000_000


def test_media_under_the_threshold_is_untouched(tmp_path: Path) -> None:
    """The control: without it, a stub-everything bug would pass the test above."""
    deck = build_deck(tmp_path / "in.pptx")
    with zipfile.ZipFile(deck) as archive:
        payload = archive.read("ppt/media/image1.png")
    out = tmp_path / "out.pptx"

    result = pptxsplit.split(deck, out, [1, 2, 3], stub_media_over=1_000_000)

    with zipfile.ZipFile(out) as archive:
        assert archive.read("ppt/media/image1.png") == payload
    assert result["stubbed"] == []


def _fatten(deck: Path, part: str, size: int) -> str:
    """Rewrite one media part with `size` bytes, leaving the rest of the zip alone."""
    with zipfile.ZipFile(deck) as archive:
        entries = [(i, archive.read(i.filename)) for i in archive.infolist()]
    with zipfile.ZipFile(deck, "w", zipfile.ZIP_DEFLATED) as archive:
        for info, data in entries:
            archive.writestr(info, b"z" * size if info.filename == part else data)
    return part
