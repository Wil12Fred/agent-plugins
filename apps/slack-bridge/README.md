# slack-bridge

Drive Claude Code and Codex sessions from a private Slack channel. You reply in a
thread; the agent answers in that thread. Sessions survive, so you can pick one
up from your phone and keep going.

```bash
uvx --from apps/slack-bridge slackbridge health
uvx --from apps/slack-bridge slackbridge serve --confirm-prod-write
```

| Command | |
|---|---|
| `slackbridge serve` | the long-running Socket Mode listener |
| `slackbridge health` | is the Claude CLI answering, is Codex installed, how many sessions are live |
| `slackbridge check` | build the Bolt app offline and report the resolved configuration, without a network call |
| `slackbridge sessions …` | list, dispatch to, stop and close sessions from the terminal |
| `slackbridge service-unit` | print a `systemd --user` unit with **this machine's** real paths |

## Configuration

Everything comes from the environment, or from a `.env` the CLI loads at
startup. Real environment variables always win, so
`SLACK_CHANNEL_ID=… slackbridge serve` overrides the file without editing it.

### Required

| | |
|---|---|
| `SLACK_BOT_TOKEN` | `xoxb-…`. **The bridge posts with this one.** Reply parsing separates your instructions from the bridge's own posts by `bot_id`/`app_id`; posting as the user makes them indistinguishable and the parser re-ingests its own output in a loop. |
| `SLACK_APP_TOKEN` | `xapp-…` with `connections:write`, for Socket Mode |
| `SLACK_CHANNEL_ID` | the private session-control channel. Everything else is ignored |
| `SLACK_ALLOWED_USER_IDS` | Slack **user ids** allowed to drive sessions. An allowlist, not a channel check |

### Optional

| | Default | |
|---|---|---|
| `SLACK_TOKEN` | keyring | `xoxp-…`, your own identity. Only where the *user* must be the author — Slack lets each identity delete only what it wrote |
| `SLACK_KEYRING_SERVICE` / `SLACK_KEYRING_ACCOUNT` | `slack-user-oauth` / `$USER` | where the user token lives in the OS keyring |
| `SLACK_BOT_USER_ID` | — | the app's own user id, never allowlisted |
| `SLACK_PRIVATE_USER_ID` | — | fallback single-user allowlist, and the `<@id>` mention prefix |
| `SLACK_PRIVATE_EMAIL` | — | DM recipient when no channel is configured |
| `NEW_SESSION_CWD` | cwd | where a session started from Slack begins |
| `NEW_SESSION_TERMINAL` | `auto` | terminal to open for a new session |
| `SLACK_WATCHDOG_SECONDS` | `600` | how long before a silent session is reported stuck |
| `SLACK_RESUME_TIMEOUT` | `900` | how long a resumed turn may take |
| `CLAUDE_BIN` / `CODEX_BIN` | `claude` / `codex` | override when they are not on `PATH` — Codex in particular installs under a *versioned* nvm bin |
| `CLAUDE_HOME` / `CODEX_HOME` | `~/.claude` / `~/.codex` | where session state is read from |
| `SLACKBRIDGE_ENV_FILE` | project root `.env` | read configuration from somewhere else |

Required scopes for the bot token: `users:read.email`, `im:write`, `chat:write`,
`im:history`. `slackbridge check` verifies them without posting anything.

## What it needs from the machine

This is the part no environment variable fixes, and it is worth knowing before
you install it:

- **`tmux`**, to type into a running session.
- **`qdbus`** (KDE) for part of the session discovery, on Linux desktops.
- `claude` and/or `codex` on `PATH`, or pointed at by `CLAUDE_BIN`/`CODEX_BIN`.

It is written for a Linux workstation. On macOS the `tmux` path should work and
the desktop discovery will not; nobody has tried it. Saying so here rather than
letting you find out is the point.

## Install as a service

```bash
mkdir -p ~/.config/systemd/user
slackbridge service-unit > ~/.config/systemd/user/slackbridge.service
systemctl --user daemon-reload
systemctl --user enable --now slackbridge.service
journalctl --user -u slackbridge.service -f
```

The unit is **printed rather than shipped** so you read it before installing it,
and because it resolves paths on *this* machine. A unit copied from someone
else's starts and then cannot find either agent binary.

## Development

```bash
cd apps/slack-bridge
uv sync
uv run pytest        # 55 tests
uv run ruff check src tests
uv run mypy src      # strict
```

## Provenance

Extracted from a private repository where it ran as a sub-command of a larger
CLI. The port was mechanical, because the configuration was already
environment-driven: no hardcoded channel ids, user ids or paths anywhere in
~3,000 lines. What changed is that five shared helpers were vendored into
`core/` and trimmed to what a bridge needs — the write guard lost its
database-specific consequences and gained a `SESSION` one, which is what this
actually touches — and the values that *were* hardcoded (the keyring account,
the agent binaries, the resume timeout) became the variables above.
