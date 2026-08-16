"""Button handlers for the session cards and answer messages.

``sess_continue``  open a modal to type an instruction for that session
``sess_last``      reply with the session's last response
``sess_stop``      interrupt the current generation (ESC)
``sess_close``     terminate the session's process (confirmed in the button dialog)
``sess_compact``   send ``/compact`` — live sessions only
``sess_new``       modal to create a new session
``sess_refresh``   re-render the list with the SAME filter the button carries
``sess_refresh_continue``  re-check a running instruction (busy -> Stop, idle -> Última)

Slow work uses post-then-update: "⏳ Procesando…" goes out immediately (instant proof the
daemon is alive), then that same message is edited with the result plus buttons.

``sess_close`` answers **ephemerally** on purpose — a permanent "closed" note inside a
listing or answer thread pollutes the thread and makes it useless for follow-up replies,
which are how the bridge is actually driven.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from logging import Logger
from typing import Any

from slack_bolt import Ack, App, Respond
from slack_sdk import WebClient

from slackbridge import sessions
from slackbridge.access import is_allowed
from slackbridge.blocks import NOTIFY_CAP, home_view, pending_blocks, result_blocks, sessions_blocks
from slackbridge.config import SlackConfig


def _bg(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()


def _value(body: dict[str, Any]) -> str:
    return str((body.get("actions") or [{}])[0].get("value", ""))


def _user(body: dict[str, Any]) -> str:
    return str((body.get("user") or {}).get("id", ""))


def _channel(body: dict[str, Any], config: SlackConfig) -> str:
    """Where to post. App Home clicks have no channel, so fall back to the control channel."""
    return str(
        (body.get("channel") or {}).get("id")
        or (body.get("container") or {}).get("channel_id")
        or config.channel_id
    )


def _from_home(body: dict[str, Any]) -> bool:
    return (body.get("container") or {}).get("type") == "view"


def _thread_ts(body: dict[str, Any]) -> str | None:
    """Thread the result under the message the button lives on.

    Only for real channel messages — an ephemeral message or a Home view has no thread to
    reply into.
    """
    container = body.get("container") or {}
    if container.get("type") == "message" and not container.get("is_ephemeral"):
        message = body.get("message") or {}
        return message.get("thread_ts") or container.get("message_ts")
    return None


def _message_ts(body: dict[str, Any]) -> str | None:
    container = body.get("container") or {}
    return container.get("message_ts") or (body.get("message") or {}).get("ts")


def _list_filter(body: dict[str, Any]) -> dict[str, Any] | None:
    """The filter of the ``/sessions`` list the button belongs to, read from its footer."""
    for block in (body.get("message") or {}).get("blocks", []):
        if block.get("block_id") != "sess_footer":
            continue
        for element in block.get("elements", []):
            if element.get("action_id") == "sess_refresh":
                try:
                    return dict(json.loads(element.get("value") or "{}"))
                except json.JSONDecodeError:
                    return {}
    return None


def _run_with_feedback(
    client: WebClient,
    channel: str,
    sid: str,
    fn: Callable[[], str],
    logger: Logger,
    *,
    thread_ts: str | None = None,
) -> None:
    ts: str | None = None
    try:
        ts = client.chat_postMessage(
            channel=channel, text="⏳ Procesando…", thread_ts=thread_ts
        ).get("ts")
    except Exception as exc:
        logger.error("postMessage failed: %s", exc)
    text = fn() or "(sin salida)"
    blocks = result_blocks(sid, text)
    try:
        if ts:
            client.chat_update(channel=channel, ts=ts, text=text[:NOTIFY_CAP], blocks=blocks)
        else:
            client.chat_postMessage(
                channel=channel, text=text[:NOTIFY_CAP], blocks=blocks, thread_ts=thread_ts
            )
    except Exception as exc:
        logger.error("chat_update failed: %s", exc)


def register(app: App, config: SlackConfig) -> None:
    """Register every ``sess_*`` button handler."""

    @app.action("sess_continue")
    def _continue(ack: Ack, body: dict[str, Any], client: WebClient, logger: Logger) -> None:
        ack()
        if not is_allowed(config, _user(body)):
            return
        sid = _value(body)
        metadata = json.dumps(
            {"sid": sid, "channel": _channel(body, config), "thread_ts": _thread_ts(body)}
        )
        try:
            client.views_open(
                trigger_id=body["trigger_id"],
                view={
                    "type": "modal",
                    "callback_id": "continue_modal",
                    "private_metadata": metadata,
                    "title": {"type": "plain_text", "text": "Continuar sesión"},
                    "submit": {"type": "plain_text", "text": "Enviar"},
                    "close": {"type": "plain_text", "text": "Cancelar"},
                    "blocks": [
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": f"Sesión `{sid[:8]}`"},
                        },
                        {
                            "type": "input",
                            "block_id": "instr",
                            "label": {"type": "plain_text", "text": "Instrucción"},
                            "element": {
                                "type": "plain_text_input",
                                "action_id": "val",
                                "multiline": True,
                            },
                        },
                    ],
                },
            )
        except Exception as exc:
            logger.error("views_open failed: %s", exc)

    @app.action("sess_last")
    def _last(ack: Ack, body: dict[str, Any], client: WebClient, logger: Logger) -> None:
        ack()
        if not is_allowed(config, _user(body)):
            return
        sid = _value(body)
        channel, thread_ts = _channel(body, config), _thread_ts(body)
        _bg(
            lambda: _run_with_feedback(
                client, channel, sid, lambda: sessions.dispatch(sid), logger, thread_ts=thread_ts
            )
        )

    @app.action("sess_stop")
    def _stop(ack: Ack, body: dict[str, Any], client: WebClient, logger: Logger) -> None:
        ack()
        if not is_allowed(config, _user(body)):
            return
        sid = _value(body)
        channel, thread_ts = _channel(body, config), _thread_ts(body)
        _bg(
            lambda: _run_with_feedback(
                client, channel, sid, lambda: sessions.stop(sid), logger, thread_ts=thread_ts
            )
        )

    @app.action("sess_compact")
    def _compact(ack: Ack, body: dict[str, Any], client: WebClient, logger: Logger) -> None:
        ack()
        if not is_allowed(config, _user(body)):
            return
        sid = _value(body)
        channel, thread_ts = _channel(body, config), _thread_ts(body)

        def work() -> None:
            # `/compact` is a TUI command: it only means anything when injected into a live
            # terminal. Resuming a closed session with -p would just send it as plain text.
            if not sessions.is_live(sid):
                try:
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=f"🗜 `{sid[:8]}`: Compact solo aplica a sesiones vivas "
                        "(con terminal abierto).",
                    )
                except Exception as exc:
                    logger.error("compact notice failed: %s", exc)
                return
            _run_with_feedback(
                client,
                channel,
                sid,
                lambda: sessions.dispatch(f"{sid} {sessions.COMPACT_CMD}"),
                logger,
                thread_ts=thread_ts,
            )

        _bg(work)

    @app.action("sess_close")
    def _close(
        ack: Ack, body: dict[str, Any], client: WebClient, respond: Respond, logger: Logger
    ) -> None:
        ack()
        user = _user(body)
        if not is_allowed(config, user):
            return
        sid = _value(body)
        from_home = _from_home(body)
        channel = _channel(body, config)
        list_filter = _list_filter(body)

        def work() -> None:
            try:
                out = sessions.close(sid)
                if from_home:
                    client.chat_postEphemeral(channel=channel, user=user, text=out)
                    rows = sessions.list_all(all_projects=True)
                    client.views_publish(user_id=user, view=home_view(rows))
                    return
                respond({"response_type": "ephemeral", "replace_original": False, "text": out})
                if list_filter is None:
                    return
                rows = sessions.list_all(
                    all_projects=True,
                    live_only=bool(list_filter.get("live")),
                    query=list_filter.get("query", ""),
                )
                if rows:
                    respond(
                        {
                            "replace_original": True,
                            "blocks": sessions_blocks(rows, list_filter),
                            "text": f"{len(rows)} sesiones",
                        }
                    )
                else:
                    respond({"replace_original": True, "text": "No hay sesiones que coincidan."})
            except Exception as exc:
                logger.error("close respond failed: %s", exc)

        _bg(work)

    @app.action("sess_new")
    def _new(ack: Ack, body: dict[str, Any], client: WebClient, logger: Logger) -> None:
        ack()
        if not is_allowed(config, _user(body)):
            return
        metadata = json.dumps({"channel": _channel(body, config), "thread_ts": _thread_ts(body)})
        try:
            client.views_open(
                trigger_id=body["trigger_id"],
                view={
                    "type": "modal",
                    "callback_id": "new_modal",
                    "private_metadata": metadata,
                    "title": {"type": "plain_text", "text": "Nueva sesión"},
                    "submit": {"type": "plain_text", "text": "Crear"},
                    "close": {"type": "plain_text", "text": "Cancelar"},
                    "blocks": [
                        {
                            "type": "input",
                            "block_id": "prompt",
                            "label": {"type": "plain_text", "text": "Prompt inicial"},
                            "element": {
                                "type": "plain_text_input",
                                "action_id": "val",
                                "multiline": True,
                            },
                        },
                        {
                            "type": "input",
                            "block_id": "model",
                            "optional": True,
                            "label": {"type": "plain_text", "text": "Modelo"},
                            "element": {
                                "type": "static_select",
                                "action_id": "val",
                                "placeholder": {"type": "plain_text", "text": "Default"},
                                "options": [
                                    {
                                        "text": {"type": "plain_text", "text": "Default"},
                                        "value": "",
                                    },
                                    {
                                        "text": {"type": "plain_text", "text": "Opus"},
                                        "value": "opus",
                                    },
                                    {
                                        "text": {"type": "plain_text", "text": "Sonnet"},
                                        "value": "sonnet",
                                    },
                                    {
                                        "text": {"type": "plain_text", "text": "Haiku"},
                                        "value": "haiku",
                                    },
                                ],
                            },
                        },
                    ],
                },
            )
        except Exception as exc:
            logger.error("views_open (new) failed: %s", exc)

    @app.action("sess_refresh")
    def _refresh(
        ack: Ack, body: dict[str, Any], client: WebClient, respond: Respond, logger: Logger
    ) -> None:
        ack()
        user = _user(body)
        if not is_allowed(config, user):
            return
        from_home = _from_home(body)
        try:
            filt = dict(json.loads(_value(body) or "{}"))
        except json.JSONDecodeError:
            filt = {}
        live_only, query = bool(filt.get("live")), str(filt.get("query", ""))

        def work() -> None:
            rows = sessions.list_all(all_projects=True, live_only=live_only, query=query)
            if from_home:
                try:
                    client.views_publish(user_id=user, view=home_view(rows, filt))
                except Exception as exc:
                    logger.error("home refresh failed: %s", exc)
                return
            if not rows:
                respond(
                    {
                        "replace_original": True,
                        "text": "Ninguna sesión coincide con el filtro actual.",
                    }
                )
                return
            respond(
                {
                    "replace_original": True,
                    "blocks": sessions_blocks(rows, filt),
                    "text": f"{len(rows)} sesiones",
                }
            )

        _bg(work)

    @app.action("sess_refresh_continue")
    def _refresh_continue(
        ack: Ack, body: dict[str, Any], client: WebClient, logger: Logger
    ) -> None:
        ack()
        if not is_allowed(config, _user(body)):
            return
        try:
            payload = dict(json.loads(_value(body) or "{}"))
        except json.JSONDecodeError:
            payload = {}
        sid, instr = str(payload.get("sid") or ""), str(payload.get("instr") or "")
        channel, ts = _channel(body, config), _message_ts(body)
        if not sid or not channel or not ts:
            return

        def work() -> None:
            rows = sessions.list_all(all_projects=True)
            row = next((r for r in rows if r.get("sid") == sid or r.get("short") == sid[:8]), None)
            busy = bool(row and row.get("status") == "busy" and row.get("pid"))
            status = (
                "Sigue procesando..."
                if busy
                else "Ya no está busy. Puedes pedir la última respuesta."
            )
            try:
                client.chat_update(
                    channel=channel,
                    ts=ts,
                    text=f"`{sid[:8]}` {status}",
                    blocks=pending_blocks(sid, instr or "Nueva instrucción", status, busy=busy),
                )
            except Exception as exc:
                logger.error("refresh continue failed: %s", exc)

        _bg(work)
