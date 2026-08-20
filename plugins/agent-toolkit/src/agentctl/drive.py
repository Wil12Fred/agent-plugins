"""Put files in a Google Drive folder, from a plan rather than by hand.

The shape this exists for: a review deck covers twenty tasks, each task needs a
few of its slides plus some images, and whoever picks up a task should find
exactly that in a folder with the task's name — not the whole deck and a link to
find the rest themselves.

So the input is a plan, not a command line. One JSON document says which slides
of which decks belong to which task, and what else goes with them; running it
produces `<ticket>/<task>/` under a folder you name, containing a real `.pptx`
cut to those slides and the files listed beside it.

Three properties that make it safe to run twice, which matters because the first
run is never the last:

* **Folders are found before they are created.** Re-running does not produce
  `Task 1`, `Task 1 (1)`, `Task 1 (2)`.
* **A file that is already there is updated, not duplicated.** Drive is happy to
  hold two files with the same name in one folder, and that is how a reader ends
  up opening the stale one.
* **Nothing is written without ``confirm``.** A dry run reports the whole tree it
  would build, because the cost of getting a shared drive wrong is somebody
  else's confusion, not your own.

Authentication is a token, from ``GOOGLE_OAUTH_TOKEN`` or from ``gcloud auth
print-access-token``. It needs the Drive scope, which a plain ``gcloud auth
login`` does **not** grant — the refusal says so and gives the flag.

Everything here is stdlib: Drive is a REST API and `urllib` speaks it. That keeps
this plugin dependency-free, which is the reason it can be copied into a project
without dragging a client library along.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentctl.errors import ApiError, ConfigError, NotFoundError, UsageError, ValidationError

API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"
TOKEN_ENV = "GOOGLE_OAUTH_TOKEN"

SHARED = {"supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
"""Without both of these, a shared drive answers as if it were empty.

That is the failure worth naming: the call succeeds, returns no files, and the
tool cheerfully creates a second copy of a folder that already exists.
"""


def token() -> str:
    """A Drive-scoped access token, or a refusal that says how to get one."""
    from_env = os.environ.get(TOKEN_ENV)
    if from_env:
        return from_env
    try:
        done = subprocess.run(
            ["gcloud", "auth", "print-access-token"], capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        raise ConfigError(
            "no access token", detail=f"set {TOKEN_ENV}, or install gcloud and authenticate"
        ) from exc
    if done.returncode != 0 or not done.stdout.strip():
        raise ConfigError(
            "gcloud has no access token",
            detail="run `gcloud auth login --enable-gdrive-access --update-adc`",
        )
    return done.stdout.strip()


def folder_id(value: str) -> str:
    """Accept a folder id or any Drive URL that contains one."""
    match = re.search(r"/folders/([A-Za-z0-9_-]{10,})", value) or re.search(
        r"[?&]id=([A-Za-z0-9_-]{10,})", value
    )
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", value):
        return value
    raise UsageError(f"not a Drive folder id or URL: {value!r}")


def _call(
    method: str, url: str, tok: str, *, body: bytes | None = None, ctype: str | None = None
) -> Any:
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Authorization", f"Bearer {tok}")
    if ctype:
        request.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        if exc.code in (401, 403):
            raise ConfigError(
                f"Drive refused the request ({exc.code})",
                detail=detail or "the token may lack the drive scope, or you cannot write here",
            ) from exc
        raise ApiError(f"Drive returned {exc.code}", detail=detail) from exc
    except urllib.error.URLError as exc:
        raise ApiError("could not reach Drive", detail=str(exc.reason)) from exc
    return json.loads(payload) if payload else {}


def find_child(parent: str, name: str, tok: str, *, folder: bool) -> str | None:
    """The id of a child with this exact name, or None."""
    escaped = name.replace("\\", "\\\\").replace("'", "\\'")
    query = f"'{parent}' in parents and name = '{escaped}' and trashed = false"
    if folder:
        query += f" and mimeType = '{FOLDER_MIME}'"
    params = {"q": query, "fields": "files(id,name)", "pageSize": "10", **SHARED}
    found = _call("GET", f"{API}/files?{urllib.parse.urlencode(params)}", tok).get("files", [])
    return found[0]["id"] if found else None


def ensure_folder(parent: str, name: str, tok: str) -> tuple[str, bool]:
    """`(id, created)` — found first, created only if absent."""
    existing = find_child(parent, name, tok, folder=True)
    if existing:
        return existing, False
    body = json.dumps({"name": name, "mimeType": FOLDER_MIME, "parents": [parent]}).encode()
    params = urllib.parse.urlencode({"fields": "id", **SHARED})
    made = _call("POST", f"{API}/files?{params}", tok, body=body, ctype="application/json")
    created_id: str = made["id"]
    return created_id, True


def upload(path: Path, parent: str, tok: str, *, name: str | None = None) -> tuple[str, str]:
    """Upload or replace one file. Returns `(id, "created"|"updated")`."""
    if not path.is_file():
        raise NotFoundError(f"not a file: {path}")
    label = name or path.name
    mime = mimetypes.guess_type(label)[0] or "application/octet-stream"
    data = path.read_bytes()

    existing = find_child(parent, label, tok, folder=False)
    if existing:
        params = urllib.parse.urlencode({"uploadType": "media", "fields": "id", **SHARED})
        _call("PATCH", f"{UPLOAD}/files/{existing}?{params}", tok, body=data, ctype=mime)
        return existing, "updated"

    boundary = uuid.uuid4().hex
    meta = json.dumps({"name": label, "parents": [parent]}).encode()
    body = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
        + meta
        + f"\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n".encode()
        + data
        + f"\r\n--{boundary}--\r\n".encode()
    )
    params = urllib.parse.urlencode({"uploadType": "multipart", "fields": "id", **SHARED})
    created = _call(
        "POST",
        f"{UPLOAD}/files?{params}",
        tok,
        body=body,
        ctype=f"multipart/related; boundary={boundary}",
    )
    return created["id"], "created"


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #


@dataclass
class Source:
    path: Path
    pages: list[int]


@dataclass
class Task:
    name: str
    sources: list[Source] = field(default_factory=list)
    attachments: list[Path] = field(default_factory=list)


@dataclass
class Plan:
    ticket: str
    drive: str
    tasks: list[Task]


def load_plan(path: Path) -> Plan:
    """Read and validate the plan.

    Two spellings are accepted per task because both are natural to write: a
    list of ``sources``, each a deck and its pages; or the flat ``pptPath`` plus
    ``pages`` when a task only draws on one deck. Everything else is refused
    with the key that is wrong — a plan that half-parses uploads half a tree.
    """
    if not path.is_file():
        raise NotFoundError(f"not a file: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{path} is not valid JSON", detail=str(exc)) from exc

    for key in ("ticketName", "drivePath", "tasks"):
        if key not in raw:
            raise ValidationError(
                f"the plan has no {key!r}",
                detail="required keys: ticketName, drivePath, tasks",
            )
    if not isinstance(raw["tasks"], list) or not raw["tasks"]:
        raise ValidationError("'tasks' must be a non-empty list")

    tasks: list[Task] = []
    for index, entry in enumerate(raw["tasks"], start=1):
        name = entry.get("taskName")
        if not name:
            raise ValidationError(f"task {index} has no 'taskName'")
        sources: list[Source] = []
        shorthand = (
            [{"pptPath": entry["pptPath"], "pages": entry.get("pages")}]
            if entry.get("pptPath")
            else []
        )
        for src in entry.get("sources") or shorthand:
            deck = Path(str(src["pptPath"])).expanduser()
            if not deck.is_file():
                raise NotFoundError(f"task {name!r}: no such deck: {deck}")
            pages = src.get("pages") or []
            if not pages:
                raise ValidationError(f"task {name!r}: {deck.name} has no 'pages'")
            sources.append(Source(deck, [int(p) for p in pages]))
        attachments = []
        for item in entry.get("attachments") or []:
            candidate = Path(str(item)).expanduser()
            if not candidate.is_file():
                raise NotFoundError(f"task {name!r}: no such attachment: {candidate}")
            attachments.append(candidate)
        if not sources and not attachments:
            raise ValidationError(f"task {name!r} would upload nothing")
        tasks.append(Task(str(name), sources, attachments))

    return Plan(str(raw["ticketName"]), str(raw["drivePath"]), tasks)


def deliver(plan: Plan, work_dir: Path, *, confirm: bool = False) -> dict[str, object]:
    """Build `<ticket>/<task>/` under the plan's folder and fill it.

    Without ``confirm`` nothing is written and the whole tree is reported, decks
    included — the split runs either way, because a plan that names a slide the
    deck does not have should fail before anything reaches a shared drive, not
    halfway through.
    """
    from agentctl import pptxsplit

    work_dir.mkdir(parents=True, exist_ok=True)
    root = folder_id(plan.drive)
    tok = token() if confirm else ""

    planned: list[dict[str, object]] = []
    ticket_id = ""
    if confirm:
        ticket_id, created = ensure_folder(root, plan.ticket, tok)
        planned.append(
            {"folder": plan.ticket, "id": ticket_id, "action": "created" if created else "found"}
        )
    else:
        planned.append({"folder": plan.ticket, "action": "would create or reuse"})

    for task in plan.tasks:
        files: list[dict[str, object]] = []
        for source in task.sources:
            safe = re.sub(r"[^\w.-]+", "-", task.name).strip("-")
            cut = work_dir / f"{safe}--{source.path.stem[:40]}.pptx"
            result = pptxsplit.split(source.path, cut, source.pages, overwrite=True)
            files.append(
                {
                    "name": cut.name,
                    "from": source.path.name,
                    "slides": source.pages,
                    "bytes": result["bytes"],
                }
            )
        for item in task.attachments:
            files.append({"name": item.name, "from": str(item), "bytes": item.stat().st_size})

        entry: dict[str, object] = {"task": task.name, "files": files}
        if confirm:
            task_id, created = ensure_folder(ticket_id, task.name, tok)
            entry["id"] = task_id
            entry["folder_action"] = "created" if created else "found"
            uploaded = []
            for spec in files:
                is_split = "slides" in spec
                local = work_dir / str(spec["name"]) if is_split else Path(str(spec["from"]))
                file_id, action = upload(local, task_id, tok, name=str(spec["name"]))
                uploaded.append({"name": spec["name"], "id": file_id, "action": action})
            entry["uploaded"] = uploaded
        planned.append(entry)

    file_count = 0
    for entry in planned:
        listed = entry.get("files")
        if isinstance(listed, list):
            file_count += len(listed)

    return {
        "ticket": plan.ticket,
        "drive_folder": root,
        "confirmed": confirm,
        "tasks": len(plan.tasks),
        "files": file_count,
        "tree": planned,
        "url": f"https://drive.google.com/drive/folders/{ticket_id}" if ticket_id else None,
    }
