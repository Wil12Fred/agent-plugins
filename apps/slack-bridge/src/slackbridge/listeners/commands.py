"""Slash commands.

``/sessions [all] [texto]``  list sessions as cards (live-only by default)
``/sessions health``         daemon status: uptime, both CLIs, live session count
``/process <sid> <instr>``   dispatch an instruction (Claude, else Codex)
``/process <sid>``           that session's last response — runs nothing
``/process <sid> stop|close|compact``  control words
``/new [opus|sonnet|haiku] <prompt>``  open a new live session

Every callback ``ack()``s inside Slack's 3-second budget and does the slow work on a
background thread; a dispatch can take minutes, and a late ack shows the user
"operation_timeout" even when the work succeeded.
"""

from __future__ import annotations

import threading
from logging import Logger
from typing import Any

from slack_bolt import Ack, App, Respond
from slack_sdk import WebClient

from slackbridge import sessions, watchdog
from slackbridge.access import is_allowed
from slackbridge.blocks import NOTIFY_CAP, result_blocks, sessions_blocks
from slackbridge.claude import open_terminal_session
from slackbridge.config import SlackConfig

HELP = (
    "*Comandos de sesiones*\n"
    "`/sessions [all] [texto]` — por defecto solo sesiones *vivas* (terminal abierto); "
    "`all`=incluye cerradas · `texto`=filtra por título/id\n"
    "`/process <sid> <instrucción>` — despacha a una sesión (Claude; si no, Codex)\n"
    "`/process <sid>` — muestra la *última respuesta* de esa sesión (sin ejecutar nada)\n"
    "`/process <sid> stop` — interrumpe la generación actual (ESC; tmux/Konsole/VS Code)\n"
    "`/process <sid> close` — cierra/termina el proceso de la sesión\n"
    "`/process <sid> compact` — compacta el contexto de la sesión (`/compact`)\n"
    "`/new [opus|sonnet|haiku] <prompt>` — crea una sesión Claude nueva (modelo opcional)\n"
    "`/sessions health` — estado del daemon (uptime, CLIs, sesiones vivas)"
)

ALL_KEYWORDS = frozenset({"all", "todos", "todas"})
LIVE_KEYWORDS = frozenset({"live", "active", "activas", "activos"})
RESERVED = ALL_KEYWORDS | LIVE_KEYWORDS
MODELS = frozenset({"opus", "sonnet", "haiku"})


def _bg(fn: Any) -> None:
    threading.Thread(target=fn, daemon=True).start()


def sessions_command(
    ack: Ack,
    command: dict[str, Any],
    respond: Respond,
    client: WebClient,
    logger: Logger,
    config: SlackConfig,
) -> None:
    """List sessions, or report daemon health."""
    ack()
    if not is_allowed(config, command.get("user_id")):
        respond("⛔ No autorizado.")
        return
    arg = (command.get("text") or "").strip()
    lowered = arg.lower()
    if lowered.startswith("help"):
        respond(HELP)
        return
    if lowered.startswith(("health", "salud")):

        def health_work() -> None:
            state = sessions.health()
            claude_icon = "🟢" if state["claude_ok"] else "🔴"
            codex_icon = "🟢" if state["codex_ok"] else "🔴"
            respond(
                f"*Salud del daemon* · uptime {watchdog.uptime()}\n"
                f"{claude_icon} Claude CLI · {codex_icon} Codex CLI · "
                f"🖥️ {state['live']} sesión(es) con terminal abierto"
            )

        _bg(health_work)
        return

    tokens = arg.split()
    all_flag = any(t.lower() in ALL_KEYWORDS for t in tokens)
    live_only = not all_flag  # default view = only sessions open in a terminal
    query = " ".join(t for t in tokens if t.lower() not in RESERVED)
    filt = {"all": all_flag, "live": live_only, "query": query}

    def work() -> None:
        rows = sessions.list_all(all_projects=True, live_only=live_only, query=query, limit=50)
        if not rows:
            if query:
                respond(f"Ninguna sesión coincide con _{query}_.")
            elif live_only:
                respond("No hay sesiones abiertas en un terminal ahora mismo.")
            else:
                respond("No hay sesiones.")
            return
        label = f"{len(rows)} sesiones" + (f" · _{query}_" if query else "")
        channel = command.get("channel_id")
        if not channel:
            respond({"blocks": sessions_blocks(rows, filt), "text": label})
            return
        try:
            client.chat_postMessage(channel=channel, blocks=sessions_blocks(rows, filt), text=label)
        except Exception as exc:
            logger.error("sessions chat_postMessage failed: %s", exc)
            respond({"blocks": sessions_blocks(rows, filt), "text": label})

    _bg(work)


def process_command(
    ack: Ack, command: dict[str, Any], respond: Respond, logger: Logger, config: SlackConfig
) -> None:
    """Dispatch an instruction to a session (or show its last response)."""
    text = (command.get("text") or "").strip()
    lone_id = len(text.split()) == 1
    ack("🔎 Buscando última respuesta…" if lone_id else "⏳ Procesando…")
    if not is_allowed(config, command.get("user_id")):
        respond("⛔ No autorizado.")
        return

    def work() -> None:
        if not text:
            respond(
                "Uso: `/process <sessionId> <instrucción>` — o solo `/process <sessionId>` "
                "para la última respuesta · `<sessionId> stop` · `<sessionId> close`."
            )
            return
        sid = text.split()[0]
        out = sessions.dispatch(text)
        respond({"text": out[:NOTIFY_CAP], "blocks": result_blocks(sid, out)})

    _bg(work)


def new_command(
    ack: Ack, command: dict[str, Any], respond: Respond, logger: Logger, config: SlackConfig
) -> None:
    """Open a new live Claude session in a terminal, optionally on a named model."""
    ack("⏳ Creando sesión…")
    if not is_allowed(config, command.get("user_id")):
        respond("⛔ No autorizado.")
        return
    raw = (command.get("text") or "").strip()
    parts = raw.split(None, 1)
    model = parts[0].lower() if parts and parts[0].lower() in MODELS else ""
    prompt = parts[1].strip() if (model and len(parts) > 1) else raw

    def work() -> None:
        if not prompt:
            respond("Uso: `/new [opus|sonnet|haiku] <prompt inicial>`")
            return
        sid, where = open_terminal_session(
            prompt, config.new_session_cwd, model=model, terminal=config.new_session_terminal
        )
        if not sid:
            respond(where[:3500])
            return
        respond(
            {
                "text": f"Sesión {sid[:8]} abierta en {where}",
                "blocks": result_blocks(
                    sid,
                    f"🆕 *Sesión nueva* `{sid[:8]}` abierta en *{where}* con tu consulta. "
                    "Sigue aquí o en la terminal.",
                ),
            }
        )

    _bg(work)


def register(app: App, config: SlackConfig) -> None:
    """Register ``/sessions``, ``/process`` and ``/new``."""

    @app.command("/sessions")
    def _sessions(
        ack: Ack, command: dict[str, Any], respond: Respond, client: WebClient, logger: Logger
    ) -> None:
        sessions_command(ack, command, respond, client, logger, config)

    @app.command("/process")
    def _process(ack: Ack, command: dict[str, Any], respond: Respond, logger: Logger) -> None:
        process_command(ack, command, respond, logger, config)

    @app.command("/new")
    def _new(ack: Ack, command: dict[str, Any], respond: Respond, logger: Logger) -> None:
        new_command(ack, command, respond, logger, config)
