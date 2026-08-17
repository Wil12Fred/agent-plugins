"""Markdown -> ADF: the comments in this repo are authored as markdown files."""

from __future__ import annotations

from typing import Any

from jiractl import adf


def blocks(markdown: str) -> list[dict[str, Any]]:
    return adf.from_markdown(markdown)["content"]


def first_text(node: dict[str, Any]) -> str:
    return "".join(child.get("text", "") for child in node.get("content", []))


def test_a_document_is_well_formed() -> None:
    document = adf.from_markdown("hello")
    assert document["type"] == "doc"
    assert document["version"] == 1


def test_headings_keep_their_level() -> None:
    result = blocks("# One\n\n### Three\n")
    assert [(b["type"], b["attrs"]["level"]) for b in result] == [("heading", 1), ("heading", 3)]


def test_heading_level_is_clamped() -> None:
    assert adf.heading("x", 99)["attrs"]["level"] == adf.MAX_HEADING_LEVEL


def test_paragraphs_are_joined_until_a_blank_line() -> None:
    result = blocks("line one\nline two\n\nsecond para")
    assert len(result) == 2
    assert first_text(result[0]) == "line one line two"


def test_inline_marks_are_applied() -> None:
    nodes = adf.inline_from_markdown("plain **bold** and `code` and *em*")
    marks = [n.get("marks", [{}])[0].get("type") for n in nodes]
    assert "strong" in marks
    assert "code" in marks
    assert "em" in marks


def test_links_carry_their_href() -> None:
    nodes = adf.inline_from_markdown("see [the MR](https://gitlab.com/x/-/merge_requests/1)")
    link = next(n for n in nodes if n.get("marks"))
    assert link["text"] == "the MR"
    assert link["marks"][0]["attrs"]["href"].endswith("/merge_requests/1")


def test_bare_urls_become_links() -> None:
    nodes = adf.inline_from_markdown("https://example.atlassian.net/browse/PROJ-123")
    assert nodes[0]["marks"][0]["type"] == "link"


def test_fenced_code_keeps_its_language_and_body() -> None:
    result = blocks("```sql\nSELECT 1;\nSELECT 2;\n```")
    assert result[0]["type"] == "codeBlock"
    assert result[0]["attrs"]["language"] == "sql"
    assert result[0]["content"][0]["text"] == "SELECT 1;\nSELECT 2;"


def test_markdown_inside_a_code_fence_is_not_interpreted() -> None:
    result = blocks("```\n# not a heading\n**not bold**\n```")
    assert len(result) == 1
    assert result[0]["type"] == "codeBlock"


def test_bullet_and_ordered_lists_are_distinguished() -> None:
    assert blocks("- a\n- b")[0]["type"] == "bulletList"
    assert blocks("1. a\n2. b")[0]["type"] == "orderedList"


def test_list_items_keep_inline_formatting() -> None:
    result = blocks("- item with **bold**")
    item = result[0]["content"][0]["content"][0]
    assert any(n.get("marks") for n in item["content"])


def test_tables_become_adf_tables_with_a_header_row() -> None:
    result = blocks("| a | b |\n|---|---|\n| 1 | 2 |\n")
    assert result[0]["type"] == "table"
    rows = result[0]["content"]
    assert rows[0]["content"][0]["type"] == "tableHeader"
    assert rows[1]["content"][0]["type"] == "tableCell"


def test_horizontal_rules_and_blockquotes() -> None:
    result = blocks("---\n\n> quoted line\n")
    assert result[0]["type"] == "rule"
    assert result[1]["type"] == "blockquote"


def test_a_realistic_comment_produces_every_block_type() -> None:
    markdown = (
        "# PROJ-123 — comentario\n\n"
        "Texto con **negrita** y `código`.\n\n"
        "---\n\n"
        "## Tareas\n\n"
        "1. primera\n2. segunda\n\n"
        "- viñeta\n\n"
        "| Campo | Valor |\n|---|---|\n| a | b |\n\n"
        '```bash\nsomecli db explain "SELECT 1"\n```\n'
    )
    kinds = {b["type"] for b in blocks(markdown)}
    assert kinds == {
        "heading",
        "paragraph",
        "rule",
        "orderedList",
        "bulletList",
        "table",
        "codeBlock",
    }


def test_mentions_and_media_cards_have_the_shape_jira_expects() -> None:
    mention = adf.mention("712020:abc", "@Kenia Vargas")
    assert mention["type"] == "mention"
    assert mention["attrs"]["id"] == "712020:abc"

    card = adf.media_card("11111111-2222-3333-4444-555555555555")
    assert card["type"] == "mediaGroup"
    assert card["content"][0]["attrs"]["type"] == "file"


def test_empty_markdown_yields_an_empty_document() -> None:
    assert adf.from_markdown("")["content"] == []


# --- ADF -> markdown ---------------------------------------------------------
# The traffic goes both ways: a ticket's `description` arrives as an ADF tree
# and a spec folder is markdown, so a spec generator needs the inverse.


def test_a_description_round_trips_through_both_converters() -> None:
    """The strongest statement available: what goes in comes back out.

    Not every ADF document can round-trip — ADF has nodes markdown has no
    spelling for — but everything `from_markdown` can produce must survive,
    or one of the two converters is lying about the same document.
    """
    source = (
        "# Título\n\n"
        "Un párrafo con **negrita**, *cursiva* y `código`.\n\n"
        "- uno\n- dos\n\n"
        "1. primero\n2. segundo\n\n"
        "> una cita\n\n"
        "```sql\nSELECT 1;\n```\n\n"
        "---\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n\n"
        "Ver [el ticket](https://example.atlassian.net/browse/PROJ-1)."
    )
    assert adf.to_markdown(adf.from_markdown(source)) == source.strip()


def test_a_plain_string_field_is_returned_unchanged() -> None:
    """Fields predating ADF come back as text, and the caller cannot tell in advance."""
    assert adf.to_markdown("  plain wiki text  ") == "plain wiki text"
    assert adf.to_markdown(None) == ""
    assert adf.to_markdown({"type": "doc", "version": 1}) == ""


def test_a_hard_break_inside_a_paragraph_becomes_a_line_break() -> None:
    """JIRA descriptions are full of these — shift+enter is how people write tickets."""
    document = adf.document(
        adf.paragraph(adf.text("primera"), {"type": "hardBreak"}, adf.text("segunda"))
    )
    assert adf.to_markdown(document) == "primera\nsegunda"


def test_a_mention_keeps_its_display_name() -> None:
    document = adf.document(adf.paragraph(adf.mention("abc-123", "@Kenia")))
    assert adf.to_markdown(document) == "@Kenia"


def test_an_attached_image_is_dropped_rather_than_faked() -> None:
    """A `media` node's id is a UUID no markdown viewer can resolve.

    Rendering it as `![](abc-123)` would put a broken image in every README;
    the attachment is still in the `<KEY>.json` snapshot for anyone who needs it.
    """
    document = adf.document(adf.paragraph(adf.text("antes")), adf.media_card("abc-123"))
    assert adf.to_markdown(document) == "antes"


def test_a_pipe_inside_a_table_cell_does_not_break_the_row() -> None:
    document = adf.table([[[adf.text("a|b")], [adf.text("c")]]])
    rendered = adf.to_markdown(adf.document(document))
    assert rendered.splitlines()[0] == r"| a\|b | c |"
