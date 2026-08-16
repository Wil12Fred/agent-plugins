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
| `slackbridge health` | is the Claude CLI answering, is Codex installed, how many sessions are live, and is every required program present |
| `slackbridge check` | build the Bolt app offline and report the resolved configuration, without a network call |
| `slackbridge sessions …` | list, dispatch to, stop and close sessions from the terminal |
| `slackbridge service-unit` | print a `systemd --user` unit with **this machine's** real paths |

## Configuration

Every command takes a global `--json` (before the subcommand:
`slackbridge --json health`) and answers with exactly one envelope, so a script
gets structured output and a meaningful exit code from the same binary a person
reads.

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

**Checked at startup, not discovered later.** `serve` refuses to start when
something required is missing, and names the install command:

```
$ slackbridge serve --confirm-prod-write
error cannot start: tmux not installed

  REQUIRED: tmux — typing a reply into a session that is already running
      install: apt install tmux · pacman -S tmux · brew install tmux
  optional: qdbus — finding sessions running in a KDE terminal window …
      install: part of Plasma: apt install qdbus-qt6 · pacman -S qt6-tools
```

| | | |
|---|---|---|
| `claude` **or** `codex` | required | there is nothing to drive without one. `CLAUDE_BIN`/`CODEX_BIN` override the lookup |
| `tmux` | required | typing a reply into a session that is already running |
| `qdbus` (`qdbus6`) | optional | finding sessions in a KDE terminal window. Without it only tmux sessions are discoverable — it starts and says so |

The required/optional split is the point. Before this check existed the bridge
connected to Slack, reported itself healthy, and failed on the *first dispatch* —
which from Slack is indistinguishable from the agent ignoring you. But refusing
over `qdbus` would be worse than the problem: nobody needs KDE to drive a session
from their phone, so that one warns and continues.

`slackbridge health` reports the same table any time, so "why did my reply do
nothing" has an answer before you go reading logs.

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
uv run pytest                    # 68 unit tests, offline
uv run pytest -m integration     # 10 e2e against this machine
uv run ruff check src tests
uv run mypy src                  # strict
```

The e2e are opt-in because they depend on the machine: they run the installed
binary the way systemd would, read the real agent state, and drive a genuinely
stripped `PATH` to prove the refusal. A missing token or binary **skips with a
reason** rather than failing — a red that only means "not configured here"
teaches people to ignore red.

They exist because the bridge shipped with 55 unit tests and not one that
touched a real binary: every green run proved the parsing and said nothing about
whether the thing could start. The first run found that `--json` did not exist at
all.

## Provenance

Extracted from a private repository where it ran as a sub-command of a larger
CLI. The port was mechanical, because the configuration was already
environment-driven: no hardcoded channel ids, user ids or paths anywhere in
~3,000 lines. What changed is that five shared helpers were vendored into
`core/` and trimmed to what a bridge needs — the write guard lost its
database-specific consequences and gained a `SESSION` one, which is what this
actually touches — and the values that *were* hardcoded (the keyring account,
the agent binaries, the resume timeout) became the variables above.
