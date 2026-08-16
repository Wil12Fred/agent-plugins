"""Strays: executables outside the code directory, and which are a decision.

Each test names the rule it enforces.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentctl import strays


def write(path: Path, content: str = "#!/usr/bin/env python\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    write(tmp_path / "src" / "main.py")
    return tmp_path


def paths(found: list[strays.Stray]) -> set[str]:
    return {s.path for s in found}


# --------------------------------------------------------------------------- #
# What counts
# --------------------------------------------------------------------------- #


def test_a_script_in_a_docs_folder_is_a_stray(repo: Path) -> None:
    write(repo / "docs" / "TICKET-1" / "monitor.py")

    assert paths(strays.find(repo)) == {"docs/TICKET-1/monitor.py"}


def test_code_inside_the_code_directory_is_not(repo: Path) -> None:
    """The control. Without it, 'everything is a stray' would pass the test above."""
    write(repo / "src" / "helper.py")

    assert paths(strays.find(repo)) == set()


def test_shell_scripts_count_too(repo: Path) -> None:
    """Rule: reading only Python is how a dead shell script survived for months.

    Its `.env` path pointed at a machine the author no longer owned. Nothing had
    failed, because nothing had looked.
    """
    write(repo / "docs" / "fetch-logs.sh", "#!/usr/bin/env bash\n")

    assert paths(strays.find(repo)) == {"docs/fetch-logs.sh"}


@pytest.mark.parametrize("name", ["a.mjs", "b.js", "c.rb", "d.bash"])
def test_the_other_extensions_count(repo: Path, name: str) -> None:
    write(repo / "docs" / name)
    assert paths(strays.find(repo)) == {f"docs/{name}"}


def test_prose_and_data_are_not_executables(repo: Path) -> None:
    write(repo / "docs" / "notes.md", "# notes\n")
    write(repo / "docs" / "data.csv", "a,b\n")

    assert strays.find(repo) == []


def test_vendored_and_cache_directories_are_skipped(repo: Path) -> None:
    """Rule: a checked-in dependency tree is not your debt, and it is enormous."""
    write(repo / "docs" / "node_modules" / "pkg" / "index.js")
    write(repo / "docs" / "__pycache__" / "x.py")

    assert strays.find(repo) == []


# --------------------------------------------------------------------------- #
# Declared versus undeclared — the distinction that makes it a decision
# --------------------------------------------------------------------------- #


def test_an_undeclared_script_is_reported(repo: Path) -> None:
    write(repo / "docs" / "one.py")

    assert len(strays.undeclared(strays.find(repo))) == 1


def test_a_declared_script_carries_its_reason_and_stops_being_open(repo: Path) -> None:
    """Rule: an undeclared script and a declared one look identical on disk.

    Only one of them is a decision, and the reason is what makes it one.
    """
    write(repo / "docs" / "one.py")
    write(
        repo / ".agentctl-allow",
        "# one-off forensics, bound to a single incident\n"
        "docs/one.py  # pinned to the 2026-05-22 outage window\n",
    )

    found = strays.find(repo)
    assert len(found) == 1
    assert found[0].declared
    assert found[0].reason == "pinned to the 2026-05-22 outage window"
    assert strays.undeclared(found) == []


def test_a_csv_manifest_declares_only_its_kept_rows(repo: Path) -> None:
    """Rule: `migrated` means the capability moved; only `kept` is an exception."""
    write(repo / "docs" / "kept.py")
    write(repo / "docs" / "moved.py")
    write(
        repo / "docs" / "refactor" / "migration-manifest.csv",
        "source_path,kind,destination,status,note\n"
        "docs/kept.py,python,-,kept,bound to one incident\n"
        "docs/moved.py,python,pkg,migrated,became a command\n",
    )

    open_items = {s.path for s in strays.undeclared(strays.find(repo))}
    assert "docs/moved.py" in open_items
    assert "docs/kept.py" not in open_items


def test_a_comment_line_in_the_allow_file_declares_nothing(repo: Path) -> None:
    write(repo / "docs" / "one.py")
    write(repo / ".agentctl-allow", "# docs/one.py\n")

    assert len(strays.undeclared(strays.find(repo))) == 1


# --------------------------------------------------------------------------- #
# The refusal that keeps the report honest
# --------------------------------------------------------------------------- #


def test_without_a_known_code_root_nothing_is_reported(tmp_path: Path) -> None:
    """Rule: 'could not measure' must not render as a list of every file.

    A documentation repository has no `src/`. Reading that as "the whole repo is
    outside the code" would report hundreds of strays and mean nothing — and a
    reader would take the length of the list as severity.
    """
    write(tmp_path / "docs" / "one.py")

    assert strays.find(tmp_path) == []


def test_line_counts_are_reported_so_severity_can_be_judged(repo: Path) -> None:
    write(repo / "docs" / "big.py", "\n".join(f"line {i}" for i in range(40)) + "\n")

    (stray,) = strays.find(repo)
    assert stray.lines == 40


# --------------------------------------------------------------------------- #
# The root-as-code-root layout
# --------------------------------------------------------------------------- #


def test_source_directories_are_not_swept_when_the_root_is_the_code_root(tmp_path: Path) -> None:
    """Rule: search the content roots, not "everything that is not code".

    In an Express layout the code root is the repository root, so "everything
    else" is `controllers/`, `migrations/`, `config/` — the entire service
    reported as strays. Searching the *content* roots is what keeps the answer
    meaningful in both layouts.
    """
    write(tmp_path / "package.json", "{}")
    write(tmp_path / "controllers" / "lesson.js", "module.exports = {}\n")
    write(tmp_path / "migrations" / "001.js", "// up\n")
    write(tmp_path / "docs" / "probe.py")

    assert paths(strays.find(tmp_path)) == {"docs/probe.py"}


def test_a_repository_with_nowhere_to_hide_reports_nothing(tmp_path: Path) -> None:
    """Rule: no content root means no place for a script to hide — measured, empty."""
    write(tmp_path / "package.json", "{}")
    write(tmp_path / "controllers" / "lesson.js", "module.exports = {}\n")

    assert strays.find(tmp_path) == []
