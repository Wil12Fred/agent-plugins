"""Atlassian Document Format: builders, and a markdown converter.

Seven ticket-local scripts each shipped their own copy of ``txt`` / ``strong`` /
``code`` / ``link`` / ``mention`` / ``para`` / ``heading`` / ``bullets``. They
are here once.

More importantly, JIRA comments in this repo are *written as markdown* — the
ticket folders are full of ``jira-comment-summary.md`` files. :func:`from_markdown`
turns one of those into ADF directly, so a comment is authored, reviewed and
committed as a file, then posted verbatim. No more hand-assembling node trees.

Supported markdown: headings, paragraphs, bold/italic/inline code, links,
fenced code blocks, bullet and ordered lists, tables, blockquotes and rules —
which is everything the existing comments use.

:func:`to_markdown` is the inverse, and it exists because the traffic goes both
ways: a ticket's ``description`` arrives from the REST API as an ADF tree, and
a spec folder is markdown. Without it a spec generator could only paste a
JSON blob into the README, which is exactly the "captured but unreadable" state
the ``<KEY>.json`` snapshots were already in.
"""

from __future__ import annotations

import re
from typing import Any

ADFNode = dict[str, Any]

MAX_HEADING_LEVEL = 6


# --- inline -----------------------------------------------------------------
def text(value: str, marks: list[ADFNode] | None = None) -> ADFNode:
    node: ADFNode = {"type": "text", "text": value}
    if marks:
        node["marks"] = marks
    return node


def strong(value: str) -> ADFNode:
    return text(value, [{"type": "strong"}])


def emphasis(value: str) -> ADFNode:
    return text(value, [{"type": "em"}])


def code(value: str) -> ADFNode:
    return text(value, [{"type": "code"}])


def link(value: str, href: str) -> ADFNode:
    return text(value, [{"type": "link", "attrs": {"href": href}}])


def mention(account_id: str, display: str) -> ADFNode:
    """An @mention. ``display`` should already include the leading ``@``."""
    return {"type": "mention", "attrs": {"id": account_id, "text": display}}


# --- blocks ------------------------------------------------------------------
def paragraph(*content: ADFNode) -> ADFNode:
    return {"type": "paragraph", "content": list(content)}


def heading(value: str, level: int = 3) -> ADFNode:
    return {
        "type": "heading",
        "attrs": {"level": min(max(level, 1), MAX_HEADING_LEVEL)},
        "content": [text(value)],
    }


def bullet_list(items: list[list[ADFNode]]) -> ADFNode:
    return {
        "type": "bulletList",
        "content": [{"type": "listItem", "content": [paragraph(*item)]} for item in items],
    }


def ordered_list(items: list[list[ADFNode]]) -> ADFNode:
    return {
        "type": "orderedList",
        "attrs": {"order": 1},
        "content": [{"type": "listItem", "content": [paragraph(*item)]} for item in items],
    }


def code_block(value: str, language: str | None = None) -> ADFNode:
    node: ADFNode = {"type": "codeBlock", "content": [text(value)] if value else []}
    if language:
        node["attrs"] = {"language": language}
    return node


def rule() -> ADFNode:
    return {"type": "rule"}


def blockquote(*blocks: ADFNode) -> ADFNode:
    return {"type": "blockquote", "content": list(blocks)}


def table(rows: list[list[list[ADFNode]]], *, header: bool = True) -> ADFNode:
    """Build a table from ``rows`` of cells, each cell a list of inline nodes."""
    content: list[ADFNode] = []
    for index, row in enumerate(rows):
        cell_type = "tableHeader" if header and index == 0 else "tableCell"
        content.append(
            {
                "type": "tableRow",
                "content": [
                    {"type": cell_type, "attrs": {}, "content": [paragraph(*cell)]} for cell in row
                ],
            }
        )
    return {"type": "table", "attrs": {"isNumberColumnEnabled": False}, "content": content}


def media_card(media_id: str, *, collection: str | None = None) -> ADFNode:
    """An inline media card — how a file is embedded *inside* a comment.

    ``media_id`` is the media UUID, which is not the attachment id: it is
    obtained by following the attachment's content-URL redirect (see
    :mod:`jiractl.attachments`).
    """
    attrs: dict[str, Any] = {"id": media_id, "type": "file"}
    if collection:
        attrs["collection"] = collection
    return {
        "type": "mediaGroup",
        "content": [{"type": "media", "attrs": attrs}],
    }


def document(*blocks: ADFNode) -> ADFNode:
    """Wrap blocks into a complete ADF document."""
    return {"type": "doc", "version": 1, "content": list(blocks)}


# --- markdown ----------------------------------------------------------------
_INLINE_PATTERN = re.compile(
    r"(?P<code>`[^`]+`)"
    r"|(?P<bold>\*\*[^*]+\*\*|__[^_]+__)"
    r"|(?P<italic>\*[^*\n]+\*|(?<![A-Za-z0-9_])_[^_\n]+_(?![A-Za-z0-9_]))"
    r"|(?P<link>\[[^\]]*\]\([^)\s]+\))"
    r"|(?P<autolink>https?://\S+)"
)
_LINK_PARTS = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_FENCE = re.compile(r"^```(\w*)\s*$")
_RULE = re.compile(r"^\s*([-*_])\1{2,}\s*$")
_QUOTE = re.compile(r"^>\s?(.*)$")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")


def inline_from_markdown(value: str) -> list[ADFNode]:
    """Convert one line of markdown into inline ADF nodes."""
    nodes: list[ADFNode] = []
    cursor = 0

    for match in _INLINE_PATTERN.finditer(value):
        if match.start() > cursor:
            nodes.append(text(value[cursor : match.start()]))
        kind = match.lastgroup
        raw = match.group()

        if kind == "code":
            nodes.append(code(raw[1:-1]))
        elif kind == "bold":
            nodes.append(strong(raw[2:-2]))
        elif kind == "italic":
            nodes.append(emphasis(raw[1:-1]))
        elif kind == "link":
            parts = _LINK_PARTS.match(raw)
            if parts:
                label, href = parts.group(1), parts.group(2)
                nodes.append(link(label or href, href))
        elif kind == "autolink":
            nodes.append(link(raw, raw))
        cursor = match.end()

    if cursor < len(value):
        nodes.append(text(value[cursor:]))
    return nodes or [text(value)]


def _split_table_row(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [cell.strip() for cell in stripped.split("|")]


def from_markdown(markdown: str) -> ADFNode:
    """Convert a markdown document into a complete ADF document."""
    lines = markdown.replace("\r\n", "\n").split("\n")
    blocks: list[ADFNode] = []
    paragraph_buffer: list[str] = []
    index = 0

    def flush_paragraph() -> None:
        if paragraph_buffer:
            joined = " ".join(line.strip() for line in paragraph_buffer)
            blocks.append(paragraph(*inline_from_markdown(joined)))
            paragraph_buffer.clear()

    while index < len(lines):
        line = lines[index]

        if fence := _FENCE.match(line):
            flush_paragraph()
            language = fence.group(1) or None
            index += 1
            body: list[str] = []
            while index < len(lines) and not _FENCE.match(lines[index]):
                body.append(lines[index])
                index += 1
            blocks.append(code_block("\n".join(body), language))
            index += 1
            continue

        if not line.strip():
            flush_paragraph()
            index += 1
            continue

        if _RULE.match(line):
            flush_paragraph()
            blocks.append(rule())
            index += 1
            continue

        if match := _HEADING.match(line):
            flush_paragraph()
            blocks.append(heading(match.group(2).strip(), len(match.group(1))))
            index += 1
            continue

        # Table: a pipe row followed by a separator row.
        if "|" in line and index + 1 < len(lines) and _TABLE_SEPARATOR.match(lines[index + 1]):
            flush_paragraph()
            rows = [[inline_from_markdown(c) for c in _split_table_row(line)]]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append([inline_from_markdown(c) for c in _split_table_row(lines[index])])
                index += 1
            blocks.append(table(rows))
            continue

        if _BULLET.match(line) or _ORDERED.match(line):
            flush_paragraph()
            ordered = bool(_ORDERED.match(line))
            items: list[list[ADFNode]] = []
            while index < len(lines):
                bullet = _BULLET.match(lines[index])
                numbered = _ORDERED.match(lines[index])
                if ordered and numbered:
                    items.append(inline_from_markdown(numbered.group(1)))
                elif not ordered and bullet:
                    items.append(inline_from_markdown(bullet.group(1)))
                else:
                    break
                index += 1
            blocks.append(ordered_list(items) if ordered else bullet_list(items))
            continue

        if quote := _QUOTE.match(line):
            flush_paragraph()
            quoted: list[str] = [quote.group(1)]
            index += 1
            while index < len(lines) and (nxt := _QUOTE.match(lines[index])):
                quoted.append(nxt.group(1))
                index += 1
            blocks.append(blockquote(paragraph(*inline_from_markdown(" ".join(quoted)))))
            continue

        paragraph_buffer.append(line)
        index += 1

    flush_paragraph()
    return document(*blocks)


# --- markdown, the other way -------------------------------------------------
_MARK_WRAPPER = {"strong": "**", "em": "*", "code": "`", "strike": "~~"}

# Node types that carry no text and no useful markdown equivalent. An attached
# image inside a description is a `media` node whose only content is a UUID the
# reader cannot resolve; rendering it as `![](abc-123)` would be a broken image
# in every viewer, so it is dropped rather than faked.
_DROPPED_BLOCKS = frozenset({"mediaGroup", "mediaSingle", "media"})


def _inline_to_markdown(node: ADFNode) -> str:
    kind = node.get("type")
    if kind == "text":
        value = str(node.get("text", ""))
        href = ""
        for mark in node.get("marks") or []:
            mark_type = mark.get("type")
            if mark_type == "link":
                href = str((mark.get("attrs") or {}).get("href", ""))
            elif mark_type in _MARK_WRAPPER:
                wrapper = _MARK_WRAPPER[mark_type]
                value = f"{wrapper}{value}{wrapper}"
        return f"[{value}]({href})" if href else value
    if kind == "hardBreak":
        return "\n"
    if kind in {"mention", "emoji"}:
        attrs = node.get("attrs") or {}
        return str(attrs.get("text") or attrs.get("shortName") or "")
    if kind in {"inlineCard", "blockCard"}:
        return str((node.get("attrs") or {}).get("url", ""))
    return _inline_run(node.get("content") or [])


def _inline_run(nodes: list[ADFNode]) -> str:
    return "".join(_inline_to_markdown(node) for node in nodes)


def _list_to_markdown(node: ADFNode, *, ordered: bool, depth: int) -> str:
    lines: list[str] = []
    indent = "  " * depth
    for position, item in enumerate(node.get("content") or [], start=1):
        marker = f"{position}." if ordered else "-"
        rendered = _blocks_to_markdown(item.get("content") or [], depth=depth + 1)
        if not rendered:
            continue
        first, *rest = rendered.split("\n")
        lines.append(f"{indent}{marker} {first}")
        # Continuation lines hang under the marker, or the list item ends there.
        lines.extend(f"{indent}  {line}" if line else "" for line in rest)
    return "\n".join(lines)


def _table_to_markdown(node: ADFNode) -> str:
    rows: list[list[str]] = []
    for row in node.get("content") or []:
        cells = []
        for cell in row.get("content") or []:
            rendered = _blocks_to_markdown(cell.get("content") or [])
            # A pipe inside a cell would end the column; a newline would end the
            # row. Both have to go, or the table stops being a table.
            cells.append(rendered.replace("|", "\\|").replace("\n", " ").strip())
        rows.append(cells)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    header, *body = padded
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * width]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def _block_to_markdown(node: ADFNode, depth: int = 0) -> str:
    kind = node.get("type")
    if kind in _DROPPED_BLOCKS:
        return ""
    if kind == "heading":
        level = int((node.get("attrs") or {}).get("level", 3))
        return (
            "#" * min(max(level, 1), MAX_HEADING_LEVEL)
            + " "
            + _inline_run(node.get("content") or [])
        )
    if kind == "codeBlock":
        language = str((node.get("attrs") or {}).get("language") or "")
        return f"```{language}\n" + _inline_run(node.get("content") or []) + "\n```"
    if kind == "rule":
        return "---"
    if kind in {"bulletList", "orderedList"}:
        return _list_to_markdown(node, ordered=kind == "orderedList", depth=depth)
    if kind == "table":
        return _table_to_markdown(node)
    if kind in {"blockquote", "panel"}:
        inner = _blocks_to_markdown(node.get("content") or [], depth=depth)
        return "\n".join(f"> {line}" if line else ">" for line in inner.split("\n"))
    if kind == "expand":
        title = str((node.get("attrs") or {}).get("title") or "")
        inner = _blocks_to_markdown(node.get("content") or [], depth=depth)
        return f"**{title}**\n\n{inner}" if title else inner
    return _inline_run(node.get("content") or []) if "content" in node else ""


def _blocks_to_markdown(nodes: list[ADFNode], depth: int = 0) -> str:
    blocks = [_block_to_markdown(node, depth) for node in nodes]
    return "\n\n".join(block for block in blocks if block.strip())


def to_markdown(document_node: object) -> str:
    """Render an ADF document as markdown.

    Accepts a plain string unchanged: JIRA fields predating ADF — and every
    field on a project still on the old wiki renderer — come back as text, and a
    caller reading `fields["description"]` cannot tell which it will get.
    """
    if isinstance(document_node, str):
        return document_node.strip()
    if not isinstance(document_node, dict):
        return ""
    content = document_node.get("content")
    if not isinstance(content, list):
        return ""
    return _blocks_to_markdown(content).strip()
