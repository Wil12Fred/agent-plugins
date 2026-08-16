"""Codex sessions: transcript parsing and ``codex exec resume`` dispatch.

Ported from ``scripts/utils/sessions/codex_sessions.py``. What the legacy script learned:

* Codex stores one JSONL per session under ``~/.codex/sessions`` (``CODEX_HOME``
  overrides the root). The session id is in the ``session_meta`` record and, redundantly,
  in the filename — the filename is the fallback when the record is missing.
* Transcripts mix two record shapes: ``event_msg`` (``user_message`` / ``agent_message`` /
  ``task_complete``) and ``response_item`` (``role`` + ``content`` blocks). Both must be
  read or the last answer comes back empty for half the sessions.
* The first user message is usually **not** the human's prompt: Codex injects
  ``<environment_context>``, ``<permissions instructions>``, the AGENTS.md preamble and so
  on first. :func:`_is_user_prompt` skips those so the listing shows a real title.
* There is **no process <-> session mapping** for Codex (unlike ``claude agents``), so
  liveness is an mtime heuristic and Codex rows are omitted from the "live" view.
* Resuming needs ``codex exec resume --all <sid> <prompt>`` run from the session's own
  ``cwd``; creating one needs ``codex exec --json`` so the new thread id can be parsed
  back out of the event stream (``thread.started``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
"""The Codex entry point. Overridable: it installs under a versioned nvm bin."""

CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
SESSIONS_DIR = CODEX_HOME / "sessions"

UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)

RESUME_TIMEOUT = 900
SLACK_REPLY_DIRECTIVE = "\n\n(Responde de forma concisa en español para enviar por Slack.)"

_TECHNICAL_PREFIXES = (
    "<environment_context>",
    "<permissions instructions>",
    "<collaboration_mode>",
    "<skills_instructions>",
    "<apps_instructions>",
    "<tools>",
    "# AGENTS.md instructions",
    "# AGENTS Guide",
)


@dataclass(frozen=True)
class CodexSession:
    """One parsed Codex transcript."""

    sid: str
    path: Path
    cwd: str | None
    started_at: str | None
    model: str | None
    originator: str | None
    first_user: str
    last_response: str
    mtime: float


def shorten(text: str, limit: int = 70) -> str:
    """Collapse whitespace and truncate for a one-line title."""
    clean = " ".join((text or "").split())
    return clean[: limit - 3] + "..." if len(clean) > limit else clean


def _is_user_prompt(text: str) -> bool:
    """False for Codex's injected context blocks, so the title is the human's prompt."""
    clean = (text or "").lstrip()
    return bool(clean) and not clean.startswith(_TECHNICAL_PREFIXES)


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = [
        item["text"]
        for item in content
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ]
    return "\n".join(p for p in parts if p).strip()


def parse_session(path: Path) -> CodexSession | None:
    """Parse one transcript; ``None`` when it is unreadable or has no session id."""
    match = UUID_RE.search(path.name)
    sid = match.group(1) if match else None
    cwd = started_at = model = originator = None
    first_user = last_response = ""

    try:
        with path.open(encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rtype = record.get("type")
                raw_payload = record.get("payload")
                payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}

                if rtype == "session_meta":
                    sid = payload.get("id") or sid
                    cwd = payload.get("cwd") or cwd
                    started_at = payload.get("timestamp") or started_at
                    originator = payload.get("originator") or originator
                    raw_git = payload.get("git")
                    git: dict[str, Any] = raw_git if isinstance(raw_git, dict) else {}
                    model = payload.get("model") or model or git.get("model")
                elif rtype == "turn_context":
                    cwd = payload.get("cwd") or cwd
                    model = payload.get("model") or model
                elif rtype == "event_msg":
                    ptype = payload.get("type")
                    if ptype == "user_message" and not first_user:
                        message = payload.get("message") or ""
                        if _is_user_prompt(message):
                            first_user = message
                    elif ptype == "agent_message":
                        last_response = payload.get("message") or last_response
                    elif ptype == "task_complete":
                        last_response = payload.get("last_agent_message") or last_response
                elif rtype == "response_item" and payload.get("type") == "message":
                    role = payload.get("role")
                    text = _content_text(payload.get("content"))
                    if role == "user" and not first_user and _is_user_prompt(text):
                        first_user = text
                    elif role == "assistant" and text:
                        last_response = text
    except OSError:
        return None

    if not sid:
        return None
    return CodexSession(
        sid=sid,
        path=path,
        cwd=cwd,
        started_at=started_at,
        model=model,
        originator=originator,
        first_user=first_user,
        last_response=last_response,
        mtime=path.stat().st_mtime,
    )


def load_sessions() -> list[CodexSession]:
    """Every parseable Codex session, newest first."""
    if not SESSIONS_DIR.is_dir():
        return []
    sessions = [s for p in SESSIONS_DIR.rglob("*.jsonl") if (s := parse_session(p))]
    sessions.sort(key=lambda s: s.mtime, reverse=True)
    return sessions


def resolve_session(
    sid_or_prefix: str, sessions: list[CodexSession] | None = None
) -> tuple[CodexSession | None, str | None]:
    """Resolve a full id or unique prefix; returns ``(session, error)``.

    An ambiguous prefix is an error, not a silent pick — dispatching an instruction to the
    wrong session is worse than refusing.
    """
    pool = load_sessions() if sessions is None else sessions
    lookup = sid_or_prefix.strip().lower()
    if not lookup:
        return None, "empty session id"
    exact = [s for s in pool if s.sid.lower() == lookup]
    if exact:
        return exact[0], None
    prefixed = [s for s in pool if s.sid.lower().startswith(lookup)]
    if len(prefixed) == 1:
        return prefixed[0], None
    if len(prefixed) > 1:
        choices = ", ".join(s.sid[:8] for s in prefixed[:6])
        return None, f"ambiguous session prefix '{sid_or_prefix}' ({choices})"
    return None, f"session not found: '{sid_or_prefix}'"


SID_TOKEN_RE = re.compile(r"^[0-9a-f]{8}[0-9a-f-]*$")
"""A session id is at least 8 hex characters. Shorter is a word, not an id."""


def first_session_token(text: str, sessions: list[CodexSession] | None = None) -> str | None:
    """First token of ``text`` that resolves to a Codex session.

    A token must *look like* a session id before it is allowed to resolve as
    one. Without that check any unique prefix matched, so an ordinary word
    made of hex letters — ``de``, ``cafe``, ``bed``, ``dada``, all common in
    Spanish — resolved, and the rest of the thread was dispatched as an
    instruction to whatever session it happened to hit.

    The Claude side has required this since the incident; this side did not,
    and `resolve_instruction` falls through to here, so the fix was only half
    applied.
    """
    pool = load_sessions() if sessions is None else sessions
    for token in (text or "").split():
        clean = token.strip("`*,;:()[]")
        if not SID_TOKEN_RE.match(clean):
            continue
        session, _ = resolve_session(clean, pool)
        if session:
            return clean
    return None


def classify_issue(text: str) -> str | None:
    """Map a known Codex failure to a Spanish explanation for the Slack thread."""
    lowered = (text or "").lower()
    if "not authenticated" in lowered or ("login" in lowered and "codex" in lowered):
        return "Codex necesita autenticación. Ejecuta `codex login` localmente y reintenta."
    if "session not found" in lowered or "no session" in lowered:
        return "No se pudo encontrar/resumir la sesión Codex indicada."
    return None


def resume(
    session: CodexSession,
    instruction: str,
    *,
    timeout: int = RESUME_TIMEOUT,
    dry_run: bool = False,
    slack_ready: bool = True,
) -> tuple[int, str]:
    """Run ``codex exec resume --all <sid> <prompt>`` from the session's own directory."""
    prompt = instruction.strip() + (SLACK_REPLY_DIRECTIVE if slack_ready else "")
    command = [CODEX_BIN, "exec", "resume", "--all", session.sid, prompt]
    cwd = Path(session.cwd).expanduser() if session.cwd else Path.cwd()
    if not cwd.is_dir():
        cwd = Path.cwd()
    if dry_run:
        return 0, f'DRY-RUN: {" ".join(command[:-1])} "{prompt}" (cwd={cwd})'
    if not shutil.which(CODEX_BIN):
        return 3, "[error] codex CLI not found on PATH"
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 3, f"[error] codex timed out ({timeout}s)"
    output = (result.stdout or "").strip() or (result.stderr or "").strip() or "(no output)"
    issue = classify_issue(output)
    return (0 if issue else result.returncode), (issue or output)


def parse_exec_json(stdout: str, stderr: str) -> tuple[str | None, str]:
    """Pull ``(thread_id, final message)`` out of a ``codex exec --json`` event stream."""
    thread_id: str | None = None
    final = ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started":
            thread_id = event.get("thread_id") or thread_id
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            final = item.get("text") or final
    return thread_id, (final.strip() or stderr.strip() or stdout.strip() or "(no output)")


def create_session(
    prompt: str,
    cwd: Path,
    *,
    timeout: int = RESUME_TIMEOUT,
    dry_run: bool = False,
    slack_ready: bool = True,
) -> tuple[int, str | None, str]:
    """Start a new Codex session; returns ``(exit_code, thread_id, answer)``."""
    final_prompt = prompt.strip() + (SLACK_REPLY_DIRECTIVE if slack_ready else "")
    command = [CODEX_BIN, "exec", "--json", final_prompt]
    if dry_run:
        return 0, None, f'DRY-RUN: {" ".join(command[:-1])} "{final_prompt}" (cwd={cwd})'
    if not shutil.which(CODEX_BIN):
        return 3, None, "[error] codex CLI not found on PATH"
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 3, None, f"[error] codex timed out ({timeout}s)"
    thread_id, output = parse_exec_json(result.stdout or "", result.stderr or "")
    issue = classify_issue(output)
    return (0 if issue else result.returncode), thread_id, (issue or output)


def list_sessions(
    *,
    project: Path | None = None,
    all_projects: bool = False,
    limit: int = 10,
    active_window: int = 10,
) -> list[dict[str, Any]]:
    """Codex sessions as uniform rows (same shape as :func:`slackbridge.claude.list_sessions`).

    ``pid`` is always ``None``: Codex exposes no process <-> session mapping, so a Codex row
    can never claim to be "live in a terminal". ``status`` is an mtime heuristic.
    """
    sessions = load_sessions()
    want = str((project or Path.cwd()).resolve())
    rows = [s for s in sessions if all_projects or s.cwd == want][:limit]
    now = time.time()
    return [
        {
            "engine": CODEX_BIN,
            "sid": s.sid,
            "short": s.sid[:8],
            "status": "busy" if now - s.mtime <= active_window else "idle",
            "started": time.strftime("%m-%d %H:%M", time.localtime(s.mtime)),
            "title": shorten(s.first_user or s.last_response or "(untitled)", 100),
            "project": s.cwd or "",
            "model": s.model or "",
            "pid": None,
        }
        for s in rows
    ]
