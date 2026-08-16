"""Claude Code sessions: discovery, transcripts, and instruction delivery.

Ported from ``scripts/utils/sessions/list.py``. The knowledge worth keeping:

* ``claude agents --json --all`` is **authoritative**, not a heuristic: it returns
  ``{pid, cwd, kind, startedAt, sessionId, status}`` for every live session, which is the
  only reliable pid -> session mapping. The mtime scan over ``~/.claude/projects`` is the
  fallback for when the CLI is unavailable.
* A **live** session cannot be resumed. ``claude --resume`` on an open session answers
  "No conversation found", so a live session must be driven by injecting keystrokes into
  its terminal (tmux / Konsole D-Bus / the VS Code bridge) instead.
* ``claude --resume`` is **cwd-scoped**: it must run from the session's own project
  directory, which is read back from the transcript's ``cwd`` field.
* The transcript's ``ai-title`` record holds the real Claude-generated title; the opening
  user prompt (``history.jsonl`` display, or the first user message) is the fallback.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
"""The Claude Code entry point. Overridable: not everyone has it on `PATH`."""

CLAUDE_HOME = Path(os.environ.get("CLAUDE_HOME", "")) or Path.home() / ".claude"
PROJECTS = CLAUDE_HOME / "projects"
VSCODE_INBOX = CLAUDE_HOME / "vscode-inbox.jsonl"

AGENTS_TIMEOUT = 30
RESUME_TIMEOUT = int(os.environ.get("SLACK_RESUME_TIMEOUT", "900"))
DEFAULT_WAIT = 180
"""Seconds to wait for a live session's new answer after injecting an instruction."""

SLACK_REPLY_DIRECTIVE = "\n\n(Responde de forma concisa en español para enviar por Slack.)"


# --- transcripts -----------------------------------------------------------
def encode_project(path: Path) -> str:
    """Claude's on-disk project folder name: ``/a/b`` -> ``-a-b``."""
    return "-" + str(path.resolve()).lstrip("/").replace("/", "-")


def find_session(sid: str) -> Path | None:
    """Locate a transcript by full session id or short prefix, across every project."""
    if not sid or not PROJECTS.is_dir():
        return None
    for project in PROJECTS.iterdir():
        if not project.is_dir():
            continue
        exact = project / f"{sid}.jsonl"
        if exact.is_file():
            return exact
        # An ambiguous prefix is an error, not a silent pick — glob order is
        # arbitrary, and dispatching an instruction to the wrong session is
        # worse than refusing. The Codex sibling already refused; these two
        # disagreed on the exact safety property the incident was about.
        matches = sorted(project.glob(f"{sid}*.jsonl"))
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return None
    return None


def resolve_sid(sid: str) -> str | None:
    """Expand a session prefix to its full id, or ``None`` when nothing matches."""
    transcript = find_session(sid)
    return transcript.stem if transcript else None


def session_cwd(transcript: Path | None) -> str | None:
    """The project directory a session belongs to (``claude --resume`` is cwd-scoped)."""
    if not transcript or not transcript.is_file():
        return None
    try:
        for line in transcript.open(encoding="utf-8", errors="ignore"):
            if '"cwd"' not in line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("cwd"):
                return str(record["cwd"])
    except OSError:
        return None
    return None


def last_response(sid: str) -> str | None:
    """Text of the last assistant message in a session, or ``None``."""
    transcript = find_session(sid)
    if not transcript:
        return None
    latest: str | None = None
    for line in transcript.open(encoding="utf-8", errors="ignore"):
        if '"assistant"' not in line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        if record.get("type") != "assistant" and message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, list):
            text = "\n".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
        else:
            text = content.strip() if isinstance(content, str) else ""
        if text:
            latest = text  # keep overwriting -> ends on the last assistant text
    return latest


def ai_title(transcript: Path) -> str | None:
    """The Claude-generated title (last ``ai-title`` record), if the session has one."""
    if not transcript.is_file():
        return None
    title: str | None = None
    for line in transcript.open(encoding="utf-8", errors="ignore"):
        if '"ai-title"' not in line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") == "ai-title" and record.get("aiTitle"):
            title = str(record["aiTitle"])
    return title


def first_prompt(transcript: Path) -> str:
    """The opening user prompt — the title a human actually recognises the session by."""
    for line in transcript.open(encoding="utf-8", errors="ignore"):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") != "message":
            continue
        message = record.get("message") or {}
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
        text = (content or "").strip().replace("\n", " ") if isinstance(content, str) else ""
        if text and not text.startswith("<"):
            return text
    return ""


def history_titles() -> dict[str, str]:
    """``sessionId -> opening prompt`` from ``~/.claude/history.jsonl``."""
    titles: dict[str, str] = {}
    path = CLAUDE_HOME / "history.jsonl"
    if not path.exists():
        return titles
    for line in path.open(encoding="utf-8", errors="ignore"):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = record.get("sessionId")
        display = (record.get("display") or "").strip().replace("\n", " ")
        if sid and display and sid not in titles:
            titles[sid] = display
    return titles


SID_TOKEN_RE = re.compile(r"^[0-9a-f]{8}[0-9a-f-]*$", re.I)
"""A token may only be treated as a session id if it looks like one: >= 8 hex chars.

Defect found in the legacy ``list.py``: ``_first_session_token`` accepted **any** token
that prefix-matched a transcript filename, and :func:`find_session` globs ``<token>*``.
A one-letter word like "a" therefore matched some arbitrary session, so an ordinary Slack
sentence could be dispatched to a random session. Observed live on 2026-08-06: the reply
"Continuamos con la revisión de …" resolved to session ``a``.
"""


def first_session_token(text: str) -> str | None:
    """First whitespace token of ``text`` that resolves to a session.

    Tolerates a leading ``@mention`` and markdown decoration, so a Slack reply of
    ``` `019ee964` sigue ``` still finds the id — but only id-shaped tokens are considered
    (see :data:`SID_TOKEN_RE`), so ordinary prose never resolves to a session by accident.
    """
    for token in (text or "").split():
        for candidate in (token, token.strip("`*_<>()[]:.,…")):
            if candidate and SID_TOKEN_RE.match(candidate) and find_session(candidate):
                return candidate
    return None


# --- live sessions ---------------------------------------------------------
def agents() -> list[dict[str, Any]]:
    """Live sessions from ``claude agents --json --all``; ``[]`` if the CLI is unavailable."""
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "agents", "--json", "--all"],
            capture_output=True,
            text=True,
            timeout=AGENTS_TIMEOUT,
            check=False,
        )
        data = json.loads(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        return list(data)
    return list(data.get("sessions") or data.get("agents") or [])


def live_pid(full_sid: str) -> int | None:
    """PID of the terminal running ``full_sid``, or ``None`` when the session is closed."""
    for agent in agents():
        if agent.get("sessionId") == full_sid and agent.get("pid"):
            return int(agent["pid"])
    return None


def is_busy(full_sid: str) -> bool:
    """True while the session is generating (``status == "busy"``)."""
    return any(a.get("sessionId") == full_sid and a.get("status") == "busy" for a in agents())


def _ancestors(pid: int) -> list[int]:
    """``pid`` and its ancestors, walked through ``/proc/<pid>/stat``."""
    chain, current = [pid], pid
    for _ in range(25):
        try:
            with open(f"/proc/{current}/stat") as handle:
                ppid = int(handle.read().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            break
        if ppid <= 1:
            break
        chain.append(ppid)
        current = ppid
    return chain


def _comm(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _tmux_target(family: set[int]) -> str | None:
    if not shutil.which("tmux"):
        return None
    panes = subprocess.run(
        [
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{pane_pid} #{session_name}:#{window_index}.#{pane_index}",
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    for line in panes.splitlines():
        pane_pid, _, target = line.partition(" ")
        if pane_pid.isdigit() and int(pane_pid) in family:
            return target
    return None


def _konsole_session(family: set[int]) -> tuple[str, str, str] | None:
    """``(qdbus binary, service, session path)`` of the Konsole tab running the session."""
    qbin = shutil.which("qdbus6") or shutil.which("qdbus")
    if not qbin:
        return None
    services = [
        s
        for s in subprocess.run([qbin], capture_output=True, text=True, check=False).stdout.split()
        if "org.kde.konsole" in s
    ]
    for service in services:
        listing = subprocess.run(
            [qbin, service], capture_output=True, text=True, check=False
        ).stdout
        for path in (line for line in listing.splitlines() if line.startswith("/Sessions/")):
            pids = []
            for method in ("foregroundProcessId", "processId"):
                value = subprocess.run(
                    [qbin, service, path, f"org.kde.konsole.Session.{method}"],
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout.strip()
                if value.isdigit():
                    pids.append(int(value))
            if any(pid in family for pid in pids):
                return qbin, service, path
    return None


def _vscode_write(payload: dict[str, Any]) -> None:
    VSCODE_INBOX.parent.mkdir(parents=True, exist_ok=True)
    with VSCODE_INBOX.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def send_live(pid: int, message: str, *, dry_run: bool = False) -> tuple[bool, str]:
    """Type ``message`` + Enter into the terminal running ``pid``.

    Routes by host terminal, in order of reliability: tmux ``send-keys`` (precise,
    focus-independent, Wayland-safe), Konsole D-Bus ``sendText`` (no focus needed), then
    the VS Code bridge extension, which is the only way to reach an integrated terminal
    tab because the OS exposes no handle for it.

    The message is collapsed to a single line first: an embedded newline inserts a line in
    the TUI instead of submitting the prompt.
    """
    family = set(_ancestors(pid))
    message = " ".join(message.split())

    target = _tmux_target(family)
    if target:
        if dry_run:
            return True, f"tmux send-keys -t {target}"
        subprocess.run(["tmux", "send-keys", "-t", target, "-l", "--", message], check=False)
        subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], check=False)
        return True, f"tmux:{target}"

    konsole = _konsole_session(family)
    if konsole:
        qbin, service, path = konsole
        if dry_run:
            return True, f"konsole sendText {service}{path}"
        send = [qbin, service, path, "org.kde.konsole.Session.sendText"]
        subprocess.run([*send, message], check=False)
        subprocess.run([*send, "\r"], check=False)
        return True, f"konsole:{service}{path}"

    if "code" in {_comm(p) for p in family}:
        if dry_run:
            return True, f"vscode bridge -> {VSCODE_INBOX} (extension matches the tab by pid)"
        _vscode_write({"claude_pid": pid, "text": message})
        return True, "vscode:bridge (needs the claude-terminal-bridge extension + window reloaded)"

    hosts = sorted({_comm(p) for p in family})
    return False, (
        f"no injection route for pid {pid} (host: {hosts}). Run the session in tmux/Konsole, "
        "or install the VS Code bridge extension."
    )


def interrupt_live(pid: int) -> tuple[bool, str]:
    """Send ESC to stop the current generation (tmux / Konsole / VS Code bridge)."""
    family = set(_ancestors(pid))
    target = _tmux_target(family)
    if target:
        subprocess.run(["tmux", "send-keys", "-t", target, "Escape"], check=False)
        return True, f"tmux:{target} (ESC)"
    konsole = _konsole_session(family)
    if konsole:
        qbin, service, path = konsole
        subprocess.run(
            [qbin, service, path, "org.kde.konsole.Session.sendText", "\x1b"], check=False
        )
        return True, f"konsole:{service}{path} (ESC)"
    if "code" in {_comm(p) for p in family}:
        _vscode_write({"claude_pid": pid, "interrupt": True})
        return True, "vscode:bridge (ESC; needs the updated bridge extension + window reloaded)"
    return False, "stop no soportado para este terminal (usa tmux/Konsole/VS Code-bridge)"


def close_live(pid: int) -> tuple[bool, str]:
    """Terminate a live session's process (SIGTERM, then SIGKILL). The tab stays open."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True, f"pid {pid} ya no existía"
    except PermissionError:
        return False, f"sin permiso para terminar pid {pid}"
    for _ in range(20):  # up to ~2s for a graceful exit
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True, f"pid {pid} terminado (SIGTERM)"
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True, f"pid {pid} terminado"
    return True, f"pid {pid} forzado (SIGKILL)"


# --- failure classification ------------------------------------------------
def classify_issue(text: str) -> str | None:
    """Turn a known CLI failure into an explanation, in Spanish, for the Slack thread.

    These three cover almost every "the bridge stopped answering" report:
    a suspended machine dropping the login, the account's session limit, and the
    "No conversation found" that really means "that session is still open".
    """
    body = text or ""
    if "Please run /login" in body or "Invalid authentication credentials" in body:
        return (
            "⚠️ La sesión necesita re-login (`/login`) — probablemente la PC se suspendió "
            "y se cayó la autenticación. Re-loguea y reintenta."
        )
    lowered = body.lower()
    if "session limit" in lowered or "hit your session limit" in lowered:
        reset = body.split("resets", 1)[1].strip() if "resets" in body else ""
        when = f" (se restablece {reset})" if reset else ""
        return (
            f"⚠️ Se alcanzó el límite de sesión de la cuenta{when}. "
            "No es un fallo del script; reintenta cuando se restablezca."
        )
    if "No conversation found" in body:
        return (
            "⚠️ No pude responder a esa sesión con `claude --resume`: o está **abierta/viva** "
            "(no se puede resumir mientras corre — hay que inyectar en su terminal), o se "
            "cerró/crasheó (p.ej. por suspensión). Reábrela, usa otra sesión, o `--fork`."
        )
    return None


# --- dispatch --------------------------------------------------------------
def wait_for_new_response(
    full_sid: str,
    prev: str | None,
    *,
    timeout: int = DEFAULT_WAIT,
    interval: int = 3,
) -> str:
    """Poll the transcript until a NEW last response appears and the session is idle."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(interval)
        current = last_response(full_sid)
        if current and current != prev and not is_busy(full_sid):
            return current
    return last_response(full_sid) or "(sin respuesta nueva — ¿la sesión recibió la instrucción?)"


def resume(
    full_sid: str,
    instruction: str,
    *,
    fork: bool = False,
    slack_ready: bool = True,
    timeout: int = RESUME_TIMEOUT,
) -> tuple[int, str]:
    """Run ``claude --resume <sid> -p <instruction>`` from the session's own project.

    ``--fork-session`` branches a new session id, which is the only way to drive a target
    that is still open in a terminal without injecting into it.
    """
    transcript = find_session(full_sid)
    prompt = instruction + (SLACK_REPLY_DIRECTIVE if slack_ready else "")
    command = [
        CLAUDE_BIN,
        "--resume",
        full_sid,
        *(["--fork-session"] if fork else []),
        "-p",
        prompt,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=session_cwd(transcript),
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError:
        return 3, "claude CLI no está en el PATH"
    except subprocess.TimeoutExpired:
        return 3, f"claude excedió el tiempo ({timeout}s)"
    out = (result.stdout or "").strip() or (result.stderr or "").strip() or "(sin salida)"
    issue = classify_issue(out)
    return (0 if issue else result.returncode), (issue or out)


def dispatch(
    sid: str,
    instruction: str,
    *,
    wait_timeout: int = DEFAULT_WAIT,
    fork: bool = False,
) -> tuple[int, str]:
    """Route an instruction to a Claude session and return ``(exit_code, text)``.

    * live session  -> inject into its terminal, then wait for the new answer;
    * closed session -> ``claude --resume`` from the session's project;
    * bare id (empty instruction) -> that session's last response.

    No Slack I/O happens here, which is what lets the CLI, the slash command and the
    message listener all share one dispatch policy.
    Exit codes: 0 ok · 2 session not found · 3 runtime error.
    """
    transcript = find_session(sid)
    if not transcript:
        return 2, f"No se encontró la sesión: '{sid}'"
    full_sid = transcript.stem
    if not instruction:
        return 0, last_response(full_sid) or "(no hay respuesta previa en esa sesión)"

    pid = live_pid(full_sid)
    if pid:
        previous = last_response(full_sid)
        ok, info = send_live(pid, instruction)
        if not ok:
            return 3, f"No pude inyectar en la sesión viva: {info}"
        answer = wait_for_new_response(full_sid, previous, timeout=wait_timeout)
        return 0, classify_issue(answer) or answer
    return resume(full_sid, instruction, fork=fork)


def _vscode_running() -> bool:
    try:
        return (
            subprocess.run(
                ["pgrep", "-f", "/usr/share/code/code"], capture_output=True, check=False
            ).returncode
            == 0
        )
    except OSError:
        return False


def open_terminal_session(
    prompt: str,
    cwd: Path,
    *,
    model: str = "",
    terminal: str = "auto",
) -> tuple[str | None, str]:
    """Open a NEW **live** Claude session in a real terminal; returns ``(sid, where)``.

    The session id is generated up front and passed as ``--session-id`` so the caller can
    report it immediately, before Claude has written anything. ``terminal="auto"`` targets
    a new VS Code tab through the bridge extension when VS Code is running, else a Konsole
    window — both of which :func:`send_live` can inject into later, unlike a headless
    ``claude -p`` session.
    """
    sid = str(uuid.uuid4())
    command = f"claude --permission-mode auto --session-id {sid}"
    if model:
        command += f" --model {shlex.quote(model)}"
    command += f" {shlex.quote(prompt)}"

    target = terminal
    if target == "auto":
        target = "vscode" if _vscode_running() else "konsole"
    try:
        if target == "vscode":
            _vscode_write(
                {
                    "new_terminal": True,
                    "name": f"claude {sid[:8]}",
                    "cwd": str(cwd),
                    "text": command,
                }
            )
            return sid, "VS Code (nueva pestaña)"
        subprocess.Popen(
            ["konsole", "--workdir", str(cwd), "-e", "bash", "-lc", f"{command}; exec bash"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        return None, f"No pude abrir terminal ({target}): {exc}"
    return sid, "Konsole"


def create_headless(prompt: str, cwd: Path, model: str = "") -> tuple[str | None, str]:
    """Create a fresh headless session (``claude -p``); returns ``(session_id, answer)``.

    Headless sessions have no terminal, so they never show as live — they can only be
    resumed later by id. Use :func:`open_terminal_session` when the session should be
    visible and injectable.
    """
    command = [CLAUDE_BIN, "-p", prompt, "--output-format", "json"]
    if model:
        command += ["--model", model]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=RESUME_TIMEOUT,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        data = json.loads(result.stdout or "{}")
    except (json.JSONDecodeError, OSError, subprocess.TimeoutExpired) as exc:
        return None, f"No pude crear la sesión: {exc}"
    return data.get("session_id"), (data.get("result") or "(sin salida)")


# --- listing ---------------------------------------------------------------
def shorten(text: str, limit: int = 100) -> str:
    """Collapse whitespace and truncate — a title is one line in a table or a Slack card."""
    clean = " ".join((text or "").split())
    return clean[: limit - 3] + "..." if len(clean) > limit else clean


def _row(
    sid: str,
    *,
    status: str,
    started: str,
    title: str,
    project: str,
    pid: int | None,
) -> dict[str, Any]:
    return {
        "engine": "claude",
        "sid": sid,
        "short": sid[:8],
        "status": status,
        "started": started,
        "title": shorten(title),
        "project": project,
        "pid": pid,
    }


def _title_for(sid: str, cwd: str, titles: dict[str, str]) -> str:
    transcript = PROJECTS / (encode_project(Path(cwd)) + f"/{sid}.jsonl") if cwd else None
    if transcript and transcript.is_file():
        title = ai_title(transcript) or first_prompt(transcript)
        if title:
            return title
    return titles.get(sid) or "(untitled)"


def list_sessions(
    *,
    project: Path | None = None,
    all_projects: bool = False,
    limit: int = 10,
    include_closed: bool = True,
) -> list[dict[str, Any]]:
    """Claude sessions as uniform rows.

    Live rows come from ``claude agents`` (authoritative status + pid); closed sessions
    are read from the transcripts and reported with ``pid=None``, so a caller can tell
    "open in a terminal, injectable" from "saved, resumable only".
    """
    titles = history_titles()
    rows: list[dict[str, Any]] = []
    want = str((project or Path.cwd()).resolve())

    for agent in sorted(agents(), key=lambda a: a.get("startedAt", 0), reverse=True):
        if agent.get("kind") != "interactive":
            continue
        cwd = agent.get("cwd", "")
        if not all_projects and cwd != want:
            continue
        started_ms = agent.get("startedAt", 0) / 1000
        sid = agent.get("sessionId", "?")
        rows.append(
            _row(
                sid,
                status=agent.get("status", "?"),
                started=(
                    time.strftime("%m-%d %H:%M", time.localtime(started_ms)) if started_ms else "?"
                ),
                title=_title_for(sid, cwd, titles),
                project=cwd,
                pid=agent.get("pid"),
            )
        )

    if not include_closed:
        return rows

    live_ids = {row["sid"] for row in rows}
    directories = (
        (
            [p for p in PROJECTS.iterdir() if p.is_dir()]
            if all_projects
            else [PROJECTS / encode_project(Path(want))]
        )
        if PROJECTS.is_dir()
        else []
    )
    files = [f for d in directories if d.is_dir() for f in d.glob("*.jsonl")]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    added = 0
    for path in files:
        if added >= limit:
            break
        sid = path.stem
        if sid in live_ids:
            continue
        rows.append(
            _row(
                sid,
                status="idle",
                started=time.strftime("%m-%d %H:%M", time.localtime(path.stat().st_mtime)),
                title=titles.get(sid) or ai_title(path) or first_prompt(path) or "(untitled)",
                project="",
                pid=None,
            )
        )
        added += 1
    return rows
