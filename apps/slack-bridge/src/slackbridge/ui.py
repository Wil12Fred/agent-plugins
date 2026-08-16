"""Slack-side presentation of a running dispatch: live progress, then the final answer.

``stream_dispatch`` posts "⏳ Procesando…" immediately (instant confirmation that the
daemon is alive), then — for a live session — polls the transcript read-only and edits that
message as the answer grows. When the authoritative dispatch returns, the final answer is
posted as a NEW thread reply and the temporary message is deleted, so Slack raises a fresh
notification instead of silently editing an old one.

**Concurrency**: the poll thread and the final write both touch the same message. A
``Lock`` plus ``stop_flag`` guarantee the final write wins — the poller only updates while
holding the lock AND only when ``stop_flag`` is unset, and the final write sets the flag
before taking the lock. A slow poll that finishes late therefore cannot clobber the result.
"""

from __future__ import annotations

import time
from logging import Logger
from pathlib import Path
from threading import Event, Lock, Thread

from slack_sdk import WebClient

from slackbridge import claude, codex, sessions
from slackbridge.blocks import NOTIFY_CAP, pending_blocks, result_blocks

POLL_SECONDS = 3
STREAM_CAP = 2800
"""Characters shown while streaming; the final message carries the full answer."""
JOIN_SECONDS = 5
NEW_SESSION_WAIT = 240
STABLE_POLLS = 2
"""Consecutive unchanged polls before a new session's first answer counts as complete."""


def stream_dispatch(
    client: WebClient,
    channel: str,
    sid: str,
    instruction: str,
    logger: Logger,
    *,
    mention: str | None = None,
    thread_ts: str | None = None,
) -> None:
    """Dispatch ``instruction`` to ``sid`` while streaming progress into Slack."""
    short = sid[:8]
    ts: str | None = None
    try:
        response = client.chat_postMessage(
            channel=channel,
            text=f"⏳ `{short}` ▶️ {instruction[:80]}",
            blocks=pending_blocks(sid, instruction),
            thread_ts=thread_ts,
        )
        ts = response.get("ts")
    except Exception as exc:
        logger.error("postMessage failed: %s", exc)

    stop_flag = Event()
    lock = Lock()

    def poll(pending_ts: str) -> None:
        baseline = sessions.last_response(sid)
        while not stop_flag.wait(POLL_SECONDS):
            current = sessions.last_response(sid)  # read-only, outside the lock (can be slow)
            if not current or current == baseline:
                continue
            with lock:
                if stop_flag.is_set():  # the final write already happened
                    return
                try:
                    client.chat_update(
                        channel=channel,
                        ts=pending_ts,
                        text=f"⏳ `{short}`…\n{current[:STREAM_CAP]}",
                        blocks=pending_blocks(sid, instruction, current),
                    )
                except Exception as exc:
                    logger.error("stream update failed: %s", exc)

    poller = (
        Thread(target=poll, args=(ts,), daemon=True) if (ts and sessions.is_live(sid)) else None
    )
    if poller:
        poller.start()

    out = sessions.dispatch(f"{sid} {instruction}")

    stop_flag.set()
    if poller:
        poller.join(timeout=JOIN_SECONDS)

    final_text = (f"<@{mention}> " if mention else "") + out
    with lock:
        try:
            client.chat_postMessage(
                channel=channel,
                text=final_text[:NOTIFY_CAP],
                blocks=result_blocks(sid, final_text, instruction),
                thread_ts=thread_ts,
            )
        except Exception as exc:
            logger.error("final post failed: %s", exc)
            return
        if ts:
            try:
                client.chat_delete(channel=channel, ts=ts)
            except Exception as exc:
                logger.error("pending delete failed: %s", exc)


def open_new_session(
    client: WebClient,
    channel: str,
    parent_ts: str,
    prompt: str,
    user: str,
    logger: Logger,
    *,
    cwd: str,
    model: str = "",
    terminal: str = "auto",
) -> None:
    """Open a fresh LIVE terminal session and post its first answer in the thread.

    A terminal-backed session (rather than headless ``claude -p``) is opened on purpose:
    it shows up under ``sessions list --live`` and can be injected into later.
    """
    sid, where = claude.open_terminal_session(prompt, Path(cwd), model=model, terminal=terminal)
    head = f"🆕 `{sid[:8]}` en {where}" if sid else where
    ts: str | None = None
    try:
        ts = client.chat_postMessage(
            channel=channel, thread_ts=parent_ts, text=f"{head} · ⏳ procesando…"
        ).get("ts")
    except Exception as exc:
        logger.error("new-session post failed: %s", exc)
    if not sid:
        return

    answer, stable, deadline = "", 0, time.time() + NEW_SESSION_WAIT
    while time.time() < deadline:
        time.sleep(POLL_SECONDS)
        current = sessions.last_response(sid)
        if current and current != answer:
            answer, stable = current, 0
        elif current and current == answer:
            stable += 1
            if stable >= STABLE_POLLS:
                break

    final = answer or ("(sesión abierta; aún sin respuesta — usa 💬 Última o mira la terminal)")
    try:
        client.chat_postMessage(
            channel=channel,
            thread_ts=parent_ts,
            text=final[:NOTIFY_CAP],
            blocks=result_blocks(
                sid, f"🆕 *Sesión nueva* `{sid[:8]}` en {where}\n<@{user}> {final}"
            ),
        )
    except Exception as exc:
        logger.error("new-session final post failed: %s", exc)
        return
    if ts:
        try:
            client.chat_delete(channel=channel, ts=ts)
        except Exception as exc:
            logger.error("pending delete failed: %s", exc)


CODEX_PREFIX = "codex"
"""A top-level message opening with this word starts a **Codex** session.

The pre-CLI bridge had this in `codex_sessions.py --process-channel`; the port
kept `codex.create_session` but wired nothing to it, so every top-level message
opened a Claude session and there was no way to start a Codex one from Slack.
"""


def wants_codex(prompt: str) -> str | None:
    """The prompt with the routing word stripped, or ``None`` if it is not for Codex.

    Matches `Codex <text>` — a bare "codex" with nothing after it is a question
    about Codex, not an instruction for it.
    """
    head, _, rest = prompt.strip().partition(" ")
    if head.lower().rstrip(":,") != CODEX_PREFIX:
        return None
    body = rest.strip()
    return body or None


def open_new_codex_session(
    client: WebClient,
    channel: str,
    parent_ts: str,
    prompt: str,
    user: str,
    logger: Logger,
    *,
    cwd: str,
) -> None:
    """Start a Codex session and post its answer in the thread.

    Codex answers once and exits — there is no live terminal to inject into, so
    unlike the Claude path there is nothing to poll: `create_session` returns
    the answer and the thread id to resume with.
    """
    ts: str | None = None
    try:
        ts = client.chat_postMessage(
            channel=channel, thread_ts=parent_ts, text="🤖 Codex · ⏳ procesando…"
        ).get("ts")
    except Exception as exc:
        logger.error("codex pending post failed: %s", exc)

    code, thread_id, answer = codex.create_session(prompt, Path(cwd))
    sid = thread_id or ""
    head = f"🤖 `{sid[:8]}` Codex" if sid else "🤖 Codex"
    if code != 0 and not answer:
        answer = f"[error] codex exited {code}"

    try:
        client.chat_postMessage(
            channel=channel,
            thread_ts=parent_ts,
            text=answer[:NOTIFY_CAP],
            blocks=result_blocks(sid, f"{head}\n<@{user}> {answer}") if sid else None,
        )
    except Exception as exc:
        logger.error("codex final post failed: %s", exc)
        return
    if ts:
        try:
            client.chat_delete(channel=channel, ts=ts)
        except Exception as exc:
            logger.error("codex pending delete failed: %s", exc)
