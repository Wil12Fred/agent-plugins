"""What practices has this repository actually adopted?

An auditor that reports rules the project never took on is noise, and noise is
how it teaches people to ignore it — including on the day it is right. So before
judging anything, establish which rules apply.

**Every answer is a file on disk, and the evidence travels with it.** Nothing
here infers a practice from a language, a framework or a directory name that
merely sounds right: a practice is present when something that could only exist
because somebody adopted it exists. A reader who disagrees with a verdict can
open the path that produced it.

That is the whole design, and it is the reason this is a command rather than a
paragraph in a prompt. A model asked "does this repo do spec-driven development?"
will answer confidently either way; a filesystem check answers with a path or
with nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRECTORIES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "dist",
        "build",
        "target",
        "vendor",
    }
)
"""Directories whose contents say nothing about what the project adopted."""

CODE_DIRECTORIES = ("src", "lib", "app", "packages", "cmd", "internal", "pkg")
"""Conventional homes for source, when a project keeps them in one."""

ROOT_MANIFESTS = (
    "package.json",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "composer.json",
    "Gemfile",
    "pyproject.toml",
    "setup.py",
)
"""A build manifest at the root means the code is at the root.

Measured against a real Express service: `controllers/`, `services/`, `models/`
and `routes/` sit directly in the repository root, with no `src/` anywhere. The
conventional-directory list answered "none found" — honest, but it left the
stray check unable to run on one of the commonest layouts there is.
"""

CONTENT_DIRECTORIES = ("docs", "doc", "specs", "knowledge", "notes", "wiki", "documentation")
"""Where prose and data live, and therefore where a script can hide."""


@dataclass(frozen=True)
class Practice:
    """One thing the repository either does or does not do.

    Attributes:
        name: stable identifier, cited by the auditor.
        adopted: whether evidence was found.
        evidence: the paths that proved it — empty exactly when not adopted.
        why: what this practice means, so a report can explain a skip.
    """

    name: str
    adopted: bool
    why: str
    evidence: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "practice": self.name,
            "adopted": self.adopted,
            "evidence": list(self.evidence),
            "why": self.why,
        }


def _exists(root: Path, *candidates: str) -> tuple[str, ...]:
    """Which of these relative paths exist. Order preserved, duplicates dropped."""
    found: list[str] = []
    for candidate in candidates:
        if (root / candidate).exists() and candidate not in found:
            found.append(candidate)
    return tuple(found)


def _iter_files(root: Path, *, limit: int = 20_000) -> Iterable[Path]:
    """Every file under `root`, skipping the directories that never carry evidence.

    Bounded on purpose: an unbounded walk over a monorepo with a checked-in
    `node_modules` turns a one-second answer into a minute, and the answer does
    not change. Hitting the limit is reported rather than silently truncated —
    see :func:`detect`.
    """
    seen = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except (PermissionError, OSError):
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name not in SKIP_DIRECTORIES and not entry.is_symlink():
                    stack.append(entry)
                continue
            seen += 1
            if seen > limit:
                return
            yield entry


def code_roots(root: Path) -> tuple[str, ...]:
    """Where this project keeps its source.

    Detected, not assumed, in two steps. A conventional directory wins; failing
    that, a build manifest at the root means the code *is* the root — that is
    the Express, Rails and Go convention, and refusing to recognise it makes the
    tool useless on a large share of real repositories.

    An empty answer means neither signal was found, and callers must read it as
    "unknown" rather than as "the root is the code root". A documentation
    repository has no manifest and no `src/`; treating it as code turns every
    markdown file into a false positive.
    """
    conventional = _exists(root, *CODE_DIRECTORIES)
    if conventional:
        return conventional
    if _exists(root, *ROOT_MANIFESTS):
        return (".",)
    return ()


def content_roots(root: Path) -> tuple[str, ...]:
    """Directories holding prose or data rather than source.

    This is where a script hides, and naming them explicitly is what makes the
    stray check precise in both layouts. The alternative — "every top-level
    directory that is not a code root" — sweeps `controllers/` and `migrations/`
    the moment the code root is the repository root itself.
    """
    code = set(code_roots(root))
    return tuple(item for item in _exists(root, *CONTENT_DIRECTORIES) if item not in code)


def _spec_driven(root: Path) -> Practice:
    evidence = _exists(
        root,
        ".specify",
        ".specify/memory/constitution.md",
        "openspec",
        ".kiro",
        "specs",
        "docs/specs",
    )
    # `specs/` alone is weak: plenty of projects have one holding OpenAPI files.
    # It counts only alongside a steering document, which is what makes the
    # practice a practice rather than a directory name.
    strong = {".specify", "openspec", ".kiro", ".specify/memory/constitution.md"}
    adopted = any(item in strong for item in evidence) or (
        bool(evidence) and bool(_exists(root, "AGENTS.md", "CLAUDE.md", "CONSTITUTION.md"))
    )
    return Practice(
        name="spec-driven",
        adopted=adopted,
        evidence=evidence if adopted else (),
        why=(
            "changes are specified before they are built, so a spec folder or a "
            "steering document is worth auditing for drift"
        ),
    )


def _debt_register(root: Path) -> Practice:
    evidence = tuple(
        str(path.relative_to(root))
        for path in _iter_files(root)
        if "baseline" in path.name and path.suffix in {".tsv", ".csv", ".json", ".txt"}
    )
    return Practice(
        name="debt-register",
        adopted=bool(evidence),
        evidence=evidence[:5],
        why="counted signals recorded against a ceiling, so a rise is a failure",
    )


def _agent_tooling(root: Path) -> Practice:
    evidence = _exists(
        root, ".claude", ".codex", ".agents", "skills", ".claude-plugin", "plugins", "AGENTS.md"
    )
    return Practice(
        name="agent-tooling",
        adopted=bool(evidence),
        evidence=evidence,
        why="the repository ships skills, subagents or plugins that can rot",
    )


def _verified_state(root: Path) -> Practice:
    evidence = tuple(
        str(path.relative_to(root))
        for path in _iter_files(root)
        if path.suffix == ".md" and "## Verified state" in _read(path)
    )
    return Practice(
        name="verified-state",
        adopted=bool(evidence),
        evidence=evidence[:5],
        why="documents claim live-system state and carry the commit they were measured against",
    )


def _declared_gates(root: Path) -> Practice:
    evidence = _exists(
        root,
        "Makefile",
        "justfile",
        "Taskfile.yml",
        ".github/workflows",
        ".gitlab-ci.yml",
        ".circleci/config.yml",
        "noxfile.py",
        "tox.ini",
    )
    return Practice(
        name="declared-gates",
        adopted=bool(evidence),
        evidence=evidence,
        why="the project states its own checks, which are the floor an audit starts from",
    )


def _content_roots(root: Path) -> Practice:
    """Directories that hold documentation or data rather than source.

    Only meaningful when the code lives somewhere identifiable: without a known
    code root, "everything that is not code" is the whole repository.
    """
    code = set(code_roots(root))
    if not code:
        return Practice(
            name="content-roots",
            adopted=False,
            evidence=(),
            why=(
                "no conventional code directory was found, so 'code' and 'not code' "
                "cannot be told apart and the stray-executable check does not apply"
            ),
        )
    evidence = content_roots(root)
    return Practice(
        name="content-roots",
        adopted=bool(evidence),
        evidence=evidence,
        why="there is somewhere for an executable to hide outside the code directory",
    )


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


@dataclass(frozen=True)
class Detection:
    root: str
    code_roots: tuple[str, ...]
    practices: tuple[Practice, ...] = field(default_factory=tuple)

    @property
    def adopted(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.practices if p.adopted)

    @property
    def not_adopted(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.practices if not p.adopted)

    def as_dict(self) -> dict[str, object]:
        return {
            "root": self.root,
            "code_roots": list(self.code_roots),
            "adopted": list(self.adopted),
            "not_adopted": list(self.not_adopted),
            "practices": [p.as_dict() for p in self.practices],
        }


DETECTORS = (
    _spec_driven,
    _debt_register,
    _agent_tooling,
    _verified_state,
    _declared_gates,
    _content_roots,
)


def detect(root: Path, *, only: Sequence[str] | None = None) -> Detection:
    """Every practice, with the evidence that decided it.

    Args:
        root: the repository to inspect.
        only: restrict to these practice names. An unknown name is ignored
            rather than raising: a caller asking about a practice this version
            does not know should get a shorter answer, not a crash.
    """
    practices = tuple(detector(root) for detector in DETECTORS)
    if only is not None:
        wanted = set(only)
        practices = tuple(p for p in practices if p.name in wanted)
    return Detection(
        root=str(root.resolve()),
        code_roots=code_roots(root),
        practices=practices,
    )
