"""The auto-dispatch listener: every message in the control channel becomes an action.

* **thread reply** -> dispatch to the session that thread is about
  (:mod:`slackbridge.replies` resolves which one), then post the answer in the thread;
* **top-level message** -> open a NEW live Claude session with that message as the prompt
  and answer in its thread, so the conversation can continue from there.

Guards, in the order they run — each one exists because of a real failure:

1. wrong channel -> ignore (the app receives every message in the workspace);
2. ``subtype`` set -> ignore edits/joins/system messages;
3. ``bot_id``/``app_id`` set -> ignore. **Config-independent on purpose**: it holds even
   when the allowlist env vars are empty in this process, where ``is_allowed`` fails open.
   Without it the bridge ingests its own completion notice and every answer spawns a new
   session (the OPER-714 loop);
4. not allowlisted -> ignore;
5. already-seen ``ts`` -> ignore. Slack redelivers events; the bounded ``OrderedDict``
   keeps the dedup set from growing without limit in a long-lived listener.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from logging import Logger
from typing import Any

from slack_bolt import App
from slack_sdk import WebClient

from slackbridge import sessions
from slackbridge.access import is_allowed, is_bridge_post
from slackbridge.api import SlackAPI
from slackbridge.blocks import NOTIFY_CAP, result_blocks
from slackbridge.config import SlackConfig
from slackbridge.replies import read_thread, resolve_instruction
from slackbridge.ui import open_new_codex_session, open_new_session, wants_codex

DEDUP_MAX = 2000
WORKING_TAG = "🛠 Trabajando · "
"""Prepended to a parent message while its thread is being worked on."""

_processed: OrderedDict[str, None] = OrderedDict()
_lock = threading.Lock()


def _seen(ts: str) -> bool:
    """True when ``ts`` was already handled; otherwise records it (evicting the oldest)."""
    with _lock:
        if ts in _processed:
            return True
        _processed[ts] = None
        if len(_processed) > DEDUP_MAX:
            _processed.popitem(last=False)
        return False


def _mark_working(
    client: WebClient, channel: str, parent_ts: str, logger: Logger, config: SlackConfig
) -> None:
    """Prefix the bridge's own parent message with 🛠, so the active thread is obvious."""
    try:
        replies = client.conversations_replies(channel=channel, ts=parent_ts, limit=1)
        parent = (replies.get("messages") or [None])[0]
        if not parent:
            return
        if config.bot_user_id and parent.get("user") != config.bot_user_id:
            return  # only ever edit our own messages
        blocks = parent.get("blocks") or []
        if not blocks:
            return
        head = blocks[0]
        node = head.get("text") if head.get("type") in ("header", "section") else None
        if not node or not isinstance(node.get("text"), str):
            return
        if "Trabajando" in node["text"]:
            return
        node["text"] = WORKING_TAG + node["text"]
        client.chat_update(channel=channel, ts=parent_ts, blocks=blocks, text="Trabajando")
    except Exception as exc:
        logger.error("mark working failed: %s", exc)


def _dispatch_thread(
    client: WebClient, config: SlackConfig, channel: str, thread_ts: str, logger: Logger
) -> None:
    """Resolve the thread's session and post the answer back into that same thread."""
    api = SlackAPI(config.require_bot_token())
    try:
        batch = read_thread(api, config, channel=channel, thread_ts=thread_ts)
        instruction = resolve_instruction(batch)
    except Exception as exc:
        logger.error("thread read failed: %s", exc)
        return
    if instruction is None:
        return

    if instruction.text:
        api.post(channel, f"{instruction.sid}\n\nEnviado; procesando...", thread_ts=thread_ts)
    out = sessions.dispatch(f"{instruction.sid} {instruction.text}".strip())
    body = f"{config.mention()}{out}"
    try:
        api.post(
            channel,
            body[:NOTIFY_CAP],
            thread_ts=thread_ts,
            blocks=result_blocks(instruction.sid, body),
        )
    except Exception as exc:
        logger.error("thread answer post failed: %s", exc)


def callback(event: dict[str, Any], client: WebClient, logger: Logger, config: SlackConfig) -> None:
    """Handle one ``message`` event. See the module docstring for the guard order."""
    if config.channel_id and event.get("channel") != config.channel_id:
        return
    if event.get("subtype"):
        return
    if is_bridge_post(event):
        return
    if not is_allowed(config, event.get("user")):
        return
    ts = event.get("ts", "")
    if not ts or _seen(ts):
        return

    channel = event.get("channel", "")
    thread_ts = event.get("thread_ts", "")
    user = event.get("user", "")

    if thread_ts:
        logger.info("thread reply in %s thread=%s -> dispatching", channel, thread_ts)
        threading.Thread(
            target=_mark_working, args=(client, channel, thread_ts, logger, config), daemon=True
        ).start()
        threading.Thread(
            target=_dispatch_thread,
            args=(client, config, channel, thread_ts, logger),
            daemon=True,
        ).start()
        return

    prompt = (event.get("text") or "").strip()
    if not prompt:
        return
    codex_prompt = wants_codex(prompt)
    if codex_prompt is not None:
        logger.info("top-level message in %s ts=%s -> new Codex session", channel, ts)
        threading.Thread(
            target=open_new_codex_session,
            args=(client, channel, ts, codex_prompt, user, logger),
            kwargs={"cwd": str(config.new_session_cwd)},
            daemon=True,
        ).start()
        return

    logger.info("top-level message in %s ts=%s -> new Claude session", channel, ts)
    threading.Thread(
        target=open_new_session,
        args=(client, channel, ts, prompt, user, logger),
        kwargs={"cwd": str(config.new_session_cwd), "terminal": config.new_session_terminal},
        daemon=True,
    ).start()


def register(app: App, config: SlackConfig) -> None:
    """Subscribe to ``message`` events; the callback scopes itself to the control channel."""

    def handler(event: dict[str, Any], client: WebClient, logger: Logger) -> None:
        callback(event, client, logger, config)

    app.event("message")(handler)
