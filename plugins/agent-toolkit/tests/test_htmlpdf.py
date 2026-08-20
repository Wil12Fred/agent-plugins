"""Printing HTML to PDF with a headless browser.

The browser is faked in every test but the last: what is checked here is the
logic around the call, which is where the defects are. A wrong subprocess
argument fails loudly; a PDF that came out on the wrong paper, or with its
images missing, does not.

Each test names the rule it enforces.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentctl import htmlpdf
from agentctl.errors import ApiError, ConfigError, NotFoundError, UsageError, ValidationError


@pytest.fixture
def page(tmp_path: Path) -> Path:
    doc = tmp_path / "doc" / "report.html"
    doc.parent.mkdir()
    (doc.parent / "images").mkdir()
    (doc.parent / "images" / "a.png").write_bytes(b"\x89PNG")
    doc.write_text('<h1>Report</h1><img src="images/a.png">', encoding="utf-8")
    return doc


def _fake_browser(monkeypatch, *, writes: bytes | None = b"%PDF-1.4\nx", record=None):
    monkeypatch.setattr(
        htmlpdf.shutil, "which", lambda n: "/usr/bin/chromium" if n == "chromium" else None
    )

    def run(cmd, **kwargs):
        if record is not None:
            record.append(cmd)
        target = next(a for a in cmd if a.startswith("--print-to-pdf="))
        if writes is not None:
            Path(target.split("=", 1)[1]).write_bytes(writes)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(htmlpdf.subprocess, "run", run)


# --------------------------------------------------------------------------- #
# Finding the engine
# --------------------------------------------------------------------------- #


def test_no_browser_names_the_variable_that_fixes_it(monkeypatch, page, tmp_path) -> None:
    """Rule: refuse with the fix, not just the fact."""
    monkeypatch.setattr(htmlpdf.shutil, "which", lambda n: None)
    monkeypatch.setattr(htmlpdf, "PLAYWRIGHT_CACHES", ())
    monkeypatch.delenv(htmlpdf.BROWSER_ENV, raising=False)
    with pytest.raises(ConfigError, match="CHROME_BINARY"):
        htmlpdf.render(page, tmp_path / "o.pdf")


def test_the_env_override_is_checked_rather_than_trusted(monkeypatch, page, tmp_path) -> None:
    """Rule: a variable pointing at nothing must not become a confusing exec error."""
    monkeypatch.setenv(htmlpdf.BROWSER_ENV, "/nope/chrome")
    with pytest.raises(ConfigError, match="points at nothing"):
        htmlpdf.render(page, tmp_path / "o.pdf")


def test_the_engine_that_was_used_is_reported(monkeypatch, page, tmp_path) -> None:
    """Rule: never choose an engine silently.

    A browser picked up from a cache directory nobody mentioned makes the output
    depend on an invisible input. Naming it is what makes that acceptable.
    """
    _fake_browser(monkeypatch)
    monkeypatch.delenv(htmlpdf.BROWSER_ENV, raising=False)
    result = htmlpdf.render(page, tmp_path / "o.pdf")
    assert result["renderer"] == "chromium"
    assert result["found_via"] == "PATH"


# --------------------------------------------------------------------------- #
# The page rule
# --------------------------------------------------------------------------- #


def test_paper_size_becomes_an_at_page_rule() -> None:
    """Rule: `--print-to-pdf` has no paper flag; the size lives in CSS.

    Without this the document silently comes out US Letter wherever you are.
    """
    assert "210mm 297mm" in htmlpdf.page_rule("A4", None)
    assert "12mm" in htmlpdf.page_rule(None, "12mm")


def test_no_rule_is_emitted_when_nothing_was_asked() -> None:
    """The control: an empty rule is what keeps the page's own @page intact."""
    assert htmlpdf.page_rule(None, None) == ""


def test_an_unknown_paper_size_is_refused_with_the_list() -> None:
    with pytest.raises(UsageError, match="A4"):
        htmlpdf.page_rule("Foolscap", None)


def test_a_margin_that_is_not_a_length_is_refused() -> None:
    """Rule: garbage in an injected stylesheet is silently ignored by the browser.

    `--margin big` would produce a normal-looking PDF with the default margin,
    and nothing would say the flag did nothing.
    """
    with pytest.raises(UsageError, match="not a CSS length"):
        htmlpdf.page_rule(None, "big")


def test_the_temp_file_is_written_beside_the_original(monkeypatch, page, tmp_path) -> None:
    """Rule: relative asset paths resolve against the document's own directory.

    Injecting the rule into a copy in /tmp loses every `<img src="images/…">`,
    and the PDF comes out with the text and none of the pictures — which reads as
    a broken document rather than a wrong working directory.
    """
    seen: list[list[str]] = []
    _fake_browser(monkeypatch, record=seen)
    htmlpdf.render(page, tmp_path / "o.pdf", paper="A4")

    url = seen[0][-1]
    assert url.startswith(page.parent.resolve().as_uri()), url
    assert not list(page.parent.glob(".agentctl-print-*")), "the temp file must be cleaned up"


def test_the_original_is_never_modified(monkeypatch, page, tmp_path) -> None:
    before = page.read_bytes()
    _fake_browser(monkeypatch)
    htmlpdf.render(page, tmp_path / "o.pdf", paper="A4")
    assert page.read_bytes() == before


# --------------------------------------------------------------------------- #
# Trusting the result
# --------------------------------------------------------------------------- #


def test_the_url_and_date_stamp_is_disabled(monkeypatch, page, tmp_path) -> None:
    """Rule: default Chromium stamps every page, and it looks like a web printout."""
    seen: list[list[str]] = []
    _fake_browser(monkeypatch, record=seen)
    htmlpdf.render(page, tmp_path / "o.pdf")
    assert "--no-pdf-header-footer" in seen[0]


def test_no_output_is_a_failure_even_at_exit_zero(monkeypatch, page, tmp_path) -> None:
    """Rule: verify the artefact, not the return code."""
    _fake_browser(monkeypatch, writes=None)
    with pytest.raises(ValidationError, match="produced no PDF"):
        htmlpdf.render(page, tmp_path / "o.pdf")


def test_something_that_is_not_a_pdf_is_refused_and_removed(monkeypatch, page, tmp_path) -> None:
    """Rule: a `.pdf` suffix is a claim; `%PDF-` is the evidence.

    And the bad file is deleted — leaving it behind means the next reader opens
    a broken PDF instead of seeing the error.
    """
    _fake_browser(monkeypatch, writes=b"<html>crash</html>")
    out = tmp_path / "o.pdf"
    with pytest.raises(ValidationError, match="not a PDF"):
        htmlpdf.render(page, out)
    assert not out.exists()


def test_a_timeout_says_it_timed_out(monkeypatch, page, tmp_path) -> None:
    monkeypatch.setattr(htmlpdf.shutil, "which", lambda n: "/usr/bin/chromium")

    def run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(htmlpdf.subprocess, "run", run)
    with pytest.raises(ApiError, match="did not finish"):
        htmlpdf.render(page, tmp_path / "o.pdf", timeout=1)


def test_a_missing_source_is_refused(tmp_path: Path) -> None:
    with pytest.raises(NotFoundError):
        htmlpdf.render(tmp_path / "nope.html", tmp_path / "o.pdf")


def test_an_existing_output_is_refused_unless_overwrite(monkeypatch, page, tmp_path) -> None:
    _fake_browser(monkeypatch)
    out = tmp_path / "o.pdf"
    out.write_bytes(b"previous")
    with pytest.raises(UsageError, match="already exists"):
        htmlpdf.render(page, out)
    assert htmlpdf.render(page, out, overwrite=True)["pages"] == 0


def test_the_page_count_ignores_the_tree_node(tmp_path: Path) -> None:
    """Rule: `/Type /Pages` is the tree, `/Type /Page` is a leaf."""
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n/Type /Pages /Count 2\n/Type /Page\n/Type /Page\n")
    assert htmlpdf.count_pages(pdf) == 2


@pytest.mark.integration
def test_a_real_browser_produces_a_real_pdf(page: Path, tmp_path: Path) -> None:
    """The one test that runs the engine. Skips with a reason when there is none.

    Everything above fakes the subprocess, so nothing above can tell whether the
    arguments are the ones this browser actually accepts — the flags are the part
    a fake cannot check. A red that only means "no browser here" teaches people
    to ignore red, so this skips rather than fails.
    """
    try:
        binary, _ = htmlpdf.find_browser()
    except ConfigError as exc:
        pytest.skip(f"no browser on this machine: {exc.message}")

    out = tmp_path / "real.pdf"
    result = htmlpdf.render(page, out, paper="A4", margin="12mm")

    assert out.read_bytes()[:5] == b"%PDF-"
    assert result["pages"] >= 1
    assert Path(binary).name == result["renderer"]
