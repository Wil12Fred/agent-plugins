"""The rules a repository declares for itself, in a form an auditor can check.

`detect` answers *what practices this repository has adopted* — spec folders, a
debt baseline, agent tooling. This module answers a different question: **what
has this repository decided, that an auditor should hold it to?**

The two are not the same, and conflating them is why an auditor drifts into
generic advice. "Every identifier and comment is English, except text quoted
into JIRA or Slack" is not a practice you can detect from a directory listing.
It is a decision somebody made, written in prose, and until it is declared it is
invisible to any tool.

## Where a rule comes from

Two sources, and both matter:

**`.agent-rules.toml`** — declared, structured, and therefore *checkable*. A
rule here has a kind the tool understands, a scope, and its exceptions. This is
the only kind an auditor can measure rather than read.

**The prose the repository already keeps** — `AGENTS.md`, `CLAUDE.md`, a
constitution, `CONTRIBUTING.md`. These carry the reasoning and the rules nobody
has made machine-readable yet. They are returned as *sources to read*, never as
rules pretended to be structured: an auditor that invents a rule out of prose is
the failure this repository has already recorded once.

## Why declaring beats inferring

A rule inferred from the code is a description of the code, so the code always
passes. The declaration has to come from outside the thing being judged, or the
audit is a mirror.

And a rule nobody declared is not a finding. An auditor reporting that a
repository fails a standard it never adopted is noise, and noise is how an
auditor teaches people to ignore it — including on the day it is right.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from agentctl.errors import ValidationError

DECLARATION_FILES = (".agent-rules.toml", ".agentrules.toml")
"""Where a declaration may live. The first that exists wins; both are searched
because neither name is a standard and guessing wrong is silent."""

PROSE_SOURCES = (
    "AGENTS.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    ".specify/memory/constitution.md",
    ".github/CONTRIBUTING.md",
    "docs/CONTRIBUTING.md",
)
"""Documents that carry rules in prose. Returned to be *read*, not parsed."""

KINDS = ("language", "naming", "layout", "process", "custom")
"""What a declared rule can be about.

`language` is the only one this module measures today, because it is the only
one where a cheap check is also an honest one. The rest are carried through to
the auditor as text with their scope attached — which is still worth far more
than nothing, because the auditor then knows the rule exists and where it
applies.
"""


@dataclass(frozen=True)
class Rule:
    """One rule the repository declared about itself.

    Attributes:
        name: stable identifier, cited in a report.
        kind: one of :data:`KINDS`.
        rule: the rule itself, in the repository's own words.
        applies_to: glob patterns the rule governs. Empty means the whole
            repository — which is a decision, so it is spelled out rather than
            inferred from an absent key.
        exempt: glob patterns explicitly outside it. **An exception is part of
            the rule**, not a footnote: "everything is English" and "everything
            is English except the text quoted into a ticket" are different
            rules, and an auditor holding a repo to the first one is wrong.
        why: the reasoning, so a report can explain a finding rather than
            assert it.
        expect: kind-specific expectation, e.g. `"english"` for `language`.
    """

    name: str
    kind: str
    rule: str
    applies_to: tuple[str, ...] = ()
    exempt: tuple[str, ...] = ()
    why: str = ""
    expect: str = ""

    @property
    def checkable(self) -> bool:
        """Whether this module can measure the rule, rather than only report it.

        Said out loud in the payload because the difference is the whole point:
        an auditor must not present "I read this rule" as "I verified this
        rule". The second is evidence; the first is a citation.
        """
        return self.kind == "language" and self.expect in _LANGUAGES

    def governs(self, relative_path: str) -> bool:
        """Whether the rule applies to this path.

        Exemptions win over inclusions. A path matched by both is exempt —
        because an exception is written to carve something *out*, and resolving
        the overlap the other way makes every exception silently inert.
        """
        if any(fnmatch(relative_path, pattern) for pattern in self.exempt):
            return False
        if not self.applies_to:
            return True
        return any(fnmatch(relative_path, pattern) for pattern in self.applies_to)

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "rule": self.rule,
            "applies_to": list(self.applies_to),
            "exempt": list(self.exempt),
            "why": self.why,
            "expect": self.expect,
            "checkable": self.checkable,
        }


@dataclass(frozen=True)
class Declaration:
    """Everything a repository has said about how it wants to be judged.

    Attributes:
        rules: the structured, declared rules.
        prose_sources: rule documents that exist but are not machine-readable.
            An auditor reads these; it must not pretend they are rules it
            measured.
        declaration_file: which file the rules came from, or None when the
            repository has declared nothing. None is meaningful: it means "this
            repository has not been given rules", not "this repository has no
            rules".
    """

    rules: tuple[Rule, ...] = ()
    prose_sources: tuple[str, ...] = ()
    declaration_file: str | None = None

    @property
    def checkable(self) -> tuple[Rule, ...]:
        return tuple(rule for rule in self.rules if rule.checkable)

    def as_dict(self) -> dict[str, object]:
        return {
            "declaration_file": self.declaration_file,
            "rules": [rule.as_dict() for rule in self.rules],
            "prose_sources": list(self.prose_sources),
            "checkable": [rule.name for rule in self.checkable],
        }


def load(root: Path) -> Declaration:
    """Read the repository's declared rules.

    An absent declaration is not an error — most repositories have none, and
    saying so plainly is more useful than inventing rules to fill the gap.

    Raises:
        ValidationError: the declaration exists but does not parse, or names a
            kind this tool does not understand. A malformed declaration must
            fail loudly: silently returning "no rules" would report a repository
            with a typo in its config as one that never had standards.
    """
    prose = tuple(name for name in PROSE_SOURCES if (root / name).is_file())

    for candidate in DECLARATION_FILES:
        path = root / candidate
        if path.is_file():
            return Declaration(
                rules=_parse(path),
                prose_sources=prose,
                declaration_file=candidate,
            )
    return Declaration(prose_sources=prose)


def _parse(path: Path) -> tuple[Rule, ...]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ValidationError(
            f"{path.name} does not parse as TOML",
            detail=str(exc),
        ) from exc

    entries = data.get("rule")
    if entries is None:
        return ()
    if not isinstance(entries, list):
        raise ValidationError(
            f"{path.name}: `rule` must be a list of tables",
            detail="write each rule as [[rule]], not [rule]",
        )

    rules = []
    for index, entry in enumerate(entries, start=1):
        rules.append(_rule_from(entry, path.name, index))
    return tuple(rules)


def _rule_from(entry: object, filename: str, index: int) -> Rule:
    if not isinstance(entry, dict):
        raise ValidationError(f"{filename}: rule {index} is not a table")

    name = str(entry.get("name") or f"rule-{index}")
    kind = str(entry.get("kind") or "custom")
    if kind not in KINDS:
        raise ValidationError(
            f"{filename}: rule {name!r} has unknown kind {kind!r}",
            detail=f"one of: {', '.join(KINDS)}",
        )
    text = str(entry.get("rule") or "").strip()
    if not text:
        raise ValidationError(
            f"{filename}: rule {name!r} has no `rule` text",
            detail="an auditor quotes this back; a rule with no words cannot be reported",
        )
    return Rule(
        name=name,
        kind=kind,
        rule=text,
        applies_to=_globs(entry.get("applies_to")),
        exempt=_globs(entry.get("exempt")),
        why=str(entry.get("why") or "").strip(),
        expect=str(entry.get("expect") or "").strip().lower(),
    )


def _globs(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    raise ValidationError(
        "glob lists must be a string or a list of strings",
        detail=f"got {type(value).__name__}",
    )


# --------------------------------------------------------------------------- #
# Checking a `language` rule
# --------------------------------------------------------------------------- #
#
# The measurement is a word-frequency ratio, which is a heuristic and is
# described as one everywhere it surfaces. It is here rather than left to a
# model because a heuristic that always answers the same way is auditable, and a
# model asked "is this English?" is not.

_LANGUAGES: dict[str, tuple[frozenset[str], frozenset[str]]] = {}


def _register(name: str, markers: Iterable[str], against: Iterable[str]) -> None:
    _LANGUAGES[name] = (frozenset(markers), frozenset(against))


_register(
    "english",
    markers=(
        " the ", " and ", " with ", " this ", " that ", " from ", " when ",
        " which ", " for ", " not ", " is ", " are ", " because ", " into ",
    ),
    against=(
        " que ", " los ", " las ", " del ", " para ", " con ", " una ", " por ",
        " como ", " pero ", " este ", " esta ", " cuando ", " porque ",
    ),
)
_register(
    "spanish",
    markers=_LANGUAGES["english"][1],
    against=_LANGUAGES["english"][0],
)

MIN_WORDS = 40
"""Below this a ratio is noise, so the file is reported as *not measured*.

A three-word comment has no measurable language. Counting it either way is how
a checker produces confident findings about nothing.
"""

_COMMENT = re.compile(
    r"""(?:^|\s)(?:\#|//|--)\s?(?P<line>[^\n]*)     # line comments
        |/\*(?P<block>.*?)\*/                        # C-style blocks
        |"{3}(?P<pydoc>.*?)"{3}                      # Python docstrings
    """,
    re.VERBOSE | re.DOTALL,
)


@dataclass(frozen=True)
class Violation:
    """One file that fails a checkable rule."""

    path: str
    rule: str
    detail: str
    confidence: str = "heuristic"

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "rule": self.rule,
            "detail": self.detail,
            "confidence": self.confidence,
        }


@dataclass
class CheckResult:
    """What checking the declared rules found.

    Attributes:
        violations: files that fail a rule.
        checked: how many files each rule actually measured. Reported because
            "no violations" over zero files is not a pass, and the two are
            indistinguishable without it — the single most common way a gate
            reports success while measuring nothing.
        unmeasurable: rules that were declared but cannot be checked by this
            tool, so an auditor reads them instead of trusting a silent pass.
    """

    violations: list[Violation] = field(default_factory=list)
    checked: dict[str, int] = field(default_factory=dict)
    unmeasurable: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "violations": [v.as_dict() for v in self.violations],
            "checked": dict(self.checked),
            "unmeasurable": list(self.unmeasurable),
            "ok": not self.violations,
        }


def prose_of(text: str, suffix: str) -> str:
    """The human-language part of a source file: comments and docstrings.

    Identifiers are excluded deliberately. `getUserById` is English and
    `obtenerUsuario` is not, but a ratio over identifiers is dominated by
    keywords and library names in whatever language the ecosystem uses, and it
    reports a perfectly Spanish file as English. Comments are where a language
    rule is actually broken and where the measurement is honest.

    For a markdown or text file the whole document is prose.
    """
    if suffix in {".md", ".markdown", ".txt", ".rst"}:
        return text
    parts = []
    for match in _COMMENT.finditer(text):
        parts.append(match.group("line") or match.group("block") or match.group("pydoc") or "")
    return "\n".join(parts)


def language_of(prose: str) -> tuple[str | None, int]:
    """Which registered language this reads as, and how many words were weighed.

    Returns `(None, count)` when there is too little text to judge — which is a
    third answer, distinct from both "English" and "not English". Collapsing it
    into either is how a checker invents findings out of short files.
    """
    words = prose.split()
    if len(words) < MIN_WORDS:
        return None, len(words)
    haystack = f" {' '.join(words).lower()} "
    scores = {
        name: sum(haystack.count(marker) for marker in markers)
        for name, (markers, _) in _LANGUAGES.items()
    }
    best = max(scores, key=lambda name: scores[name])
    if scores[best] == 0:
        return None, len(words)
    return best, len(words)


def check(
    root: Path,
    declaration: Declaration,
    *,
    files: Sequence[Path] | None = None,
) -> CheckResult:
    """Measure every checkable rule; name every rule that could not be measured.

    Args:
        root: the repository.
        declaration: what it declared about itself.
        files: which files to weigh. Defaults to the tracked, text-like files.

    A rule this tool cannot measure is listed in `unmeasurable` rather than
    passing quietly. A silent pass and a real pass look identical in a report,
    and only one of them is true.
    """
    result = CheckResult()
    candidates = list(files) if files is not None else _text_files(root)

    for rule in declaration.rules:
        if not rule.checkable:
            result.unmeasurable.append(rule.name)
            continue
        measured = 0
        for path in candidates:
            relative = path.relative_to(root).as_posix()
            if not rule.governs(relative):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            reads_as, words = language_of(prose_of(text, path.suffix))
            if reads_as is None:
                continue
            measured += 1
            if reads_as != rule.expect:
                result.violations.append(
                    Violation(
                        path=relative,
                        rule=rule.name,
                        detail=(
                            f"reads as {reads_as}, the rule expects {rule.expect} "
                            f"({words} words weighed)"
                        ),
                    )
                )
        result.checked[rule.name] = measured

    return result


_SKIP_DIRECTORIES = frozenset(
    {
        ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
        ".pytest_cache", ".ruff_cache", "dist", "build", ".next", "target",
    }
)
_TEXT_SUFFIXES = frozenset(
    {
        ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
        ".rb", ".php", ".sh", ".bash", ".zsh", ".sql", ".md", ".markdown",
        ".txt", ".rst", ".yml", ".yaml", ".toml",
    }
)


def _text_files(root: Path, *, limit: int = 20_000) -> list[Path]:
    """Text-like files under `root`, skipping vendored and generated trees.

    The limit is a guard, not a policy: a walk that wanders into a
    `node_modules` nobody excluded should stop rather than hang. When it trips,
    the caller is told how many files were weighed, so a truncated run cannot be
    read as a complete one.
    """
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if len(found) >= limit:
            break
        if not path.is_file() or path.suffix not in _TEXT_SUFFIXES:
            continue
        if _SKIP_DIRECTORIES.intersection(path.relative_to(root).parts):
            continue
        found.append(path)
    return found
