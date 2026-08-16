"""Modal submissions.

``continue_modal``  dispatch a typed instruction to an existing session
``new_modal``       create a new session (prompt + optional model)

The modal carries its target in ``private_metadata`` (session id, channel, thread) because
a view submission arrives without the message it came from.
"""

from __future__ import annotations

import json
import threading
from logging import Logger
from typing import Any

from slack_bolt import Ack, App
from slack_sdk import WebClient

from slackbridge.access import is_allowed
from slackbridge.blocks import NOTIFY_CAP, result_blocks
from slackbridge.claude import create_headless
from slackbridge.config import SlackConfig
from slackbridge.ui import stream_dispatch


def _meta(view: dict[str, Any]) -> dict[str, Any]:
    try:
        return dict(json.loads(view.get("private_metadata") or "{}"))
    except json.JSONDecodeError:
        return {}


def _input(view: dict[str, Any], block: str) -> str:
    values = view.get("state", {}).get("values", {})
    return str((values.get(block, {}).get("val", {}) or {}).get("value") or "").strip()


def register(app: App, config: SlackConfig) -> None:
    """Register the ``continue_modal`` and ``new_modal`` submissions."""

    @app.view("continue_modal")
    def _continue(
        ack: Ack, body: dict[str, Any], view: dict[str, Any], client: WebClient, logger: Logger
    ) -> None:
        ack()
        user = str((body.get("user") or {}).get("id", ""))
        if not is_allowed(config, user):
            return
        meta = _meta(view)
        sid, channel = str(meta.get("sid", "")), str(meta.get("channel", ""))
        instruction = _input(view, "instr")
        if not sid or not instruction or not channel:
            return
        threading.Thread(
            target=stream_dispatch,
            args=(client, channel, sid, instruction, logger),
            kwargs={"mention": user, "thread_ts": meta.get("thread_ts")},
            daemon=True,
        ).start()

    @app.view("new_modal")
    def _new(
        ack: Ack, body: dict[str, Any], view: dict[str, Any], client: WebClient, logger: Logger
    ) -> None:
        ack()
        if not is_allowed(config, str((body.get("user") or {}).get("id", ""))):
            return
        meta = _meta(view)
        channel = str(meta.get("channel", ""))
        thread_ts = meta.get("thread_ts")
        prompt = _input(view, "prompt")
        selected = (
            view.get("state", {}).get("values", {}).get("model", {}).get("val", {}) or {}
        ).get("selected_option") or {}
        model = str(selected.get("value", ""))
        if not prompt or not channel:
            return

        def work() -> None:
            ts: str | None = None
            pending = f"⏳ Creando sesión{f' ({model})' if model else ''}…"
            try:
                ts = client.chat_postMessage(
                    channel=channel, text=pending, thread_ts=thread_ts
                ).get("ts")
            except Exception as exc:
                logger.error("postMessage failed: %s", exc)
            sid, answer = create_headless(prompt, config.new_session_cwd, model)
            blocks = result_blocks(sid, answer) if sid else None
            try:
                if ts:
                    client.chat_update(
                        channel=channel, ts=ts, text=answer[:NOTIFY_CAP], blocks=blocks
                    )
                else:
                    client.chat_postMessage(
                        channel=channel,
                        text=answer[:NOTIFY_CAP],
                        blocks=blocks,
                        thread_ts=thread_ts,
                    )
            except Exception as exc:
                logger.error("chat_update failed: %s", exc)

        threading.Thread(target=work, daemon=True).start()
