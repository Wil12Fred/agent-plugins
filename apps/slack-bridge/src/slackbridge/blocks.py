"""Block Kit rendering for session listings and answers.

Every message the bridge posts carries the same action row, so any answer is as actionable
as a listing card. The ``action_id`` values are a contract with
:mod:`slackbridge.listeners.actions` — renaming one here silently breaks the button.

Cards omit ✗ Cerrar / ⏹ Stop for sessions with no ``pid``: there is no process to
terminate or interrupt, and offering the button would only produce a confusing error.
"""

from __future__ import annotations

import json
from typing import Any

MAX_CARDS = 15
NOTIFY_CAP = 150
"""Slack's notification preview length — the `text` fallback is truncated to it."""
BODY_CAP = 3400


def action_row(
    sid: str,
    short: str,
    *,
    include_last: bool = True,
    include_stop: bool = False,
    include_close: bool = True,
) -> dict[str, Any]:
    """The button row. Buttons carry the FULL session id in ``value``."""
    elements: list[dict[str, Any]] = [
        {
            "type": "button",
            "action_id": "sess_continue",
            "value": sid,
            "style": "primary",
            "text": {"type": "plain_text", "text": "▶️ Continuar…", "emoji": True},
        }
    ]
    if include_close:
        elements.append(
            {
                "type": "button",
                "action_id": "sess_close",
                "value": sid,
                "style": "danger",
                "text": {"type": "plain_text", "text": "🗙 Cerrar", "emoji": True},
                "confirm": {
                    "title": {"type": "plain_text", "text": "¿Cerrar sesión?"},
                    "text": {"type": "mrkdwn", "text": f"Se terminará el proceso de `{short}`."},
                    "confirm": {"type": "plain_text", "text": "Cerrar"},
                    "deny": {"type": "plain_text", "text": "Cancelar"},
                },
            }
        )
    elements.append(
        {
            "type": "button",
            "action_id": "sess_compact",
            "value": sid,
            "text": {"type": "plain_text", "text": "🗜 Compact", "emoji": True},
        }
    )
    if include_last:
        elements.append(
            {
                "type": "button",
                "action_id": "sess_last",
                "value": sid,
                "text": {"type": "plain_text", "text": "💬 Última", "emoji": True},
            }
        )
    if include_stop:
        elements.append(
            {
                "type": "button",
                "action_id": "sess_stop",
                "value": sid,
                "text": {"type": "plain_text", "text": "⏹ Stop", "emoji": True},
            }
        )
    return {"type": "actions", "block_id": f"sess::{short}", "elements": elements}


def session_card(row: dict[str, Any]) -> list[dict[str, Any]]:
    """One session's section + action row."""
    sid, short = row.get("sid", ""), row.get("short", "?")
    emoji = "🟠" if row.get("status") == "busy" else "🟢"
    badge = "🟣 Codex" if row.get("engine") == "codex" else "🔵 Claude"
    project = (row.get("project") or "").rstrip("/").split("/")[-1] or "—"
    title = (row.get("title") or "(untitled)")[:140]
    live = bool(row.get("pid"))
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{emoji} *{title}*\n{badge}  ·  `{short}`  ·  {row.get('status', '?')}  ·  "
                    f"{row.get('started', '?')}  ·  _{project}_"
                ),
            },
        },
        action_row(
            sid,
            short,
            include_close=live,
            include_stop=live and row.get("status") == "busy",
        ),
    ]


def sessions_blocks(
    rows: list[dict[str, Any]], filt: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Session cards grouped Working (busy) / Live (idle, terminal open) / Otros (closed).

    ``filt`` (``{all, live, query}``) is echoed in the header and carried by the 🔄
    Refrescar button, so refreshing reproduces exactly the same view.
    """
    filt = filt or {}
    tags = "".join(
        [
            " · live" if filt.get("live") else "",
            " · all" if filt.get("all") else "",
            f" · “{filt['query']}”" if filt.get("query") else "",
        ]
    )
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🗂  Sesiones ({len(rows)}){tags}",
                "emoji": True,
            },
        }
    ]
    working = [r for r in rows if r.get("pid") and r.get("status") == "busy"]
    live = [r for r in rows if r.get("pid") and r.get("status") != "busy"]
    others = [r for r in rows if not r.get("pid")]
    shown = 0
    for label, group in (("🛠 Working", working), ("🟢 Live", live), ("💤 Otros", others)):
        if not group:
            continue
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{label} ({len(group)})*"}}
        )
        for row in group:
            if shown >= MAX_CARDS:
                break
            blocks.extend(session_card(row))
            shown += 1
    if len(rows) > shown:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"…y {len(rows) - shown} más"}],
            }
        )
    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "actions",
            "block_id": "sess_footer",
            "elements": [
                {
                    "type": "button",
                    "action_id": "sess_new",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "➕ Nueva sesión", "emoji": True},  # noqa: RUF001
                },
                {
                    "type": "button",
                    "action_id": "sess_refresh",
                    "value": json.dumps(
                        {
                            "all": bool(filt.get("all")),
                            "live": bool(filt.get("live")),
                            "query": filt.get("query", ""),
                        },
                        ensure_ascii=False,
                    ),
                    "text": {"type": "plain_text", "text": "🔄 Refrescar", "emoji": True},
                },
            ],
        }
    )
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "🟠 busy (procesando) · 🟢 idle (lista)"}],
        }
    )
    return blocks


def home_view(rows: list[dict[str, Any]], filt: dict[str, Any] | None = None) -> dict[str, Any]:
    """App Home dashboard: the same cards, always available without a slash command."""
    blocks = (
        sessions_blocks(rows, filt)
        if rows
        else [
            {"type": "section", "text": {"type": "mrkdwn", "text": "*No hay sesiones activas.*"}},
            {
                "type": "actions",
                "block_id": "sess_footer",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "sess_new",
                        "style": "primary",
                        "text": {"type": "plain_text", "text": "➕ Nueva sesión", "emoji": True},  # noqa: RUF001
                    },
                    {
                        "type": "button",
                        "action_id": "sess_refresh",
                        "value": json.dumps(filt or {}, ensure_ascii=False),
                        "text": {"type": "plain_text", "text": "🔄 Refrescar", "emoji": True},
                    },
                ],
            },
        ]
    )
    return {"type": "home", "blocks": blocks}


def result_blocks(sid: str, text: str, instr: str | None = None) -> list[dict[str, Any]]:
    """A session's answer, rendered with the action row so every reply stays actionable."""
    short = (sid or "?")[:8]
    head = f"*`{short}`*" + (f"  ▶️ _{instr[:80]}_" if instr else "")
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{head}\n{(text or '(sin salida)')[:BODY_CAP]}"},
        },
        action_row(sid, short, include_last=False),
    ]


def pending_blocks(
    sid: str, instr: str, preview: str | None = None, *, busy: bool = True
) -> list[dict[str, Any]]:
    """The temporary message shown while an instruction is running."""
    short = (sid or "?")[:8]
    body = preview or "Procesando..."
    elements: list[dict[str, Any]] = []
    if busy:
        elements.append(
            {
                "type": "button",
                "action_id": "sess_stop",
                "value": sid,
                "style": "danger",
                "text": {"type": "plain_text", "text": "⏹ Stop", "emoji": True},
            }
        )
    else:
        elements.append(
            {
                "type": "button",
                "action_id": "sess_last",
                "value": sid,
                "text": {"type": "plain_text", "text": "💬 Última", "emoji": True},
            }
        )
    elements.append(
        {
            "type": "button",
            "action_id": "sess_refresh_continue",
            "value": json.dumps({"sid": sid, "instr": instr[:500]}, ensure_ascii=False),
            "text": {"type": "plain_text", "text": "🔄 Refrescar", "emoji": True},
        }
    )
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*`{short}`*  ▶️ _{instr[:80]}_\n{body[:2800]}"},
        },
        {"type": "actions", "block_id": f"sess-pending::{short}", "elements": elements},
    ]
