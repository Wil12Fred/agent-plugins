"""Background health watchdog for the Socket Mode listener.

Posts a startup notice, then alerts the channel when the Claude backend stops answering
(the usual cause is the machine suspending and the CLI losing its login) and again when it
recovers. It probes BEFORE announcing, so "🟢 Listener iniciado" is an observation rather
than an assumption.

It can only report what a running process can see: if the listener itself is down, nothing
is posted at all. Silence after an expected answer therefore still means "check the
service" — the watchdog narrows the diagnosis, it does not replace it.
"""

from __future__ import annotations

import time
from logging import getLogger
from threading import Thread

from slack_sdk import WebClient

from slackbridge.sessions import health

logger = getLogger(__name__)

_started_at = time.time()


def uptime() -> str:
    """Process uptime as ``"2h 5m"`` / ``"5m"``."""
    seconds = int(time.time() - _started_at)
    hours, minutes = seconds // 3600, (seconds % 3600) // 60
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def _post(client: WebClient, channel: str, text: str) -> None:
    try:
        client.chat_postMessage(channel=channel, text=text)
    except Exception as exc:
        logger.error("watchdog post failed: %s", exc)


def start(client: WebClient, channel: str, *, interval: int = 600) -> None:
    """Start the watchdog thread. No-op when no channel is configured."""
    if not channel:
        logger.info("watchdog: no SLACK_CHANNEL_ID -> disabled")
        return

    def loop() -> None:
        previous_ok = bool(health().get("claude_ok"))
        _post(
            client,
            channel,
            "🟢 Listener iniciado — Claude operativo."
            if previous_ok
            else "🟠 Listener iniciado, pero Claude no responde (revisa `/login`).",
        )
        while True:
            time.sleep(interval)
            try:
                ok = bool(health().get("claude_ok"))
                if not ok and previous_ok:
                    _post(
                        client,
                        channel,
                        "⚠️ *Watchdog*: el CLI de Claude no responde "
                        "(¿deslogueado o caído tras suspensión?). Revisa `/login`.",
                    )
                elif ok and not previous_ok:
                    _post(client, channel, "🟢 *Watchdog*: Claude responde de nuevo.")
                previous_ok = ok
            except Exception:
                logger.exception("watchdog cycle failed")

    Thread(target=loop, daemon=True).start()
    logger.info("watchdog started (every %ss)", interval)
