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
| `slackbridge routes check` / `routes example` | validate the alert routes as `serve` would (`--workers`: what the next hand-over would do); print a commented `routes.toml` |
| `slackbridge items list` / `items set` | the ledger of alerts and questions handed to a worker, and what became of each |
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

## Alert routes

Besides the control channel, the bridge can watch team channels for alerts and hand each
one — and every reply in its thread — to a **worker session** it starts and owns. By default
the worker is an interactive Claude session in a **detached tmux session** named after it:
nothing opens on screen, and `tmux attach -t support-worker` shows it (Ctrl-b d detaches).
The bridge never writes to the watched channels, and never types into a session that merely
shares the worker's name. It keeps the worker in the route's permission mode, because a
worker someone Shift+Tab'd into `manual` stops on prompts nobody sees.

Routes live in `$XDG_CONFIG_HOME/slackbridge/routes.toml` (or `SLACKBRIDGE_ROUTES_FILE`).
[`routes.example.toml`](routes.example.toml) is the commented template, identical to
`slackbridge routes example`. Per route:

| key | | |
|---|---|---|
| `name`, `channels`, `session`, `cwd`, `instructions` | required | `cwd` absolute and existing; `session` is the worker's name — not a terminal session's |
| `trigger` | required unless every channel has its own | phrase making a top-level message an alert |
| `channel_triggers` | | `{ C… = "*" }`: per channel; `*` = every top-level human message is a question |
| `test_channels`, `ignore_users` | | the prompt says "test"; posts by these users are never routed |
| `from` | | Slack user ids: **only** they open items, and their thread replies are routed — the operator's own requests (the prompt says so). Must not overlap `ignore_users`; a post made through an app's API under their name (`bot_id` set) is not theirs and is ignored, so the worker never routes itself |
| `terminal` | `tmux` | `tmux` (detached tmux session, attach on demand) or `background` (`claude --bg`, for a machine without tmux) |
| `permission_mode` | `auto` | `acceptEdits`, `auto`, `bypassPermissions`, `manual`, `dontAsk`, `plan` |
| `permission_prompts` | `none` under `dontAsk` | `host` or `none` → `--permission-prompts` |
| `settings` | | absolute path of an existing file → `--settings` |
| `setting_sources` | | `user`/`project`/`local`, list or `"a,b"` → `--setting-sources`. Allow-lists **merge** across sources, so a user-level `Bash(*)` reaches the worker unless `user` is left out |
| `follow_thread_days`, `backfill_hours`, `seed_state` | `5`, `24`, — | thread follow window, catch-up window on start (0 = off), read-only seed of tracked threads |

**A channel a route claims belongs to it, the control channel included**: nothing there
opens a new session any more. Put the control channel in a route with `trigger = "*"` and
`from = [<your id>]` and your messages there become requests to the worker instead.

`serve` refuses to start on anything that would route silently wrong: an unknown mode, a
missing settings file, a channel two routes claim, a trigger for a channel the route does
not watch, two routes giving one worker different launch options. An unknown key is a
warning. Without the file, the legacy `SLACK_ALERTS_*` variables (see `config.py`) still
make one equivalent route named `env`, in `auto` mode; with it, they are ignored and a
warning names them.

**Before every hand-over, a tmux worker** (the default) is checked: the tmux session with
its name must run the Claude session id the state file recorded for it — anything else with
that name is not ours, and is neither typed into nor killed (the item is marked
`dispatch-error`). Its live mode is read from the pane's footer. Measured with Claude Code
2.1.332 and tmux 3.8:

| the worker is… | the bridge | because |
|---|---|---|
| not running | `tmux new-session -d -s <session> -c <cwd> claude <route flags> --resume <recorded sid> <prompt>` (`--session-id <new uuid>` the first time) | Claude submits a command-line prompt itself (busy 0.6 s after the launch; listed 1.2 s after a bare launch), so nothing waits on the TUI; an interactive `--resume` keeps the same session id |
| ours, in the route's mode | types the prompt in (`tmux send-keys`), idle or busy | text sent mid-turn is queued by Claude itself and answered after the turn |
| ours, in another mode, idle | `tmux kill-session`, then the launch above with `--resume` | the footer shows a Shift+Tab at once; the transcript only records it with the next prompt, and calls `manual` `"default"` |
| ours, in another mode, mid-turn | queues it; the drainer retries | killing it would lose the turn |
| launched seconds ago, not listed yet | queues it | a second launch would start a second worker |

On start, after the listener connects, every tmux worker that is not running is launched
(no prompt), and then the catch-up runs. The worker's session id and tmux session name are
kept in the alerts state file, so a relaunch resumes the same conversation.

**A background worker** (`terminal = "background"`) is found by name in
`claude agents --json --all` and its mode read from the last `"permissionMode"` of its
transcript:

| the worker is… | the bridge runs | because |
|---|---|---|
| not there | `claude --bg <route flags> [--resume <last sid>] <prompt>` | a resumed copy keeps the conversation |
| stopped, right mode | `claude --bg --resume <full sid> <prompt>` — **no flags** | any flag, even the same ones, starts a copy; a short id does too |
| alive and idle, right mode | `claude stop <id>`, then the same | resuming a live session starts a copy |
| in another (or unreadable) mode, or launched with other saved options | stop if alive, then `claude --bg <route flags> --resume <sid> <prompt>` | the copy has the history and the route's mode |
| busy | queue it (in the state file); a drainer delivers the queue as one prompt when idle | resuming a busy session starts a copy instead of delivering |

The worker records what it did with each item through the ledger CLI the prompt names:
`slackbridge items set <CHANNEL:TS> --status in-progress` when it takes one, then the final
status (`done`/`skipped` for a question; the tracker's status or `taken-by-other` for an
alert). On start the catch-up re-sends every item of its window whose thread is still empty
and whose ledger entry only the bridge ever wrote; one the worker touched, or with any
reply, is not. A live redelivery of a known item is never sent twice.

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
uv run pytest                    # unit tests, offline
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
