"""The Socket Mode listener: build the Bolt app, then run it.

``build_app()`` constructs the ``App`` and wires every handler **without opening a socket
or starting a thread**, so ``slackbridge app-check`` and the unit tests can exercise the
wiring offline. ``serve()`` is the only thing that connects.

Socket Mode needs two tokens with different jobs:

* ``SLACK_BOT_TOKEN`` (``xoxb-…``) — the app's identity for every Web API call. The bridge
  must post with this one: reply parsing separates the human's instructions from the
  bridge's own posts by ``bot_id``, and a user token would make its posts look like the
  human's;
* ``SLACK_APP_TOKEN`` (``xapp-…``) — the app-level token that opens the WebSocket. It
  authenticates the connection, not the messages.

Required bot scopes: ``users:read.email``, ``im:write``, ``chat:write``, ``im:history``
(``slackbridge.config.REQUIRED_BOT_SCOPES``). The ``manifest.json`` of the retired
standalone app also carried ``channels:history``/``groups:*`` for the private control
channel and ``commands`` for the three slash commands.
"""

from __future__ import annotations

import logging

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from slackbridge import watchdog
from slackbridge.config import SlackConfig, load
from slackbridge.listeners import register


def build_app(config: SlackConfig | None = None) -> tuple[App, SlackConfig]:
    """Build the Bolt app with every listener attached. Starts nothing.

    Slack's token check is skipped at construction time so the object can be built without
    a network round-trip; the token is still required and is validated on the first call.
    """
    resolved = config or load()
    app = App(token=resolved.require_bot_token(), token_verification_enabled=False)
    register(app, resolved)
    return app, resolved


def serve(config: SlackConfig | None = None, *, log_level: int = logging.INFO) -> None:
    """Run the listener until interrupted. Long-running: this call does not return.

    Starts the health watchdog first (background thread, owned by the entrypoint rather
    than by listener registration), then opens the Socket Mode connection.
    """
    logging.basicConfig(level=log_level)
    app, resolved = build_app(config)
    watchdog.start(app.client, resolved.channel_id, interval=resolved.watchdog_seconds)
    # slack_bolt ships py.typed but leaves SocketModeHandler.start()
    # unannotated, so strict mode rejects the one call that runs the listener.
    SocketModeHandler(app, resolved.require_app_token()).start()  # type: ignore[no-untyped-call]
