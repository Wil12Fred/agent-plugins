"""``slackbridge`` — the Slack <-> Claude/Codex session bridge.

Two halves:

* **one-shot commands** — list sessions, read a session's last answer, dispatch an
  instruction, read or process the replies in a Slack thread, purge the control channel;
* **``slackbridge serve``** — the long-running Socket Mode listener that does all of that
  automatically as messages arrive. It is a service, not a one-shot: it does not return.

Operational facts that must not be lost:

* the bridge posts as the **bot** (``SLACK_BOT_TOKEN``, ``xoxb-…``), never as the user
  (``SLACK_TOKEN``, ``xoxp-…``). Reply processing filters the bridge's own messages by
  ``bot_id``; with a user token its posts look like the human's and poison the parsing;
* access is an **allowlist by Slack user id** (``SLACK_ALLOWED_USER_IDS``);
* a reply that is **just a session id** returns that session's last response; a reply whose
  **first token is a session id** sends the rest as an instruction, which becomes
  ``claude --resume <id> -p "<instruction>"`` for a closed session (or a keystroke
  injection for a live one);
* required bot scopes: ``users:read.email``, ``im:write``, ``chat:write``, ``im:history``.

Tokens resolve through :mod:`slackbridge.core.secrets` and are never printed; where one has to be
acknowledged at all it is rendered with :func:`slackbridge.core.secrets.redact`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer

from slackbridge import channel as channel_mod
from slackbridge import claude, codex, config, requirements, sessions
from slackbridge.api import SlackAPI
from slackbridge.blocks import result_blocks
from slackbridge.core.env import project_root
from slackbridge.core.errors import ConfigError, NotFoundError, ValidationError
from slackbridge.core.guard import Consequence, WriteIntent, check_write
from slackbridge.core.output import get_output
from slackbridge.core.secrets import redact
from slackbridge.replies import read_thread, resolve_instruction

app = typer.Typer(
    name="slackbridge",
    help="Slack <-> Claude/Codex bridge: sessions, thread dispatch, Socket Mode listener.",
    no_args_is_help=True,
)


@app.callback()
def _root(
    json_mode: Annotated[
        bool,
        typer.Option("--json", help="Emit exactly one JSON envelope on stdout."),
    ] = False,
    quiet: Annotated[bool, typer.Option("--quiet", help="Suppress progress on stderr.")] = False,
) -> None:
    """Options every command shares.

    `--json` is the contract a script or an agent depends on: exactly one
    envelope on stdout, `{"ok": ..., "data": ...}`, and an exit code that means
    something. It lives here rather than on each command because a flag that
    exists on some commands and not others is worse than one that exists on
    none — the caller cannot write a loop.

    This was missing from the first port, and no unit test could see it: the
    commands wrote JSON anyway when they had nothing human to print, so the
    output *looked* right while `ok`, `command` and the error class were absent
    from every one of them.
    """
    from slackbridge.core.output import Output, set_output

    set_output(Output(json_mode=json_mode, quiet=quiet))

sessions_app = typer.Typer(
    name="sessions",
    help="Claude/Codex sessions: list, read, dispatch, close.",
    no_args_is_help=True,
)
app.add_typer(sessions_app, name="sessions")

replies_app = typer.Typer(
    name="replies",
    help="Slack thread replies: read them, or dispatch them to a session.",
    no_args_is_help=True,
)
app.add_typer(replies_app, name="replies")

channel_app = typer.Typer(name="channel", help="Control-channel maintenance.", no_args_is_help=True)
app.add_typer(channel_app, name="channel")

DryRunOption = Annotated[
    bool, typer.Option("--dry-run", help="Rehearse: show what would run, change nothing.")
]
ConfirmOption = Annotated[
    bool, typer.Option("--confirm-prod-write", help="Actually perform the action.")
]
ChannelOption = Annotated[
    str | None, typer.Option("--channel", help="Slack channel id (default: SLACK_CHANNEL_ID).")
]

SESSION_COLUMNS = ["engine", "short", "status", "started", "title", "project"]


# --- sessions --------------------------------------------------------------
@sessions_app.command("list")
def list_sessions(
    engine: Annotated[str, typer.Option("--engine", help="claude | codex | all.")] = "all",
    live: Annotated[
        bool, typer.Option("--live", help="Only sessions open in a terminal (Claude only).")
    ] = False,
    all_projects: Annotated[
        bool, typer.Option("--all-projects/--this-project", help="Every project, or just this one.")
    ] = True,
    project: Annotated[
        Path | None, typer.Option("--project", help="Project directory for --this-project.")
    ] = None,
    query: Annotated[str, typer.Option("--query", help="Filter by title / id / project.")] = "",
    limit: Annotated[int, typer.Option("--limit", help="Max sessions per engine.")] = 10,
) -> None:
    """List Claude and Codex sessions with their opening prompt as the title.

    Claude rows come from ``claude agents --json --all``, which is authoritative: it
    reports ``busy``/``idle`` and the pid of the terminal running each session. Rows with
    no pid are closed sessions read from the transcripts — resumable, but not injectable.
    Codex rows never carry a pid: Codex publishes no process-to-session mapping, so
    ``--live`` excludes it by construction.
    """
    if engine not in ("all", "claude", "codex"):
        raise ValidationError(f"unknown engine {engine!r}; expected claude, codex or all")
    rows = sessions.list_all(
        project=project,
        all_projects=all_projects,
        live_only=live,
        engine=engine,
        limit=limit,
        query=query,
    )
    get_output().table(rows, columns=SESSION_COLUMNS, title="sessions")


@sessions_app.command("last")
def last(
    sid: Annotated[str, typer.Argument(help="Full session id or a unique prefix.")],
) -> None:
    """Print a session's last assistant response. Reads a transcript; runs nothing."""
    full = claude.resolve_sid(sid)
    if full:
        text = claude.last_response(full)
        if text:
            get_output().result({"engine": "claude", "sid": full, "response": text}, human=text)
            return
        raise NotFoundError(f"session {full[:8]} has no assistant response yet")

    session, error = codex.resolve_session(sid)
    if not session:
        raise NotFoundError(error or f"session not found: {sid!r}")
    text = session.last_response or ""
    if not text:
        raise NotFoundError(f"session {session.sid[:8]} has no assistant response yet")
    get_output().result({"engine": "codex", "sid": session.sid, "response": text}, human=text)


@sessions_app.command("send")
def send(
    sid: Annotated[str, typer.Argument(help="Full session id or a unique prefix.")],
    instruction: Annotated[list[str], typer.Argument(help="The instruction to send.")],
    fork: Annotated[
        bool, typer.Option("--fork", help="Branch a new session id (safe when the target is busy).")
    ] = False,
    wait: Annotated[
        int, typer.Option("--wait", help="Seconds to wait for a live session's new answer.")
    ] = claude.DEFAULT_WAIT,
    dry_run: DryRunOption = False,
    confirm: ConfirmOption = False,
) -> None:
    """Send an instruction to a session and print its answer.

    A live session is driven by injecting the text into its terminal (tmux / Konsole /
    the VS Code bridge) because ``claude --resume`` refuses a session that is still open.
    A closed session is resumed with ``claude --resume <id> -p`` from its own project
    directory. If the id is not a Claude session, Codex claims it.
    """
    text = " ".join(instruction).strip()
    if not text:
        raise ValidationError("empty instruction")
    intent = WriteIntent(
        consequence=Consequence.EXTERNAL,
        action=f"run an agent instruction ({text[:60]!r})",
        target=f"session {sid}",
    )
    if not check_write(intent, dry_run=dry_run, confirmed=confirm):
        get_output().result({"sid": sid, "instruction": text, "dry_run": True})
        return
    out = sessions.dispatch(f"{sid} {text}", wait_timeout=wait, fork=fork)
    get_output().result({"sid": sid, "response": out}, human=out)


@sessions_app.command("stop")
def stop(
    sid: Annotated[str, typer.Argument(help="Session id or prefix.")],
    dry_run: DryRunOption = False,
    confirm: ConfirmOption = False,
) -> None:
    """Interrupt a live session's current generation by sending ESC to its terminal."""
    intent = WriteIntent(
        # A session runs on this machine; nobody outside it sees the ESC.
        consequence=Consequence.LOCAL_PROCESS,
        action="interrupt the current generation",
        target=f"session {sid}",
    )
    if not check_write(intent, dry_run=dry_run, confirmed=confirm):
        # A --dry-run that prints nothing is indistinguishable from a crash to
        # a --json caller: the envelope is the contract, rehearsal included.
        get_output().result({"sid": sid, "would": intent.describe(), "stopped": False})
        return
    out = sessions.stop(sid)
    get_output().result({"sid": sid, "result": out}, human=out)


@sessions_app.command("close")
def close(
    sid: Annotated[str, typer.Argument(help="Session id or prefix.")],
    dry_run: DryRunOption = False,
    confirm: ConfirmOption = False,
) -> None:
    """Terminate a live session's process (SIGTERM, then SIGKILL). The terminal tab stays."""
    intent = WriteIntent(
        consequence=Consequence.LOCAL_PROCESS,
        action="terminate the session process",
        target=f"session {sid}",
    )
    if not check_write(intent, dry_run=dry_run, confirmed=confirm):
        get_output().result({"sid": sid, "would": intent.describe(), "closed": False})
        return
    out = sessions.close(sid)
    get_output().result({"sid": sid, "result": out}, human=out)


@sessions_app.command("new")
def new(
    prompt: Annotated[list[str], typer.Argument(help="Opening prompt for the session.")],
    model: Annotated[str, typer.Option("--model", help="opus | sonnet | haiku.")] = "",
    headless: Annotated[
        bool,
        typer.Option(
            "--headless",
            help="Create a headless `claude -p` session instead of opening a terminal.",
        ),
    ] = False,
    dry_run: DryRunOption = False,
    confirm: ConfirmOption = False,
) -> None:
    """Start a new Claude session.

    By default a real terminal is opened (VS Code tab through the bridge extension, else a
    Konsole window) so the session is *live*: it appears under ``--live`` and can be given
    follow-up instructions by injection. ``--headless`` runs ``claude -p`` instead, which
    answers once and can only be resumed later by id.
    """
    text = " ".join(prompt).strip()
    if not text:
        raise ValidationError("empty prompt")
    cfg = config.load()
    intent = WriteIntent(
        consequence=Consequence.LOCAL_PROCESS,
        action=f"start a {'headless' if headless else 'terminal'} Claude session",
        target=str(cfg.new_session_cwd),
    )
    if not check_write(intent, dry_run=dry_run, confirmed=confirm):
        get_output().result({"prompt": text, "would": intent.describe(), "started": False})
        return
    if headless:
        sid, answer = claude.create_headless(text, cfg.new_session_cwd, model)
        get_output().result({"sid": sid, "response": answer}, human=f"{sid}\n\n{answer}")
        return
    sid, where = claude.open_terminal_session(
        text, cfg.new_session_cwd, model=model, terminal=cfg.new_session_terminal
    )
    if not sid:
        raise ValidationError(where)
    get_output().result({"sid": sid, "where": where}, human=f"{sid} → {where}")


@sessions_app.command("notify")
def notify(
    channel: ChannelOption = None,
    live: Annotated[bool, typer.Option("--live", help="Only terminal-open sessions.")] = False,
    dry_run: DryRunOption = False,
    confirm: ConfirmOption = False,
) -> None:
    """Post the current session listing to Slack (the mobile-friendly one-line-per-session form).

    Replaces ``claude-sessions --slack``: a listing pushed to the phone, from which any
    reply in the resulting thread becomes an instruction for that session.
    """
    cfg = config.load()
    target = cfg.require_channel(channel)
    rows = sessions.list_all(all_projects=True, live_only=live, limit=25)
    body = (
        "\n\n".join(
            f"{'🟠' if r['status'] == 'busy' else '⚪'} *{r['status']}* `{r['short']}` · "
            f"{r['started']}\n{r['title']}"
            for r in rows
        )
        or "(sin sesiones)"
    )
    intent = WriteIntent(
        consequence=Consequence.EXTERNAL,
        action=f"post {len(rows)} session rows to Slack",
        target=target,
    )
    if not check_write(intent, dry_run=dry_run, confirmed=confirm):
        get_output().result({"channel": target, "rows": len(rows), "preview": body})
        return
    api = SlackAPI.bot(cfg)
    ts = api.post(target, f"{cfg.mention()}*Sesiones*\n{body}")
    get_output().result({"channel": target, "ts": ts, "rows": len(rows)})


# --- replies ---------------------------------------------------------------
@replies_app.command("read")
def read_replies(
    channel: ChannelOption = None,
    ts: Annotated[
        str | None, typer.Option("--ts", help="Parent message ts (default: newest thread).")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Max replies to fetch.")] = 50,
) -> None:
    """Show the human replies in a Slack thread, and which session they resolve to.

    Read-only: nothing is dispatched and nothing is posted. Use it to see what
    ``replies process`` would do.
    """
    cfg = config.load()
    api = SlackAPI.bot(cfg)
    batch = read_thread(api, cfg, channel=channel, thread_ts=ts, limit=limit)
    instruction = resolve_instruction(batch)
    get_output().result(
        {
            "channel": batch.channel,
            "thread_ts": batch.thread_ts,
            "parent": batch.parent_text[:200],
            "replies": batch.texts,
            "resolved_session": instruction.sid if instruction else None,
            "resolved_instruction": instruction.text if instruction else None,
        }
    )


@replies_app.command("process")
def process_replies(
    channel: ChannelOption = None,
    ts: Annotated[
        str | None, typer.Option("--ts", help="Parent message ts (default: newest thread).")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Max replies to fetch.")] = 50,
    dry_run: DryRunOption = False,
    confirm: ConfirmOption = False,
) -> None:
    """Dispatch a thread's replies to their session and post the answer back in the thread.

    This is the one-shot form of what ``serve`` does continuously. The session is taken
    from the parent message (the bridge writes the id at the start of everything it posts);
    failing that, from the first reply whose first token is a session id.
    """
    cfg = config.load()
    api = SlackAPI.bot(cfg)
    batch = read_thread(api, cfg, channel=channel, thread_ts=ts, limit=limit)
    instruction = resolve_instruction(batch)
    if instruction is None or not batch.thread_ts:
        get_output().result({"channel": batch.channel, "dispatched": False, "reason": "no replies"})
        return

    intent = WriteIntent(
        consequence=Consequence.EXTERNAL,
        action=f"dispatch {len(batch.texts)} Slack reply(ies) and post the answer",
        target=f"session {instruction.sid} in {batch.channel}",
    )
    if not check_write(intent, dry_run=dry_run, confirmed=confirm):
        get_output().result(
            {
                "channel": batch.channel,
                "thread_ts": batch.thread_ts,
                "session": instruction.sid,
                "instruction": instruction.text,
                "dry_run": True,
            }
        )
        return

    if not claude.resolve_sid(instruction.sid) and not codex.resolve_session(instruction.sid)[0]:
        message = f"No se encontró la sesión: '{instruction.sid}'"
        api.post(batch.channel, message, thread_ts=batch.thread_ts)
        raise NotFoundError(message)

    out = sessions.dispatch(f"{instruction.sid} {instruction.text}".strip())
    body = f"{cfg.mention()}{out}"
    posted = api.post(
        batch.channel,
        body[:2900],
        thread_ts=batch.thread_ts,
        blocks=result_blocks(instruction.sid, body),
    )
    get_output().result(
        {
            "session": instruction.sid,
            "thread_ts": batch.thread_ts,
            "posted_ts": posted,
            "response": out,
        }
    )


# --- channel ---------------------------------------------------------------
@channel_app.command("clean")
def clean(
    channel: ChannelOption = None,
    dry_run: DryRunOption = False,
    confirm: ConfirmOption = False,
) -> None:
    """Delete every message and thread reply in the control channel.

    DESTRUCTIVE and irreversible; only meant for the private test channel that fills up
    with bridge output. Thread replies are removed before their parents (a deleted parent
    orphans its replies), and each message is attempted with the bot identity and then the
    user identity, because Slack only lets an identity delete what it authored.
    """
    cfg = config.load()
    target = cfg.require_channel(channel)
    api = SlackAPI.bot(cfg)
    timestamps = channel_mod.collect_timestamps(api, target)

    intent = WriteIntent(
        consequence=Consequence.EXTERNAL,
        action=f"delete {len(timestamps)} Slack messages",
        target=target,
    )
    if not check_write(intent, dry_run=dry_run, confirmed=confirm):
        get_output().result({"channel": target, "messages": len(timestamps), "deleted": 0})
        return

    clients = [api]
    if cfg.user_token:
        clients.append(SlackAPI.user(cfg))
    result = channel_mod.purge(target, timestamps, clients)
    get_output().result({"channel": target, **result})


# --- service ---------------------------------------------------------------
@app.command("health")
def health() -> None:
    """Probe both backends: is the Claude CLI answering, is Codex installed, how many live sessions.

    A ``claude_ok`` of false is what a post-suspend logout looks like — the watchdog alerts
    the channel on exactly this flip.

    Also reports the external programs the bridge shells out to, so "why did my
    reply do nothing" has an answer before you go looking in the logs.
    """
    results = requirements.check()
    payload = dict(sessions.health())
    payload["requirements"] = [r.as_dict() for r in results]
    payload["can_start"] = not requirements.blocking(results)
    get_output().result(payload)


@app.command("check")
def check() -> None:
    """Build the Bolt app offline and report the resolved configuration.

    Verifies the listener would start — tokens present, handlers registered — **without
    opening a socket or calling Slack**. Tokens are only ever shown redacted.
    """
    from slackbridge.bolt_app import build_app

    cfg = config.load()
    bolt, _ = build_app(cfg)
    listener_count = len(bolt._listeners)
    get_output().result(
        {
            "bot_token": redact(cfg.bot_token),
            "app_token": redact(cfg.app_token),
            "user_token": redact(cfg.user_token),
            "channel_id": cfg.channel_id or None,
            "allowlisted_users": len(cfg.allowed_user_ids),
            "bot_user_id": cfg.bot_user_id or None,
            "new_session_cwd": str(cfg.new_session_cwd),
            "required_scopes": list(config.REQUIRED_BOT_SCOPES),
            "listeners_registered": listener_count,
        }
    )


@app.command("serve")
def serve(
    log_level: Annotated[
        str, typer.Option("--log-level", help="DEBUG | INFO | WARNING | ERROR.")
    ] = "INFO",
    dry_run: DryRunOption = False,
    confirm: ConfirmOption = False,
) -> None:
    """Run the Socket Mode listener until interrupted (long-running service).

    While it runs, every message in the control channel is acted on: a thread reply is
    dispatched to that thread's session, a top-level message opens a new session. Starting
    the service IS the authorisation — individual dispatches are not re-confirmed — which
    is why the confirmation is required here, once, at start-up. It also keeps the MCP
    bridge from ever handing an agent a command that never returns.

    ``--dry-run`` builds the app and stops before connecting (same check as
    ``slackbridge check``). Install it as a ``systemd --user`` unit;
    ``slackbridge service-unit`` prints the unit.

    **It refuses to start when a required program is missing.** The bridge shells
    out, and before this check it found that out one message at a time: connected,
    reported healthy, then failed on the first dispatch because ``tmux`` was not
    installed — indistinguishable, from Slack, from the agent ignoring you.
    Missing *optional* programs do not block; they are announced, because a
    reduced capability nobody was told about is the same failure one level down.
    """
    from slackbridge.bolt_app import build_app
    from slackbridge.bolt_app import serve as run_serve

    results = requirements.check()
    if blocked := requirements.blocking(results):
        raise ConfigError(
            "cannot start: "
            + ", ".join(r.requirement.name for r in blocked)
            + " not installed",
            detail="\n" + requirements.explain(results),
        )
    for result in requirements.missing(results):
        get_output().warn(
            f"{result.requirement.name} is not installed — {result.requirement.purpose}. "
            f"Install: {result.requirement.install}"
        )

    cfg = config.load()
    intent = WriteIntent(
        consequence=Consequence.EXTERNAL,
        action="run the Slack listener, dispatching every channel message to a session",
        target=cfg.require_channel(),
    )
    if not check_write(intent, dry_run=dry_run, confirmed=confirm):
        build_app(cfg)  # prove the wiring is sound without opening the socket
        get_output().result({"channel": cfg.channel_id, "would_serve": True, "connected": False})
        return

    level = getattr(logging, log_level.upper(), logging.INFO)
    get_output().info("starting Socket Mode listener (Ctrl-C to stop)")
    run_serve(cfg, log_level=level)


@app.command("service-unit")
def service_unit(
    binary_path: Annotated[
        str, typer.Option("--binary", help="Absolute path of the `slackbridge` entry point.")
    ] = "",
    unit_name: Annotated[
        str, typer.Option("--unit-name", help="Name of the systemd unit to print.")
    ] = "slackbridge.service",
) -> None:
    """Print the ``systemd --user`` unit that runs ``slackbridge serve``.

    Emitted rather than written, so you read it before installing it.

    It resolves the real paths on *this* machine, which is the whole reason it is
    a command and not a file in the repository: `claude` usually lives in
    `~/.local/bin`, but `codex` is installed under a **versioned** nvm node bin,
    and a minimal systemd environment has neither on its `PATH`. A unit copied
    from someone else's machine starts and then cannot find either binary.
    """
    import shutil

    root = project_root()
    binary = binary_path or shutil.which("slackbridge") or str(root / ".venv/bin/slackbridge")
    codex_bin = shutil.which("codex")
    path_entries = ["%h/.local/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    if codex_bin:
        codex_dir = str(Path(codex_bin).parent)
        if codex_dir not in path_entries:
            path_entries.insert(1, codex_dir)
    unit = f"""# systemd --user unit for the Slack Socket Mode listener.
# Install (one-time):
#   mkdir -p ~/.config/systemd/user
#   slackbridge service-unit > ~/.config/systemd/user/{unit_name}
#   systemctl --user daemon-reload
#   systemctl --user enable --now {unit_name}
# Inspect:
#   systemctl --user status {unit_name}
#   journalctl --user -u {unit_name} -f
[Unit]
Description=Slack Socket Mode listener (dispatch thread replies to Claude/Codex)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# The CLI loads the .env itself, so no EnvironmentFile is needed. The claude and codex
# CLIs must be reachable: claude is in ~/.local/bin, codex under a versioned nvm node bin.
Environment=PATH={":".join(path_entries)}
WorkingDirectory={root}
ExecStart={binary} serve --confirm-prod-write
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""
    out = get_output()
    if out.json_mode:
        out.result({"unit": unit})
        return
    typer.echo(unit)  # verbatim: Rich would re-wrap the unit and break the paths


def main() -> None:
    """Entry point. Loads the `.env`, then renders our own errors as messages.

    The `.env` comes first because several options default from the
    environment, so reading it after parsing would be too late to change them.

    The exception handler is the other half, and it is not cosmetic. Without it
    a missing `tmux` — or a missing channel id — reaches the terminal as a
    syntax-highlighted traceback pointing at `cli.py`, which reads as *this tool
    is broken* rather than *you need to install something*. Our own errors carry
    the message, the fix and an exit code; anything else is a real bug and keeps
    its traceback.
    """
    from slackbridge.core.env import load_env_file
    from slackbridge.core.errors import BridgeError

    load_env_file()
    try:
        app()
    except BridgeError as exc:
        get_output().failure(exc)
        raise SystemExit(exc.exit_code) from None
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        raise SystemExit(130) from None


if __name__ == "__main__":  # pragma: no cover
    main()
