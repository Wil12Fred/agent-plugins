"""Slack <-> Claude/Codex session bridge.

Drives local Claude Code and Codex sessions from a private Slack channel: list them, read
their last answer, send an instruction, and run the Socket Mode listener that does all of
that automatically when a message arrives.

Layout:

``config``   env/secret resolution (bot vs user token, allowlist, control channel)
``api``      the Slack Web API calls the bridge makes
``access``   the allowlist and the "never answer yourself" guards
``claude``   Claude sessions: transcripts, ``claude agents``, terminal injection, resume
``codex``    Codex sessions: transcripts, ``codex exec resume``
``sessions`` the one dispatch policy every entry point shares
``replies``  turning a Slack thread into an instruction for a session
``blocks``   Block Kit rendering
``ui``       streamed progress + final answer
``watchdog`` background health alerts
``listeners``/``bolt_app``  the Socket Mode app
``cli``      ``slackbridge``
"""
