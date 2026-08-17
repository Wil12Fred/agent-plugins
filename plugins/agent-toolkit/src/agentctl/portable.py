"""Which of this repository's code is not actually about this project.

A repository accumulates two kinds of code and stops being able to tell them
apart. Some of it encodes what the business does — endpoints, table names, the
rule about which venue can sell what. The rest is mechanism: a screenshot
driver, a parallelism calculator, a git hook enforcing a branch direction. The
second kind works anywhere and is invisible, because it sits in the same
directories as the first and nobody re-reads a helper that already works.

This finds it, so it can move to a shared repository and be reused instead of
rewritten in the next project.

## How it decides, and why the test is mechanical

**Count the lines that mention the project.** A file with none is portable by
construction — it cannot be about a business it never names. One with a handful
usually names a repository in a single sentence and is generic everywhere else.
One with many is domain code.

The test is deliberately not editorial. "Does this feel reusable?" is a question
a model answers confidently either way; "how many of these 121 lines mention the
company" has one answer, the same one every run, and a reader who disagrees can
open the file and count.

**The vocabulary is declared, never inferred.** It comes from `.agent-rules.toml`
— guessing which words are proper nouns from the code means asking the code
whether it is about itself, and it always says no.

## The failure this is really built around

Extraction is the easy half. **Doing it by copying instead of moving is the
half that goes wrong**, and it goes wrong silently: both copies work, so nothing
fails, and the divergence is only found when a fix made in one does not appear
in the other. It has happened three times in the repository this was written
for.

So when a target checkout is given, every candidate is checked against it, and
a file that already exists there is reported as `duplicate` — a defect, ranked
above every opportunity, because an opportunity costs nothing to defer and a
duplicate is already costing something.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

DECLARATION_FILES = (".agent-rules.toml", ".agentrules.toml")

CODE_SUFFIXES = frozenset(
    {
        ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs",
        ".rb", ".sh", ".bash", ".zsh", ".java", ".kt", ".lua", ".pl",
    }
)
"""Only executable code. Documentation is about the project by definition, and
including it turns every report into a list of README files."""

SKIP_DIRECTORIES = frozenset(
    {
        ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
        ".pytest_cache", ".ruff_cache", "dist", "build", "target", ".next",
    }
)

MIN_LINES = 15
"""Below this there is nothing to reuse.

A ten-line wrapper is cheaper to rewrite than to depend on, and listing it
crowds out the findings that are worth acting on.
"""

NEAR_MISS = 2
"""At most this many project-mentioning lines **in the file's substance**.

Not zero, because the commonest shape is a genuinely generic file whose header
comment names the repository it was written in. That is one edit, and refusing
to surface it would hide most of what is actually portable.

Import lines are counted separately and do not consume this budget — see
:func:`_classify`.
"""

_IMPORT = re.compile(
    r"^\s*(?:from\s+\S+\s+import\b|import\b|(?:const|let|var)\s.*\brequire\(|"
    r"import\s.*\bfrom\s|#include\b|use\s+\S+;|package\s+\S+)"
)
"""A line whose only job is naming where something comes from.

These are separated because they are a **different kind of work**. A file whose
only mentions are `from acme.core import ...` is portable after a rename — the
mechanism inside it knows nothing about the project. A file with a hardcoded
host in its logic is not, and no rename fixes it. Counting them together
produced "6 mentions" for a file that needed one mechanical edit, which reads
like a file nobody should touch.
"""


@dataclass(frozen=True)
class Vocabulary:
    """The words that mean "this project", and where they were declared.

    Attributes:
        terms: matched case-insensitively, as whole words where the term is
            alphanumeric. A term like `oper-` is a prefix and is matched as one.
        source: the file it came from, or None when nothing was declared —
            which is a refusal, not an empty vocabulary. Measuring against no
            terms would report the entire repository as portable, and be
            believed.
    """

    terms: tuple[str, ...] = ()
    source: str | None = None

    @property
    def declared(self) -> bool:
        return bool(self.terms)

    def pattern(self) -> re.Pattern[str] | None:
        if not self.terms:
            return None
        parts = []
        for term in self.terms:
            escaped = re.escape(term)
            # A trailing `-` or `_` marks a prefix (`oper-`, `sp_`): those are
            # ticket keys and must match `OPER-917`, so no closing boundary.
            parts.append(escaped if term[-1] in "-_" else rf"\b{escaped}\b")
        return re.compile("|".join(parts), re.IGNORECASE)


def load_vocabulary(root: Path) -> Vocabulary:
    """Read the project's own words from its rule declaration.

    Looks for a rule carrying a `terms` list — conventionally the `naming` rule
    that already says "nothing may name a company". That rule states the
    principle; `terms` is the part a tool can act on.
    """
    for candidate in DECLARATION_FILES:
        path = root / candidate
        if not path.is_file():
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            return Vocabulary(source=candidate)
        terms: list[str] = []
        for entry in data.get("rule", []):
            if not isinstance(entry, dict):
                continue
            for term in entry.get("terms", []) or []:
                if isinstance(term, str) and term.strip():
                    terms.append(term.strip())
        return Vocabulary(terms=tuple(dict.fromkeys(terms)), source=candidate)
    return Vocabulary()


@dataclass
class Candidate:
    """One file that could live in a shared repository.

    Attributes:
        path: repository-relative.
        lines: how big it is, so a reader can judge whether moving it is worth
            the dependency.
        mentions: lines naming the project **in its substance** — logic,
            prose, defaults. Zero means portable as it stands.
        imports: lines naming the project only to say where something comes
            from. Separate because they are a rename, not a rewrite.
        examples: up to three of those lines, because "one mention" is only
            actionable if you can see whether it is a header comment or a
            hardcoded host.
        duplicate_of: the same file already present in the target checkout.
            Set only when a target was given and the content matched.
        language_note: set when the file is portable but written in a language
            the target repository does not accept.
    """

    path: str
    lines: int
    mentions: int
    imports: int = 0
    examples: tuple[str, ...] = ()
    duplicate_of: str | None = None
    language_note: str | None = None

    @property
    def blocked(self) -> bool:
        """Whether something must happen before this can move."""
        return bool(self.mentions or self.language_note)

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "lines": self.lines,
            "mentions": self.mentions,
            "imports": self.imports,
            "examples": list(self.examples),
            "duplicate_of": self.duplicate_of,
            "language_note": self.language_note,
            "verdict": self.verdict,
        }

    @property
    def verdict(self) -> str:
        if self.duplicate_of:
            return "duplicate"
        if self.mentions == 0 and not self.language_note:
            return "portable"
        return "portable-after-edit"


@dataclass
class Survey:
    """What the sweep found.

    Attributes:
        candidates: worth moving, most portable first, duplicates before all.
        scanned: how many files were weighed. Reported because an empty result
            over an empty scan is not a clean repository, and the two are
            indistinguishable without it.
        vocabulary: what the verdicts were measured against.
        target: the shared repository checked for duplicates, if given.
    """

    candidates: list[Candidate] = field(default_factory=list)
    scanned: int = 0
    vocabulary: Vocabulary = field(default_factory=Vocabulary)
    target: str | None = None

    @property
    def duplicates(self) -> list[Candidate]:
        return [c for c in self.candidates if c.duplicate_of]

    def as_dict(self) -> dict[str, object]:
        return {
            "scanned": self.scanned,
            "measured_against": list(self.vocabulary.terms),
            "declared_in": self.vocabulary.source,
            "target": self.target,
            "candidates": [c.as_dict() for c in self.candidates],
            "duplicates": len(self.duplicates),
        }


def survey(
    root: Path,
    *,
    target: Path | None = None,
    vocabulary: Vocabulary | None = None,
    expect_language: str | None = None,
    exclude: Sequence[str] = (),
) -> Survey:
    """Find the code in `root` that is not about the project.

    Args:
        root: the repository to sweep.
        target: a checkout of the shared repository. When given, a candidate
            already present there is reported as a duplicate rather than as an
            opportunity.
        vocabulary: the project's own words. Read from `root` when omitted.
        expect_language: the language the target repository requires, e.g.
            `"english"`. A portable file in another language is reported as
            blocked on translation — it has to happen *before* the move, or the
            receiving repository is in violation the day it lands.
        exclude: glob patterns to skip, for directories already extracted.

    Raises:
        ValidationError: no vocabulary is declared. Measuring against no terms
            would report every file in the repository as portable, and that
            answer looks exactly like a real one.
    """
    from agentctl.errors import ValidationError

    words = vocabulary or load_vocabulary(root)
    if not words.declared:
        raise ValidationError(
            "no project vocabulary declared, so nothing can be judged portable",
            detail=(
                "add `terms = [...]` to a rule in .agent-rules.toml — the words that mean "
                "'this project'. Without them every file scores zero mentions and the whole "
                "repository would be reported as reusable"
            ),
        )
    pattern = words.pattern()
    assert pattern is not None  # guaranteed by `declared`

    found: list[Candidate] = []
    scanned = 0
    fingerprints = _fingerprints(target) if target else {}

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in CODE_SUFFIXES:
            continue
        relative = path.relative_to(root).as_posix()
        if SKIP_DIRECTORIES.intersection(Path(relative).parts):
            continue
        if any(fnmatch(relative, pattern_) for pattern_ in exclude):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines = text.splitlines()
        if len(lines) < MIN_LINES:
            continue
        scanned += 1

        substance, imports = _classify(lines, pattern)
        duplicate_of = fingerprints.get(_digest(text))
        # A duplicate is reported however coupled it is. It is a defect, not an
        # opportunity, and the threshold exists to rank opportunities: applying
        # it here hid `adf.py` — 407 lines already living in the shared repo —
        # because the copy that stayed behind had picked up project references
        # the extracted one had shed. The more diverged the copy, the more
        # important the finding, and the more certainly the filter would drop it.
        if len(substance) > NEAR_MISS and duplicate_of is None:
            continue

        candidate = Candidate(
            path=relative,
            lines=len(lines),
            mentions=len(substance),
            imports=len(imports),
            examples=tuple(h[:100] for h in substance[:3]),
            duplicate_of=duplicate_of,
            language_note=_language_note(text, path.suffix, expect_language),
        )
        found.append(candidate)

    found.sort(key=lambda c: (c.duplicate_of is None, c.mentions, -c.lines))
    return Survey(candidates=found, scanned=scanned, vocabulary=words, target=str(target) if target else None)


def _classify(
    lines: Sequence[str], pattern: re.Pattern[str]
) -> tuple[list[str], list[str]]:
    """Split the project-mentioning lines into substance and imports."""
    substance, imports = [], []
    for line in lines:
        if not pattern.search(line):
            continue
        (imports if _IMPORT.match(line) else substance).append(line.strip())
    return substance, imports


def _digest(text: str) -> str:
    """Content hash of everything except the import lines.

    Whitespace is normalised per line, because trailing-space and
    final-newline differences are not divergence and treating them as such
    would hide the duplicate this exists to catch.

    **Imports are excluded, and that is the load-bearing part.** A file is
    almost never copied unchanged: it is copied and its imports are rewritten
    to the new package namespace. An exact hash therefore reports the one shape
    that matters — a copy that has already been adapted — as two unrelated
    files. Measured on the repository this was written for: three modules
    (`adf`, `attachments`, `mentions`) were byte-identical apart from a single
    changed import line each, and an exact hash saw none of them.

    The cost is a wider net, and it is the right trade here: a false duplicate
    costs one glance at two files, a missed one costs a fix that silently
    reaches only half its users.
    """
    return hashlib.sha256(_executable_body(text).encode()).hexdigest()[:16]


_DOCSTRING = re.compile(r'("""|\'\'\')(?:.|\n)*?\1')
_LINE_COMMENT = re.compile(r"^\s*(?:#|//|--)")


def _executable_body(text: str) -> str:
    """The file with its prose removed: no docstrings, comments or imports.

    Prose is exactly what an extraction edits. Moving a module to a shared
    repository means taking the project's name out of its docstrings, so the
    copy that stayed behind and the copy that left are byte-different in the
    only part nobody executes. Measured: `adf.py` is 407 lines identical in
    every statement and differs in **two docstring lines**, and hashing the
    whole substance saw two unrelated files.

    Hashing what runs instead is both narrower and more honest — it answers
    "is this the same code", which is the question, rather than "is this the
    same file", which is not.
    """
    stripped = _DOCSTRING.sub("", text)
    return "\n".join(
        line.rstrip()
        for line in stripped.splitlines()
        if line.strip() and not _LINE_COMMENT.match(line) and not _IMPORT.match(line)
    ).strip()


def _fingerprints(target: Path) -> dict[str, str]:
    """Every code file in the shared repository, by content."""
    index: dict[str, str] = {}
    if not target.is_dir():
        return index
    for path in target.rglob("*"):
        if not path.is_file() or path.suffix not in CODE_SUFFIXES:
            continue
        if SKIP_DIRECTORIES.intersection(path.relative_to(target).parts):
            continue
        try:
            index.setdefault(_digest(path.read_text(encoding="utf-8")), path.relative_to(target).as_posix())
        except (OSError, UnicodeDecodeError):
            continue
    return index


def _language_note(text: str, suffix: str, expect: str | None) -> str | None:
    """Whether the file's prose is in the language the target requires."""
    if not expect:
        return None
    from agentctl import rules

    reads_as, _ = rules.language_of(rules.prose_of(text, suffix))
    if reads_as is None or reads_as == expect:
        return None
    return f"comments read as {reads_as}; the target repository requires {expect}"


def summarise(found: Iterable[Candidate]) -> list[str]:
    """Human lines, duplicates first because they are the only defect here."""
    lines = []
    for candidate in found:
        mark = {
            "duplicate": "DUPLICATE",
            "portable": "portable ",
            "portable-after-edit": "after-edit",
        }[candidate.verdict]
        detail = f"{candidate.lines} lines"
        if candidate.imports:
            detail += f", {candidate.imports} import line(s) to rename"
        lines.append(f"  [{mark}] {candidate.path}  ({detail})")
        if candidate.duplicate_of:
            lines.append(f"      already in the target as {candidate.duplicate_of}")
        for example in candidate.examples:
            lines.append(f"      names the project: {example}")
        if candidate.language_note:
            lines.append(f"      {candidate.language_note}")
    return lines
