"""Bolt listener registration.

``register(app, config)`` is pure wiring: it attaches handlers and starts nothing, so
``slackbridge app`` can build the App object (and the tests can import it) without opening
a socket or spawning a thread. Background workers belong to the entrypoint
(:func:`slackbridge.bolt_app.serve`), not to registration.

The sample handlers from the Bolt starter template (``sample_action``, ``sample_command``,
``sample_message``, ``sample_shortcut``, ``sample_view``) are deliberately NOT ported: they
were scaffolding, none of them is referenced by the manifest, and the ``sample_message``
regex matched every message in the workspace.
"""

from __future__ import annotations

from slack_bolt import App

from slackbridge.config import SlackConfig
from slackbridge.listeners import actions, commands, events, messages, views


def register(app: App, config: SlackConfig) -> None:
    """Attach every session handler to ``app``."""
    actions.register(app, config)
    commands.register(app, config)
    events.register(app, config)
    messages.register(app, config)
    views.register(app, config)


__all__ = ["register"]
