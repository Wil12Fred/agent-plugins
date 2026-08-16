"""App Home: an always-available session dashboard.

Published whenever an allowlisted user opens the bot's Home tab; anyone else sees a
refusal. The list is fetched on each open, and the in-view 🔄 Refrescar button republishes
it (handled in :mod:`slackbridge.listeners.actions`).
"""

from __future__ import annotations

from logging import Logger
from typing import Any

from slack_bolt import App
from slack_sdk import WebClient

from slackbridge import sessions
from slackbridge.access import is_allowed
from slackbridge.blocks import home_view
from slackbridge.config import SlackConfig


def register(app: App, config: SlackConfig) -> None:
    """Register the ``app_home_opened`` handler."""

    @app.event("app_home_opened")
    def _home(client: WebClient, event: dict[str, Any], logger: Logger) -> None:
        if event.get("tab") != "home":
            return
        user = event.get("user", "")
        try:
            if not is_allowed(config, user):
                client.views_publish(
                    user_id=user,
                    view={
                        "type": "home",
                        "blocks": [
                            {
                                "type": "section",
                                "text": {"type": "mrkdwn", "text": "⛔ No autorizado."},
                            }
                        ],
                    },
                )
                return
            rows = sessions.list_all(all_projects=True, live_only=True)
            client.views_publish(user_id=user, view=home_view(rows))
        except Exception as exc:
            logger.error("Error publishing home tab: %s", exc)
