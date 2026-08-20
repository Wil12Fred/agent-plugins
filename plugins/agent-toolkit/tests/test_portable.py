"""Finding code that is not about the project, and the ways that goes wrong.

The sweep has two jobs and they pull against each other. It must surface enough
to be worth running, and it must not surface so much that the one *defect* in
the list — a file already copied to the shared repository — is lost among
opportunities. Most of these tests are about that ordering.

Every duplicate-detection test here is a real shape from the repository this was
written for. None of them is hypothetical, and each defeated an earlier, simpler
version of the check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentctl import portable
from agentctl.errors import ValidationError

RULES = """
[[rule]]
name = "no-project-names"
kind = "naming"
rule = "Nothing shared may name the project."
terms = ["acme", "widgetco", "tkt-"]
"""

GENERIC = '''\
"""A helper that knows nothing about any business.

It counts things and returns the count. Fifteen lines of it, because anything
shorter is cheaper to rewrite than to depend on.
"""


def total(rows):
    result = 0
    for row in rows:
        result += row
    return result


def average(rows):
    return total(rows) / len(rows) if rows else 0
'''

COUPLED = '''\
"""Reads the acme establishment table."""

ACME_HOST = "api.acme.example"
ACME_DB = "acmesys"


def fetch(establishment_id):
    """Look up an acme venue by id, for ticket TKT-42."""
    return f"{ACME_HOST}/acme/{establishment_id}"


def audit(rows):
    return [r for r in rows if r["acme_id"]]


def report(rows):
    return len(audit(rows))
'''


def project(tmp_path: Path, **files: str) -> Path:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / ".agent-rules.toml").write_text(RULES, encoding="utf-8")
    for name, body in files.items():
        target = root / name.replace("__", "/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return root


# --- the vocabulary has to be declared --------------------------------------


def test_with_no_declared_vocabulary_it_refuses_instead_of_passing_everything(
    tmp_path: Path,
) -> None:
    """Rule: an undeclared vocabulary is a refusal, never an empty one.

    With no terms, every file scores zero mentions and the entire repository is
    reported as reusable. That answer looks exactly like a real one, which makes
    it the worst available failure — a confident, complete, wrong list.
    """
    root = tmp_path / "bare"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text(GENERIC, encoding="utf-8")

    with pytest.raises(ValidationError, match="no project vocabulary"):
        portable.survey(root)


def test_the_vocabulary_comes_from_the_declaration(tmp_path: Path) -> None:
    root = project(tmp_path)

    vocabulary = portable.load_vocabulary(root)

    assert vocabulary.declared
    assert "acme" in vocabulary.terms
    assert vocabulary.source == ".agent-rules.toml"


# --- what counts as portable ------------------------------------------------


def test_a_file_that_never_names_the_project_is_portable(tmp_path: Path) -> None:
    root = project(tmp_path, src__helper=GENERIC.replace("helper", "helper"))
    (root / "src" / "helper.py").write_text(GENERIC, encoding="utf-8")

    result = portable.survey(root)

    assert [c.path for c in result.candidates] == ["src/helper.py"]
    assert result.candidates[0].verdict == "portable"


def test_a_file_full_of_the_project_is_not_reported(tmp_path: Path) -> None:
    """The control. Without it, a sweep that returns everything passes the test
    above and is useless."""
    root = project(tmp_path)
    (root / "src" / "domain.py").write_text(COUPLED, encoding="utf-8")

    result = portable.survey(root)

    assert result.candidates == []
    assert result.scanned == 1, "it must have actually weighed the file"


def test_an_import_of_the_project_namespace_is_a_rename_not_a_rewrite(
    tmp_path: Path,
) -> None:
    """Rule: import lines are counted apart from the file's substance.

    A file whose only mentions are `from acme.core import ...` is portable after
    a rename — the mechanism inside knows nothing about the project. Counting
    those together with a hardcoded host produced "6 mentions" for a file
    needing one mechanical edit, which reads like a file nobody should touch.
    """
    root = project(tmp_path)
    (root / "src" / "mech.py").write_text(
        "from acme.core import thing\nimport acme.util\n\n" + GENERIC, encoding="utf-8"
    )

    result = portable.survey(root)

    candidate = result.candidates[0]
    assert candidate.mentions == 0, "an import is not substance"
    assert candidate.imports == 2
    assert candidate.verdict == "portable"


def test_something_too_small_to_reuse_is_skipped(tmp_path: Path) -> None:
    root = project(tmp_path)
    (root / "src" / "tiny.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    result = portable.survey(root)

    assert result.candidates == []


# --- duplicates: the defect this is really for ------------------------------
#
# Each of these defeated an earlier version of the check, and each is a real
# shape. Extraction by copying is silent — both copies work, so nothing fails,
# and the divergence surfaces when a fix in one does not reach the other.


def test_a_file_already_in_the_target_is_a_duplicate(tmp_path: Path) -> None:
    root = project(tmp_path)
    (root / "src" / "helper.py").write_text(GENERIC, encoding="utf-8")
    shared = tmp_path / "shared"
    (shared / "lib").mkdir(parents=True)
    (shared / "lib" / "helper.py").write_text(GENERIC, encoding="utf-8")

    result = portable.survey(root, target=shared)

    assert result.candidates[0].verdict == "duplicate"
    assert result.candidates[0].duplicate_of == "lib/helper.py"


def test_a_copy_whose_imports_were_rewritten_is_still_a_duplicate(tmp_path: Path) -> None:
    """Rule: a copy is almost never byte-identical — its imports were changed.

    Moving a module to a shared repository means repointing it at the new
    package namespace, so an exact hash reports the one shape that matters as
    two unrelated files. Three real modules were byte-identical apart from a
    single import line each, and an exact hash saw none of them.
    """
    root = project(tmp_path)
    (root / "src" / "helper.py").write_text("from acme.core import x\n" + GENERIC, encoding="utf-8")
    shared = tmp_path / "shared"
    (shared / "lib").mkdir(parents=True)
    (shared / "lib" / "helper.py").write_text(
        "from shared.core import x\n" + GENERIC, encoding="utf-8"
    )

    result = portable.survey(root, target=shared)

    assert result.candidates[0].duplicate_of == "lib/helper.py"


def test_a_copy_whose_docstrings_were_edited_is_still_a_duplicate(tmp_path: Path) -> None:
    """Rule: hash what runs, not what reads.

    Extraction edits *prose* — taking the project's name out of the docstrings
    is the whole point of it. A 407-line module was identical in every statement
    and differed in two docstring lines, and hashing the substance saw two
    unrelated files.
    """
    root = project(tmp_path)
    (root / "src" / "helper.py").write_text(
        GENERIC.replace("any business", "the acme business"), encoding="utf-8"
    )
    shared = tmp_path / "shared"
    (shared / "lib").mkdir(parents=True)
    (shared / "lib" / "helper.py").write_text(GENERIC, encoding="utf-8")

    result = portable.survey(root, target=shared)

    assert result.candidates[0].duplicate_of == "lib/helper.py"


def test_a_duplicate_is_reported_however_coupled_it_has_become(tmp_path: Path) -> None:
    """Rule: the portability threshold ranks opportunities; a duplicate is a
    defect and must bypass it.

    Applying the threshold hid a 407-line module already living in the shared
    repository, because the copy that stayed behind had picked up project
    references the extracted one had shed. **The more diverged the copy, the
    more important the finding — and the more certainly the filter drops it.**
    """
    root = project(tmp_path)
    drifted = COUPLED + "\n\n" + GENERIC
    (root / "src" / "drifted.py").write_text(drifted, encoding="utf-8")
    shared = tmp_path / "shared"
    (shared / "lib").mkdir(parents=True)
    (shared / "lib" / "drifted.py").write_text(drifted, encoding="utf-8")

    result = portable.survey(root, target=shared)

    assert [c.verdict for c in result.candidates] == ["duplicate"]


def test_two_different_files_are_not_called_duplicates(tmp_path: Path) -> None:
    """The control for every duplicate test above. A matcher loose enough to
    catch an edited copy must still distinguish unrelated code."""
    root = project(tmp_path)
    (root / "src" / "helper.py").write_text(GENERIC, encoding="utf-8")
    shared = tmp_path / "shared"
    (shared / "lib").mkdir(parents=True)
    (shared / "lib" / "other.py").write_text(COUPLED, encoding="utf-8")

    result = portable.survey(root, target=shared)

    assert result.candidates[0].duplicate_of is None


def test_duplicates_are_listed_before_opportunities(tmp_path: Path) -> None:
    root = project(tmp_path)
    (root / "src" / "fresh.py").write_text(GENERIC.replace("counts", "tallies"), encoding="utf-8")
    (root / "src" / "copied.py").write_text(GENERIC, encoding="utf-8")
    shared = tmp_path / "shared"
    (shared / "lib").mkdir(parents=True)
    (shared / "lib" / "copied.py").write_text(GENERIC, encoding="utf-8")

    result = portable.survey(root, target=shared)

    assert result.candidates[0].path == "src/copied.py"


# --- language, and the count that stops a blank looking like a pass ---------


def test_a_portable_file_in_the_wrong_language_is_blocked_on_translation(
    tmp_path: Path,
) -> None:
    """Rule: translation happens before the move, not after.

    A shared repository that requires English is in violation the day a
    Spanish-commented file lands in it, and the move is the moment nobody is
    looking.
    """
    root = project(tmp_path)
    (root / "src" / "ayuda.py").write_text(
        '"""Este modulo cuenta cosas y devuelve el total de todas ellas, porque el\n'
        "verificador necesita bastante texto para poder juzgar de que idioma se\n"
        "trata, y con dos palabras no hay nada que medir en absoluto todavia.\n"
        'Aqui hay mas texto para que la cuenta de palabras pase del minimo."""\n\n' + GENERIC,
        encoding="utf-8",
    )

    result = portable.survey(root, expect_language="english")

    candidate = result.candidates[0]
    assert candidate.verdict == "portable-after-edit"
    assert candidate.language_note is not None
    assert "spanish" in candidate.language_note


def test_the_number_of_files_weighed_is_always_reported(tmp_path: Path) -> None:
    """"Nothing portable" over nothing scanned is not a clean repository, and
    the two are indistinguishable without the count."""
    root = project(tmp_path)

    result = portable.survey(root)

    assert result.candidates == []
    assert result.scanned == 0


# --- the entry point renders our own failures -------------------------------


def test_a_refusal_is_a_message_not_a_traceback() -> None:
    """Rule: our own errors reach the terminal as messages with an exit code.

    `agentctl` had no handler at all, so `portable` on a repository with no
    declared vocabulary printed a syntax-highlighted traceback pointing at
    `cli.py` — which reads as *this tool is broken* rather than *configure it*,
    even though the message already said exactly what to add.
    """
    import shutil
    import subprocess
    import sys

    binary = shutil.which("agentctl") or str(
        Path(sys.executable).parent / "agentctl"
    )
    if not Path(binary).exists():
        pytest.skip("agentctl is not installed in this environment")

    result = subprocess.run(
        [binary, "portable", str(Path(__file__).resolve().parents[3])],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    combined = result.stdout + result.stderr
    assert "Traceback" not in combined, "our own errors must render as messages"
    assert "terms" in combined, "and must still say what to add"
    assert result.returncode == 5, "a ValidationError keeps its own exit code"


def test_a_usage_error_exits_two_rather_than_crashing() -> None:
    """The control: the handler must not turn every failure into exit 5."""
    import shutil
    import subprocess
    import sys

    binary = shutil.which("agentctl") or str(Path(sys.executable).parent / "agentctl")
    if not Path(binary).exists():
        pytest.skip("agentctl is not installed in this environment")

    result = subprocess.run(
        [binary, "no-such-command"], capture_output=True, text=True, timeout=60, check=False
    )

    assert result.returncode == 2
    assert "Traceback" not in result.stdout + result.stderr
