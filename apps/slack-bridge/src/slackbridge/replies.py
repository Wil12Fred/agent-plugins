"""Turning a Slack thread into an instruction for a session.

This is the ``--process-replies`` logic of both legacy scripts, unified.

**How a session is identified**, in order of reliability:

1. the **parent** message — the bridge writes the session id at the START of every
   message it posts, precisely so the thread it opens is self-identifying. When the parent
   names a session, *all* the human replies are the instruction;
2. otherwise the first reply whose first token resolves to a session — that token is
   stripped and the rest of the replies become the instruction;
3. otherwise the first token of the first reply, verbatim (the original behaviour).

Two shapes a reply can take, and they mean different things:

* a reply that is **just a session id** returns that session's last response and runs
  nothing (cheap "what did it say?");
* a reply whose **first token is a session id** sends the rest as an instruction, which
  ends up wrapping ``claude --resume <id> -p "<instruction>"`` for a closed session, or a
  keystroke injection for a live one.

Only messages from allowlisted humans are read as instructions; the bridge's own posts are
excluded by ``bot_id``/``app_id`` (see :mod:`slackbridge.access`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from slackbridge import claude, codex
from slackbridge.access import human_texts, is_human_instruction
from slackbridge.api import SlackAPI
from slackbridge.config import SlackConfig


@dataclass(frozen=True)
class ThreadBatch:
    """The human messages of one thread, plus where to answer."""

    channel: str
    thread_ts: str | None
    parent_text: str
    texts: list[str]
    bridge_text: str
    """Concatenated non-human text of the thread — used as a session-id hint."""


@dataclass(frozen=True)
class Instruction:
    """What a thread resolved to."""

    sid: str
    text: str
    """Empty means "just show the last response"."""


def read_thread(
    api: SlackAPI,
    config: SlackConfig,
    *,
    channel: str | None = None,
    thread_ts: str | None = None,
    limit: int = 50,
) -> ThreadBatch:
    """Read one thread's human replies.

    Passing ``thread_ts`` reads that exact thread, which is what the Socket Mode listener
    does: it is deterministic and immune to another thread being active at the same time.
    Without it, the newest thread that has human replies is used, falling back to the
    newest top-level human message.
    """
    target = config.require_channel(channel)

    if thread_ts:
        messages = api.replies(target, thread_ts, limit=limit)
        return _batch(config, target, thread_ts, messages)

    tops = api.history(target, limit=max(limit, 10))
    for parent in tops:
        ts = parent.get("thread_ts") or parent.get("ts")
        if not ts or not (parent.get("reply_count") or parent.get("thread_ts")):
            continue
        messages = api.replies(target, ts, limit=limit)
        if human_texts(config, messages[1:]):
            return _batch(config, target, ts, messages)

    for parent in tops:
        text = (parent.get("text") or "").strip()
        if text and is_human_instruction(config, parent):
            return ThreadBatch(
                channel=target,
                thread_ts=parent.get("ts"),
                parent_text="",
                texts=[text],
                bridge_text="",
            )
    return ThreadBatch(channel=target, thread_ts=None, parent_text="", texts=[], bridge_text="")


def _batch(
    config: SlackConfig, channel: str, thread_ts: str, messages: list[dict[str, Any]]
) -> ThreadBatch:
    parent = messages[0] if messages else {}
    replies = messages[1:]
    bridge_text = "\n".join(
        (m.get("text") or "") for m in replies if not is_human_instruction(config, m)
    )
    return ThreadBatch(
        channel=channel,
        thread_ts=thread_ts,
        parent_text=(parent.get("text") or ""),
        texts=human_texts(config, replies),
        bridge_text=bridge_text,
    )


def resolve_instruction(batch: ThreadBatch) -> Instruction | None:
    """Work out which session the thread is about and what to send it.

    Returns ``None`` when the thread carries no human text at all.
    """
    if not batch.texts:
        return None

    hint = f"{batch.parent_text}\n{batch.bridge_text}"
    sid = claude.first_session_token(hint) or codex.first_session_token(hint)
    if sid:
        return Instruction(sid=sid, text="\n".join(batch.texts).strip())

    for index, text in enumerate(batch.texts):
        token = claude.first_session_token(text) or codex.first_session_token(text)
        if not token:
            continue
        rest = text.replace(token, "", 1).strip()
        others = batch.texts[:index] + batch.texts[index + 1 :]
        return Instruction(sid=token, text="\n".join(([rest] if rest else []) + others).strip())

    head = batch.texts[0].split(None, 1)
    return Instruction(
        sid=head[0],
        text="\n".join(([head[1]] if len(head) > 1 else []) + batch.texts[1:]).strip(),
    )
