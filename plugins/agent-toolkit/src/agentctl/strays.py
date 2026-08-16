"""Executables that leaked out of the code directory.

A task needs a script, the script gets written next to the notes for that task,
it works, and it stays there — untested, called by nothing, invisible to every
gate, and reimplemented three tasks later by somebody who could not have known it
existed.

Finding them is arithmetic. Deciding what to do with each is not, so this command
does the first half only and hands the second to a human or an agent, with the
three outcomes named:

- **port it** — it could serve a second task, so it belongs in the code
  directory as a tested command;
- **promote it** — it drives a flow end to end, so it belongs in a test suite;
- **declare it** — genuinely single-use, so it stays where it is *with its reason
  written down*.

"Leave it where it is" is not on that list, and the absence is the point: an
undeclared script and a declared one look identical on disk, and only one of them
is a decision.

**Not one extension.** A check that reads only `.py` misses the same debt written
in shell — measured on a real repository, that blind spot kept a script alive
whose `.env` path pointed at a machine the author no longer owned. Nothing had
failed, because nothing had looked.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from agentctl.detect import SKIP_DIRECTORIES, code_roots, content_roots

EXECUTABLE_SUFFIXES = frozenset(
    {".py", ".sh", ".bash", ".zsh", ".mjs", ".cjs", ".js", ".rb", ".pl"}
)
"""Extensions that are somebody's script. Extend it rather than narrowing it."""

DECLARATION_FILES = (
    "docs/refactor/migration-manifest.csv",
    ".agentctl-allow",
    "docs/one-off-scripts.txt",
)
"""Where a project may declare a script as a deliberate exception.

Checked in order; the first that exists wins. A CSV is read for a `kept`-style
status column, anything else as one path per line. Both ignore `#` comments, so a
declaration can carry its reason beside it — which is the only thing that makes
it a declaration rather than a list.
"""


@dataclass(frozen=True)
class Stray:
    path: str
    suffix: str
    lines: int
    declared: bool
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "suffix": self.suffix,
            "lines": self.lines,
            "declared": self.declared,
            "reason": self.reason,
        }


def _declarations(root: Path) -> dict[str, str | None]:
    """Declared exceptions as `path -> reason`. Empty when nothing declares any."""
    for relative in DECLARATION_FILES:
        candidate = root / relative
        if not candidate.is_file():
            continue
        text = candidate.read_text(encoding="utf-8", errors="ignore")
        if candidate.suffix == ".csv":
            declared: dict[str, str | None] = {}
            for row in csv.DictReader(text.splitlines()):
                status = (row.get("status") or "").strip().lower()
                path = (row.get("source_path") or "").strip()
                if path and status in {"kept", "keep", "declared"}:
                    declared[path] = (row.get("note") or "").strip() or None
            return declared
        entries: dict[str, str | None] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            path, _, reason = line.partition("#")
            entries[path.strip()] = reason.strip() or None
        return entries
    return {}


def _walk(root: Path, directory: str) -> Iterable[Path]:
    base = root / directory
    if not base.is_dir():
        return
    stack = [base]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except (PermissionError, OSError):
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name not in SKIP_DIRECTORIES:
                    stack.append(entry)
            elif entry.suffix in EXECUTABLE_SUFFIXES:
                yield entry


def _line_count(path: Path) -> int:
    try:
        return sum(1 for _ in path.open("rb"))
    except OSError:
        return 0


def find(root: Path, *, search: Iterable[str] | None = None) -> list[Stray]:
    """Executables outside the code directories, declared or not.

    Args:
        root: the repository to inspect.
        search: directories to sweep. Defaults to the repository's content
            roots — the directories holding prose and data. Not "everything that
            is not code": when the code root is the repository root itself, that
            reading sweeps `controllers/` and `migrations/` as strays.

    Returns an empty list when the code root is unknown. That is deliberate:
    without knowing where code lives, every file is outside it, and a check that
    reports the entire repository has told you nothing.
    """
    code = set(code_roots(root))
    if not code:
        return []

    if search is None:
        search = list(content_roots(root))

    declared = _declarations(root)
    found: list[Stray] = []
    for directory in search:
        for path in sorted(_walk(root, directory)):
            relative = str(path.relative_to(root))
            found.append(
                Stray(
                    path=relative,
                    suffix=path.suffix,
                    lines=_line_count(path),
                    declared=relative in declared,
                    reason=declared.get(relative),
                )
            )
    return found


def undeclared(strays: Iterable[Stray]) -> list[Stray]:
    """The ones that are a decision nobody has made."""
    return [stray for stray in strays if not stray.declared]
