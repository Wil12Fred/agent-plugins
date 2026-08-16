"""Detection: a practice is present when its evidence is, and never otherwise.

The failure this guards against is an auditor confidently reporting that a
microservice "does not follow spec-driven development" when it never claimed to.
Each test names the rule it enforces.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentctl import detect


def write(path: Path, content: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def practice(root: Path, name: str) -> detect.Practice:
    found = {p.name: p for p in detect.detect(root).practices}
    return found[name]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    write(tmp_path / "src" / "main.py", "print('hi')\n")
    return tmp_path


# --------------------------------------------------------------------------- #
# The rule the whole command exists for
# --------------------------------------------------------------------------- #


def test_a_plain_service_adopts_almost_nothing(repo: Path) -> None:
    """Rule: a repo that never took a practice on must not be reported as failing it.

    This is the case that matters. A microservice with source and a pipeline is
    the common shape, and every practice it never adopted has to come back
    `adopted=False` with no evidence — so the auditor skips those sections
    instead of generating findings nobody asked for.
    """
    write(repo / ".gitlab-ci.yml", "stages: [test]\n")

    result = detect.detect(repo)

    assert "declared-gates" in result.adopted
    assert "spec-driven" in result.not_adopted
    assert "debt-register" in result.not_adopted
    assert "verified-state" in result.not_adopted


def test_every_verdict_carries_the_path_that_produced_it(repo: Path) -> None:
    """Rule: a reader who disagrees opens the evidence instead of arguing."""
    write(repo / ".specify" / "memory" / "constitution.md", "# rules\n")

    found = practice(repo, "spec-driven")

    assert found.adopted
    assert found.evidence, "an adopted practice with no evidence is a guess"
    assert all((repo / item).exists() for item in found.evidence)


def test_a_practice_that_is_absent_carries_no_evidence(repo: Path) -> None:
    """The other half: evidence and adoption cannot disagree."""
    found = practice(repo, "spec-driven")
    assert not found.adopted
    assert found.evidence == ()


# --------------------------------------------------------------------------- #
# spec-driven: the one with a weak signal
# --------------------------------------------------------------------------- #


def test_a_specs_directory_alone_is_not_spec_driven_development(repo: Path) -> None:
    """Rule: plenty of projects have `specs/` holding OpenAPI files.

    Counting the directory name alone is how the auditor would start lecturing a
    service about a practice it never adopted — the exact noise this command
    exists to prevent.
    """
    write(repo / "specs" / "openapi.yaml", "openapi: 3.0.0\n")

    assert not practice(repo, "spec-driven").adopted


def test_a_specs_directory_with_a_steering_document_is(repo: Path) -> None:
    """Rule: the directory plus something that steers it is the practice."""
    write(repo / "specs" / "TICKET-1" / "README.md", "# spec\n")
    write(repo / "AGENTS.md", "# how to work here\n")

    assert practice(repo, "spec-driven").adopted


@pytest.mark.parametrize("marker", [".specify", "openspec", ".kiro"])
def test_a_tool_directory_is_enough_on_its_own(repo: Path, marker: str) -> None:
    """Rule: these exist only because somebody adopted the practice."""
    write(repo / marker / "config.yaml", "x: 1\n")
    assert practice(repo, "spec-driven").adopted


# --------------------------------------------------------------------------- #
# Code roots — the answer everything else depends on
# --------------------------------------------------------------------------- #


def test_the_code_root_is_detected_not_assumed(tmp_path: Path) -> None:
    """Rule: `packages/` and `src/` are both ordinary; guessing makes the rest wrong."""
    (tmp_path / "packages").mkdir()
    assert detect.code_roots(tmp_path) == ("packages",)


def test_no_conventional_code_directory_answers_empty(tmp_path: Path) -> None:
    """Rule: 'unknown' must not collapse into 'the root is the code root'.

    A documentation repository has no `src/`. Reading that as "the whole repo is
    code" turns every markdown file into a stray and the report into noise.
    """
    write(tmp_path / "notes.md", "# notes\n")
    assert detect.code_roots(tmp_path) == ()


def test_content_roots_do_not_apply_without_a_code_root(tmp_path: Path) -> None:
    """Rule: without knowing where code is, 'not code' is the whole repository."""
    write(tmp_path / "docs" / "a.md", "# a\n")

    found = practice(tmp_path, "content-roots")
    assert not found.adopted
    assert "cannot be told apart" in found.why


def test_a_code_directory_is_never_reported_as_a_content_root(repo: Path) -> None:
    """`packages/` is where code belongs; it cannot also be where code hides."""
    (repo / "packages").mkdir()
    write(repo / "docs" / "a.md", "# a\n")

    found = practice(repo, "content-roots")
    assert "docs" in found.evidence
    assert "packages" not in found.evidence


# --------------------------------------------------------------------------- #
# The content-scanning detectors
# --------------------------------------------------------------------------- #


def test_verified_state_is_found_by_reading_the_file_not_its_name(repo: Path) -> None:
    """Rule: the block is the evidence. A file called `verified-state.md` is not."""
    write(repo / "docs" / "queue.md", "# queue\n\n## Verified state\n\n```yaml\nx: 1\n```\n")
    write(repo / "docs" / "verified-state-notes.md", "# just a name\n")

    found = practice(repo, "verified-state")
    assert found.adopted
    assert found.evidence == ("docs/queue.md",)


def test_a_debt_baseline_is_found_by_any_of_its_usual_extensions(repo: Path) -> None:
    write(repo / "docs" / "debt-baseline.tsv", "signal\tcount\n")
    assert practice(repo, "debt-register").adopted


def test_vendored_directories_are_not_evidence(repo: Path) -> None:
    """Rule: somebody else's `node_modules` says nothing about what you adopted."""
    write(repo / "node_modules" / "pkg" / "debt-baseline.json", "{}")

    assert not practice(repo, "debt-register").adopted


# --------------------------------------------------------------------------- #
# Robustness — this runs against repositories it has never seen
# --------------------------------------------------------------------------- #


def test_an_unknown_practice_name_shortens_the_answer_rather_than_raising(repo: Path) -> None:
    """Rule: a caller asking about a practice this version lacks gets less, not a crash."""
    result = detect.detect(repo, only=["spec-driven", "invented-yesterday"])

    assert [p.name for p in result.practices] == ["spec-driven"]


def test_an_undecodable_file_does_not_take_the_scan_down(repo: Path) -> None:
    """Rule: a binary in `docs/` is normal and must not raise."""
    path = repo / "docs" / "diagram.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")

    detect.detect(repo)  # must not raise


def test_an_empty_repository_answers_rather_than_failing(tmp_path: Path) -> None:
    result = detect.detect(tmp_path)
    assert result.adopted == ()
    assert len(result.practices) == len(detect.DETECTORS)


# --------------------------------------------------------------------------- #
# The root-as-code-root layout — Express, Rails, Go
# --------------------------------------------------------------------------- #


def test_a_root_build_manifest_makes_the_root_the_code_root(tmp_path: Path) -> None:
    """Rule: `controllers/` and `services/` at the root is a code layout, not a mess.

    Measured against a real Express service with no `src/` anywhere. Answering
    "none found" there was honest but useless: it left the stray check unable to
    run on one of the commonest shapes there is.
    """
    write(tmp_path / "package.json", '{"name": "svc"}')
    (tmp_path / "controllers").mkdir()

    assert detect.code_roots(tmp_path) == (".",)


def test_a_conventional_directory_beats_a_root_manifest(tmp_path: Path) -> None:
    """Rule: a project with both keeps its code in the directory, not at the root."""
    write(tmp_path / "pyproject.toml", "[project]\nname='x'\n")
    (tmp_path / "src").mkdir()

    assert detect.code_roots(tmp_path) == ("src",)


def test_a_documentation_repository_is_still_unknown(tmp_path: Path) -> None:
    """The control. No manifest and no code directory must stay 'unknown'.

    Without it the manifest rule could creep into 'the root is always code',
    which turns a notes repository into hundreds of false strays.
    """
    write(tmp_path / "notes.md", "# notes\n")
    write(tmp_path / "docs" / "a.md", "# a\n")

    assert detect.code_roots(tmp_path) == ()


def test_content_roots_exclude_a_directory_that_is_the_code_root(tmp_path: Path) -> None:
    write(tmp_path / "package.json", "{}")
    write(tmp_path / "docs" / "a.md", "# a\n")

    assert detect.content_roots(tmp_path) == ("docs",)
