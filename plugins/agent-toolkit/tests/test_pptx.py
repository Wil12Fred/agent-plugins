"""Reading and editing a `.pptx` without a rendering engine.

Every case here runs against a real zip built by :func:`build_deck` — the parts,
the relationships and the namespaces PowerPoint writes — rather than against a
mock, because all three bugs this module is shaped around are bugs about the
*format*: a part name that is not a position, a media directory that is not the
slides' artwork, and a sentence that is not a run. A fake would have agreed with
whatever the code assumed.

Each test names the rule it enforces.
"""

from __future__ import annotations

import csv
import re
import zipfile
from pathlib import Path

import pytest

from agentctl import pdfassets, pptxdeck
from agentctl.errors import NotFoundError, UsageError, ValidationError

NS_P = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
NS_A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
NS_R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
NS_PKG = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'

PNG = bytes.fromhex("89504e470d0a1a0a") + b"fake-png-body"


def _paragraph(runs: list[str]) -> str:
    """One `<a:p>` whose text is split across `runs`.

    Splitting is the point: PowerPoint does it constantly, and a fixture with one
    run per paragraph would let a per-run search pass every test here.
    """
    body = "".join(f"<a:r><a:rPr/><a:t>{run}</a:t></a:r>" for run in runs)
    return f"<a:p>{body}</a:p>"


def _slide_xml(paragraphs: list[list[str]], images: list[str]) -> str:
    text = "".join(_paragraph(runs) for runs in paragraphs)
    pics = "".join(
        f'<p:pic><p:blipFill><a:blip r:embed="{rid}"/></p:blipFill></p:pic>' for rid in images
    )
    return (
        f"<?xml version='1.0' encoding='UTF-8'?><p:sld {NS_P} {NS_A} {NS_R}>"
        f"<p:cSld><p:spTree><p:sp><p:txBody>{text}</p:txBody></p:sp>{pics}</p:spTree></p:cSld>"
        "</p:sld>"
    )


REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _rels(entries: list[tuple[str, str, str]]) -> str:
    body = "".join(
        f'<Relationship Id="{rid}" Type="{REL_TYPE}/{kind}" Target="{target}"/>'
        for rid, kind, target in entries
    )
    return f"<?xml version='1.0' encoding='UTF-8'?>{NS_PKG}{body}</Relationships>"


def build_deck(
    path: Path,
    *,
    order: list[int] | None = None,
    notes: str | None = None,
) -> Path:
    """A three-slide deck whose part names deliberately disagree with its order.

    `slide1` carries text split across runs and an image; `slide2` carries a
    second image; `slide3` carries neither. A fourth image is referenced only by
    the layout, and a fifth by nothing at all — the two cases a flat listing of
    `ppt/media/` cannot tell apart from artwork on a slide.
    """
    order = order or [3, 1, 2]
    with zipfile.ZipFile(path, "w") as deck:
        deck.writestr(
            "[Content_Types].xml",
            "<?xml version='1.0'?><Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\"/>",
        )
        sld_ids = "".join(
            f'<p:sldId id="{255 + position}" r:id="rId{number}"/>'
            for position, number in enumerate(order, start=1)
        )
        deck.writestr(
            "ppt/presentation.xml",
            f"<?xml version='1.0'?><p:presentation {NS_P} {NS_R}>"
            f"<p:sldIdLst>{sld_ids}</p:sldIdLst></p:presentation>",
        )
        deck.writestr(
            "ppt/_rels/presentation.xml.rels",
            _rels([(f"rId{n}", "slide", f"slides/slide{n}.xml") for n in (1, 2, 3)]),
        )

        deck.writestr(
            "ppt/slides/slide1.xml",
            _slide_xml([["Tot", "al: ", "42"], ["Second line"]], ["rId9"]),
        )
        slide1_rels = [("rId9", "image", "../media/image1.png")]
        if notes is not None:
            slide1_rels.append(("rId8", "notesSlide", "../notesSlides/notesSlide1.xml"))
            deck.writestr(
                "ppt/notesSlides/notesSlide1.xml",
                f"<?xml version='1.0'?><p:notes {NS_P} {NS_A}>{_paragraph([notes])}</p:notes>",
            )
        deck.writestr("ppt/slides/_rels/slide1.xml.rels", _rels(slide1_rels))

        deck.writestr("ppt/slides/slide2.xml", _slide_xml([["Slide two"]], ["rId9"]))
        deck.writestr(
            "ppt/slides/_rels/slide2.xml.rels", _rels([("rId9", "image", "../media/image2.png")])
        )
        deck.writestr("ppt/slides/slide3.xml", _slide_xml([["Slide three"]], []))

        deck.writestr("ppt/slideLayouts/slideLayout1.xml", "<x/>")
        deck.writestr(
            "ppt/slideLayouts/_rels/slideLayout1.xml.rels",
            _rels([("rId1", "image", "../media/logo.png")]),
        )

        for name in ("image1.png", "image2.png", "logo.png", "orphan.png"):
            deck.writestr(f"ppt/media/{name}", PNG + name.encode())
        # A deck's media directory is not only images. Slide 3 embeds a video,
        # which is what made a real run appear to hang.
        deck.writestr("ppt/media/media1.mp4", b"\x00\x00\x00\x18ftypmp42" + b"x" * 64)
        deck.writestr(
            "ppt/slides/_rels/slide3.xml.rels", _rels([("rId9", "video", "../media/media1.mp4")])
        )
    return path


@pytest.fixture
def deck(tmp_path: Path) -> Path:
    return build_deck(tmp_path / "deck.pptx")


# --------------------------------------------------------------------------- #
# Opening
# --------------------------------------------------------------------------- #


def test_a_binary_ppt_is_refused_as_a_format_not_as_corruption(tmp_path: Path) -> None:
    """Rule: name the real failure.

    A pre-2007 `.ppt` renamed to `.pptx` is the common mistake, and `BadZipFile`
    on its own reads as a truncated download — sending the reader to re-download
    a file that will fail again.
    """
    fake = tmp_path / "old.pptx"
    fake.write_bytes(b"\xd0\xcf\x11\xe0not a zip")
    with pytest.raises(ValidationError, match="Office Open XML"):
        pptxdeck.inspect(fake)


def test_a_docx_is_refused_even_though_it_is_a_valid_zip(tmp_path: Path) -> None:
    """Rule: a zip with the right shape is not a presentation.

    The control for the test above: that one fails at `zipfile`, this one gets
    past it and must still be refused, so "is it a zip" cannot be the whole check.
    """
    other = tmp_path / "doc.pptx"
    with zipfile.ZipFile(other, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<w/>")
    with pytest.raises(ValidationError, match="not a presentation"):
        pptxdeck.inspect(other)


def test_a_real_deck_is_accepted(deck: Path) -> None:
    """The control: without it, 'refuse everything' passes both tests above."""
    assert len(pptxdeck.inspect(deck).slides) == 3


# --------------------------------------------------------------------------- #
# Order
# --------------------------------------------------------------------------- #


def test_slide_order_comes_from_the_presentation_not_the_part_names(deck: Path) -> None:
    """Rule: `slide7.xml` is not the seventh slide.

    Part names are assigned at creation and never renumbered, so a reordered
    deck disagrees with itself. The fixture presents 3, 1, 2 on purpose; sorting
    the filenames would give 1, 2, 3 and every downstream index would be wrong.
    """
    parts = [slide.part for slide in pptxdeck.inspect(deck).slides]
    assert parts == [
        "ppt/slides/slide3.xml",
        "ppt/slides/slide1.xml",
        "ppt/slides/slide2.xml",
    ]


def test_positions_are_one_based_and_contiguous(deck: Path) -> None:
    assert [slide.index for slide in pptxdeck.inspect(deck).slides] == [1, 2, 3]


def test_order_falls_back_to_part_numbers_when_the_list_is_missing(tmp_path: Path) -> None:
    """Rule: a hand-assembled archive still reads, in a stated order.

    Only reachable when nothing PowerPoint wrote produced the file — which is
    exactly the archive somebody built with a script, and refusing it would make
    this tool unusable on its own output.
    """
    path = tmp_path / "flat.pptx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("ppt/presentation.xml", f"<p:presentation {NS_P} {NS_R}/>")
        for number in (2, 10, 1):
            archive.writestr(f"ppt/slides/slide{number}.xml", _slide_xml([[f"s{number}"]], []))
    parts = [slide.part for slide in pptxdeck.inspect(path).slides]
    assert parts == [
        "ppt/slides/slide1.xml",
        "ppt/slides/slide2.xml",
        "ppt/slides/slide10.xml",
    ], "slide10 must not sort before slide2"


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #


def test_a_paragraph_split_across_runs_reads_as_one_line(deck: Path) -> None:
    """Rule: a run boundary is a formatting artefact, not a word boundary.

    The fixture stores "Total: 42" as three runs, which is what PowerPoint does.
    Reporting them separately turns one sentence into three fragments and makes
    the output unsearchable — the same defect that makes a per-run find/replace
    miss visible text.
    """
    first = pptxdeck.inspect(deck).slides[1]
    assert "Total: 42" in first.text


def test_empty_paragraphs_are_dropped(deck: Path) -> None:
    assert all(line.strip() for slide in pptxdeck.inspect(deck).slides for line in slide.text)


def test_speaker_notes_are_read_from_the_slides_own_relationship(tmp_path: Path) -> None:
    """Rule: notes belong to the slide that links them, not to a numbering rule.

    `notesSlide4.xml` can belong to any slide; only the relationship says which.
    """
    path = build_deck(tmp_path / "noted.pptx", notes="remember the budget")
    slides = {slide.part: slide for slide in pptxdeck.inspect(path).slides}
    assert slides["ppt/slides/slide1.xml"].notes == ("remember the budget",)
    assert slides["ppt/slides/slide2.xml"].notes == (), "notes must not leak between slides"


# --------------------------------------------------------------------------- #
# Media
# --------------------------------------------------------------------------- #


def test_media_is_split_into_slides_chrome_and_unreferenced(deck: Path) -> None:
    """Rule: `ppt/media/` is not the slides' artwork.

    It holds every part's artwork. A logo referenced by the layout appears on
    every slide and in none of their relationships, so per-slide attribution
    under-reports it and a flat listing cannot tell it from a leftover.
    """
    result = pptxdeck.inspect(deck)
    assert set(result.slide_media) == {
        "ppt/media/image1.png",
        "ppt/media/image2.png",
        "ppt/media/media1.mp4",
    }
    assert result.chrome_media == ("ppt/media/logo.png",)
    assert result.unreferenced_media == ("ppt/media/orphan.png",)


def test_extraction_copies_bytes_rather_than_re_encoding(deck: Path, tmp_path: Path) -> None:
    """Rule: extraction must not resample.

    The whole reason to lift the embedded file instead of rendering the slide is
    that the embedded file is the original. A byte comparison is the only check
    that distinguishes the two.
    """
    rows = pptxdeck.extract_media(deck, tmp_path / "out", prefix="asset")
    written = {
        row["extracted_from"]: (tmp_path / "out" / str(row["file"])).read_bytes() for row in rows
    }
    assert written["ppt/media/image1.png"] == PNG + b"image1.png"


def test_the_manifest_records_which_slides_use_each_file(deck: Path, tmp_path: Path) -> None:
    """Rule: the output is renumbered, so the archive name is the only way back.

    `replace-image` addresses a part by that name and by nothing else.
    """
    rows = pptxdeck.extract_media(deck, tmp_path / "out")
    by_source = {row["extracted_from"]: row for row in rows}
    assert by_source["ppt/media/image1.png"]["slides"] == "2"
    assert (tmp_path / "out" / "manifest.csv").is_file()


def test_a_video_is_extracted_but_never_measured(
    deck: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule: only rasters go to ImageMagick.

    Found by running the tool on a real deck rather than by reading it. A 93 MB
    embedded mp4 handed to `identify` makes ImageMagick decode the video; the
    extraction did not finish in two minutes and looked like a hang, not like a
    wrong answer. The video must still come out — it is one of the deck's
    assets — with its dimensions reported as unknown.
    """
    measured: list[Path] = []

    def spy(path: Path, **kwargs: object) -> None:
        measured.append(path)
        return None

    monkeypatch.setattr(pptxdeck, "identify", spy)
    rows = pptxdeck.extract_media(deck, tmp_path / "out")

    video = next(row for row in rows if row["extracted_from"] == "ppt/media/media1.mp4")
    assert video["width"] == 0 and video["height"] == 0
    assert not any(path.suffix == ".mp4" for path in measured), "identify must not see a video"
    assert any(path.suffix == ".png" for path in measured), "but it must still see the images"


def test_an_unreferenced_file_is_not_extracted(deck: Path, tmp_path: Path) -> None:
    """Rule: extract the artwork, not the archive's leftovers.

    A media part nothing refers to is a deleted slide's image that the file
    format kept. Extracting it puts a picture that appears nowhere in the deck
    into a folder somebody is about to treat as the deck's contents — and the
    manifest cannot flag it, because its slide column is empty either way.

    The layout's logo is the control: unreferenced-by-a-slide is not the same
    condition, and it must still come out.
    """
    rows = pptxdeck.extract_media(deck, tmp_path / "out")
    sources = {row["extracted_from"] for row in rows}
    assert "ppt/media/orphan.png" not in sources
    assert "ppt/media/logo.png" in sources


def test_filtering_by_slide_excludes_the_master_artwork(deck: Path, tmp_path: Path) -> None:
    """Rule: a run scoped to one slide must not include what sits on all of them.

    The opposite behaviour is defensible and wrong for the use it exists for:
    someone extracting slide 4 wants slide 4, not the logo forty times.
    """
    rows = pptxdeck.extract_media(deck, tmp_path / "out", slides=[2])
    assert [row["extracted_from"] for row in rows] == ["ppt/media/image1.png"]


def test_asking_for_a_slide_that_does_not_exist_is_refused(deck: Path, tmp_path: Path) -> None:
    """Rule: refuse rather than return an empty answer.

    An out-of-range slide silently extracting nothing reads exactly like a slide
    that genuinely has no images.
    """
    with pytest.raises(UsageError, match="no such slide"):
        pptxdeck.extract_media(deck, tmp_path / "out", slides=[9])


def test_a_deck_with_no_matching_media_is_refused(deck: Path, tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="no media extracted"):
        pptxdeck.extract_media(deck, tmp_path / "out", min_bytes=10_000)


# --------------------------------------------------------------------------- #
# Replacing text
# --------------------------------------------------------------------------- #


def test_a_match_spanning_several_runs_is_replaced(deck: Path, tmp_path: Path) -> None:
    """Rule: match the paragraph, not the run.

    "Total: 42" is three runs in the fixture and in most real decks. This is the
    case a per-run implementation fails while looking correct, because the text
    is plainly on the slide.
    """
    out = tmp_path / "new.pptx"
    applied = pptxdeck.replace_text(deck, out, [("Total: 42", "Total: 43")])

    assert [item.after for item in applied] == ["Total: 43"]
    assert "Total: 43" in pptxdeck.inspect(out).slides[1].text


def test_the_report_says_when_formatting_was_collapsed(deck: Path, tmp_path: Path) -> None:
    """Rule: name the cost of the edit at the time it is paid.

    Joining a paragraph to match it means writing the result into its first run,
    so anything bold in the middle stops being bold. Discovering that when the
    deck is opened is too late to know which edit caused it.
    """
    applied = pptxdeck.replace_text(deck, tmp_path / "new.pptx", [("Total: 42", "Total: 43")])
    assert applied[0].collapsed_runs == 3


def test_a_match_inside_one_run_does_not_collapse_the_paragraph(
    deck: Path, tmp_path: Path
) -> None:
    """Rule: collapse on the *match*, not on the paragraph's run count.

    Found by running the tool on a real 27-slide deck: all five edits sat inside
    a single run of a paragraph that had several, so deciding by run count
    flattened five paragraphs' formatting for no reason. The fixture stores
    "Total: 42" as `Tot` / `al: ` / `42`; replacing `Tot` needs no joining.
    """
    out = tmp_path / "new.pptx"
    applied = pptxdeck.replace_text(deck, out, [("Tot", "Sub")])

    assert applied[0].collapsed_runs == 0, "no run boundary was crossed"
    assert "Subal: 42" in pptxdeck.inspect(out).slides[1].text


def test_the_runs_really_do_survive_that_edit(deck: Path, tmp_path: Path) -> None:
    """The control for the test above: `collapsed_runs == 0` must be a fact.

    Reporting zero while having merged the runs anyway is the failure this pair
    exists to catch — the caller believes formatting is intact and it is not.
    """
    out = tmp_path / "new.pptx"
    pptxdeck.replace_text(deck, out, [("Tot", "Sub")])
    with zipfile.ZipFile(out) as archive:
        xml = archive.read("ppt/slides/slide1.xml").decode()
    assert xml.count("<a:t>") == 4, "three runs in the edited paragraph, one in the next"


def test_keep_runs_leaves_a_split_match_alone(deck: Path, tmp_path: Path) -> None:
    """Rule: `--keep-runs` trades reach for formatting, and must actually refuse.

    A flag that promises to preserve formatting and then collapses anyway is
    worse than no flag: the caller believes the deck is intact.
    """
    with pytest.raises(ValidationError, match="no text matched"):
        pptxdeck.replace_text(
            deck, tmp_path / "new.pptx", [("Total: 42", "Total: 43")], keep_runs=True
        )


def test_keep_runs_still_replaces_within_a_single_run(deck: Path, tmp_path: Path) -> None:
    """The control for the test above: `keep_runs` must not refuse everything."""
    out = tmp_path / "new.pptx"
    pptxdeck.replace_text(deck, out, [("Second line", "Third line")], keep_runs=True)
    assert "Third line" in pptxdeck.inspect(out).slides[1].text


def test_the_replacement_is_xml_escaped(deck: Path, tmp_path: Path) -> None:
    """Rule: replacement text is data, not markup.

    `Ben & Co` written raw closes nothing and produces a deck PowerPoint calls
    corrupt — and the tool would have reported success.
    """
    out = tmp_path / "new.pptx"
    pptxdeck.replace_text(deck, out, [("Slide two", "Ben & <Co>")])
    assert pptxdeck.inspect(out).slides[2].text == ("Ben & <Co>",)


def test_every_other_part_is_copied_byte_for_byte(deck: Path, tmp_path: Path) -> None:
    """Rule: an edit touches what it was asked to touch.

    A rewrite that re-serialises the whole archive changes parts nobody reviewed,
    and the damage shows up as an unrelated rendering bug later.
    """
    out = tmp_path / "new.pptx"
    pptxdeck.replace_text(deck, out, [("Slide two", "Slide 2")])
    with zipfile.ZipFile(deck) as before, zipfile.ZipFile(out) as after:
        assert before.namelist() == after.namelist()
        for name in before.namelist():
            if name != "ppt/slides/slide2.xml":
                assert before.read(name) == after.read(name), name


def test_editing_in_place_is_refused(deck: Path) -> None:
    """Rule: the source is the only copy of what the deck said before.

    A find/replace that matched the wrong thing is invisible until somebody
    opens the file, by which time the original is gone.
    """
    with pytest.raises(UsageError, match="the output is the source"):
        pptxdeck.replace_text(deck, deck, [("a", "b")])


def test_an_existing_output_is_refused_unless_overwrite(deck: Path, tmp_path: Path) -> None:
    out = tmp_path / "new.pptx"
    out.write_bytes(b"previous work")
    with pytest.raises(UsageError, match="already exists"):
        pptxdeck.replace_text(deck, out, [("Slide two", "Slide 2")])
    pptxdeck.replace_text(deck, out, [("Slide two", "Slide 2")], overwrite=True)


def test_matching_nothing_is_an_error_and_writes_no_file(deck: Path, tmp_path: Path) -> None:
    """Rule: a no-op edit must not leave a file that looks edited.

    An output identical to the input is indistinguishable from a successful run,
    and the typo in the search string is never found.
    """
    out = tmp_path / "new.pptx"
    with pytest.raises(ValidationError, match="no text matched"):
        pptxdeck.replace_text(deck, out, [("absent", "present")])
    assert not out.exists()


def test_replacing_with_no_pairs_is_refused(deck: Path, tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="nothing to replace"):
        pptxdeck.replace_text(deck, tmp_path / "new.pptx", [])


# --------------------------------------------------------------------------- #
# Replacing an image
# --------------------------------------------------------------------------- #


def test_an_image_is_swapped_and_the_rest_is_untouched(deck: Path, tmp_path: Path) -> None:
    replacement = tmp_path / "new.png"
    replacement.write_bytes(PNG + b"replacement")
    out = tmp_path / "out.pptx"

    pptxdeck.replace_media(deck, out, media="image1.png", image=replacement)

    with zipfile.ZipFile(out) as after, zipfile.ZipFile(deck) as before:
        assert after.read("ppt/media/image1.png") == PNG + b"replacement"
        assert after.read("ppt/media/image2.png") == before.read("ppt/media/image2.png")


def test_a_bare_name_and_a_full_part_path_both_work(deck: Path, tmp_path: Path) -> None:
    """Rule: accept both spellings the two output formats hand you.

    `inspect` reports `ppt/media/image1.png`; a person types `image1.png`.
    """
    replacement = tmp_path / "new.png"
    replacement.write_bytes(PNG)
    result = pptxdeck.replace_media(
        deck, tmp_path / "a.pptx", media="ppt/media/image1.png", image=replacement
    )
    assert result["replaced"] == "ppt/media/image1.png"


def test_a_different_extension_is_refused(deck: Path, tmp_path: Path) -> None:
    """Rule: the content type is declared per extension, not per part.

    A `.jpg` written over `image1.png` produces a deck PowerPoint calls corrupt,
    having accepted the write without complaint. The check has to be here
    because the format gives no feedback until the file is opened.
    """
    replacement = tmp_path / "new.jpg"
    replacement.write_bytes(b"jpeg")
    with pytest.raises(ValidationError, match="extension mismatch"):
        pptxdeck.replace_media(deck, tmp_path / "a.pptx", media="image1.png", image=replacement)


def test_a_file_with_no_extension_is_named_as_such(deck: Path, tmp_path: Path) -> None:
    """Rule: an empty value in an error message reads as a broken tool.

    `extension mismatch:  cannot replace .png` looks like the tool lost the
    value, not like the file genuinely has no extension, and sends the reader
    to the wrong problem.
    """
    replacement = tmp_path / "noextension"
    replacement.write_bytes(PNG)
    with pytest.raises(ValidationError, match="no extension cannot replace"):
        pptxdeck.replace_media(deck, tmp_path / "a.pptx", media="image1.png", image=replacement)


def test_an_unknown_media_part_lists_what_is_there(deck: Path, tmp_path: Path) -> None:
    """Rule: an error that says what broke without saying what to do is half an error."""
    replacement = tmp_path / "new.png"
    replacement.write_bytes(PNG)
    with pytest.raises(NotFoundError, match="image1.png"):
        pptxdeck.replace_media(deck, tmp_path / "a.pptx", media="nope.png", image=replacement)


def test_a_missing_replacement_file_is_refused(deck: Path, tmp_path: Path) -> None:
    with pytest.raises(NotFoundError):
        pptxdeck.replace_media(
            deck, tmp_path / "a.pptx", media="image1.png", image=tmp_path / "absent.png"
        )


# --------------------------------------------------------------------------- #
# Exporting to a folder
# --------------------------------------------------------------------------- #


def test_the_export_writes_slides_in_display_order(deck: Path, tmp_path: Path) -> None:
    """Rule: the markdown is ordered by what the audience saw, not by part name.

    The fixture presents 3, 1, 2. An export numbered from the filenames would
    put the deck's own instructions in an order the client never sees, which is
    unreviewable against the deck it came from.
    """
    pptxdeck.export_folder(deck, tmp_path / "out")
    body = (tmp_path / "out" / "index.md").read_text(encoding="utf-8")

    assert body.index("Slide three") < body.index("Total: 42") < body.index("Slide two")


def test_an_image_is_referenced_from_every_slide_that_uses_it(
    deck: Path, tmp_path: Path
) -> None:
    """Rule: one file, referenced as many times as the deck references it.

    Writing a second copy per slide is the obvious implementation and the wrong
    one: a logo used on forty slides becomes forty files, and the folder stops
    being readable at the exact size where reading it matters.
    """
    pptxdeck.export_folder(deck, tmp_path / "out")
    body = (tmp_path / "out" / "index.md").read_text(encoding="utf-8")
    files = sorted(p.name for p in (tmp_path / "out" / "images").iterdir())

    assert len(files) == 3, f"one file per referenced image, got {files}"
    # Two are placed on slides; the layout's logo belongs to no slide and is
    # listed separately rather than written to disk and never mentioned.
    assert body.count("](images/") == 3
    assert "## Images not placed on any slide" in body


def test_video_is_left_out_unless_asked_for(deck: Path, tmp_path: Path) -> None:
    """Rule: a repository is not where a 94 MB clip goes, and silence is worse.

    The skip has to be reported. A folder that quietly lacks the deck's video
    reads as a deck that had none.
    """
    without = pptxdeck.export_folder(deck, tmp_path / "a")
    assert "ppt/media/media1.mp4" in without["skipped_media"]

    with_media = pptxdeck.export_folder(deck, tmp_path / "b", include_media=True)
    assert with_media["skipped_media"] == []
    assert (tmp_path / "b" / "images" / "deck_4.mp4").exists()


def test_speaker_notes_survive_the_export(tmp_path: Path) -> None:
    """Rule: the notes are where a deck hides its reasoning.

    In a review deck the slide says what to change and the note says why. An
    export that drops them loses the half that cannot be reconstructed.
    """
    path = build_deck(tmp_path / "noted.pptx", notes="remember the budget")
    pptxdeck.export_folder(path, tmp_path / "out")
    assert "remember the budget" in (tmp_path / "out" / "index.md").read_text(encoding="utf-8")


def test_a_non_empty_destination_is_refused(deck: Path, tmp_path: Path) -> None:
    """Rule: an export overwrites a folder, so it must not do it by accident."""
    out = tmp_path / "out"
    out.mkdir()
    (out / "notes.md").write_text("hand-written", encoding="utf-8")

    with pytest.raises(UsageError, match="not empty"):
        pptxdeck.export_folder(deck, out)
    pptxdeck.export_folder(deck, out, overwrite=True)


def test_an_empty_destination_is_fine(deck: Path, tmp_path: Path) -> None:
    """The control: refusing every existing directory would refuse `mkdir -p`."""
    out = tmp_path / "out"
    out.mkdir()
    assert pptxdeck.export_folder(deck, out)["slides"] == 3


def test_the_manifest_keeps_the_size_the_image_arrived_at(
    deck: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule: a downscale must say what it shrank, and from what.

    Without the original dimensions the manifest cannot answer the one question
    asked of it later — is this file the asset, or a preview of it?
    """
    monkeypatch.setattr(
        pptxdeck, "identify", lambda p, **k: pdfassets.ImageMeta(4000, 3000, "sRGB")
    )
    monkeypatch.setattr(pptxdeck, "_downscale", lambda p, w: (1600, 1200))

    pptxdeck.export_folder(deck, tmp_path / "out", max_width=1600)
    rows = list(csv.DictReader((tmp_path / "out" / "manifest.csv").open(encoding="utf-8")))

    assert rows[0]["width"] == "1600" and rows[0]["original_width"] == "4000"


def test_nothing_is_downscaled_when_no_bound_is_given(
    deck: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the test above: `--max-width 0` must not touch a file."""
    called: list[Path] = []
    monkeypatch.setattr(pptxdeck, "_downscale", lambda p, w: called.append(p))

    pptxdeck.export_folder(deck, tmp_path / "out")
    assert called == []


def test_quantizing_is_off_unless_asked_for(
    deck: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule: an export must not silently re-encode what it was handed.

    Quantizing is lossy. Doing it by default would mean a folder produced to
    *preserve* a deck quietly degrades it, and nothing in the output says so.
    """
    seen: list[int] = []
    monkeypatch.setattr(pptxdeck, "_quantize", lambda p, c: seen.append(c) or True)

    pptxdeck.export_folder(deck, tmp_path / "a")
    assert seen == []

    pptxdeck.export_folder(deck, tmp_path / "b", colors=256)
    assert seen == [256, 256, 256], "every image, and only images"


def test_quantizing_never_touches_a_video(
    deck: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule: the colour tools are for rasters.

    Same shape as the `identify` hang: handing ImageMagick a video makes it
    decode the video. Here the video is only present with --include-media, so
    the two options must not combine into a several-minute stall.
    """
    seen: list[Path] = []
    monkeypatch.setattr(pptxdeck, "_quantize", lambda p, c: seen.append(p) or True)

    pptxdeck.export_folder(deck, tmp_path / "out", colors=256, include_media=True)
    assert not any(p.suffix == ".mp4" for p in seen)
    assert any(p.suffix == ".png" for p in seen), "but the images were still done"


def test_images_can_live_outside_the_markdown_and_stay_linked(
    deck: Path, tmp_path: Path
) -> None:
    """Rule: splitting the two must not break the document.

    The markdown is the half worth committing; the images are the half that is
    recoverable and carries all the weight. The split is only useful if the
    links still resolve from where `index.md` sits, which means walking *up* out
    of its directory — `Path.relative_to` cannot express that, and reaching for
    it is how this silently produces an unusable file.
    """
    out = tmp_path / "spec" / "attachments" / "deck"
    elsewhere = tmp_path / "spec" / "refs" / "deck" / "images"
    pptxdeck.export_folder(deck, out, images_dir=elsewhere)

    body = (out / "index.md").read_text(encoding="utf-8")
    assert "](../../refs/deck/images/" in body, body[:400]
    assert not (out / "images").exists(), "nothing is written beside the markdown"
    assert len(list(elsewhere.iterdir())) == 3

    for link in re.findall(r"\]\(([^)]+)\)", body):
        assert (out / link).resolve().is_file(), f"dead link: {link}"


def test_the_default_still_puts_images_beside_the_markdown(
    deck: Path, tmp_path: Path
) -> None:
    """The control: the split must be opt-in, and the simple case stay simple."""
    out = tmp_path / "out"
    pptxdeck.export_folder(deck, out)
    body = (out / "index.md").read_text(encoding="utf-8")

    assert "](images/" in body
    for link in re.findall(r"\]\(([^)]+)\)", body):
        assert (out / link).resolve().is_file(), f"dead link: {link}"
