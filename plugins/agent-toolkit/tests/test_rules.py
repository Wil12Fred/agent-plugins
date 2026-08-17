"""The rules a repository declares, and the one kind of rule we can measure.

Two things are being defended here, and they pull in opposite directions:

* a declared rule must actually be enforceable, or declaring it is theatre;
* a rule we *cannot* enforce must be reported as unenforced, never as passed.

Most of these tests are about the second. A checker that quietly skips what it
cannot measure produces a clean report for a repository it never looked at.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentctl import rules
from agentctl.errors import ValidationError

SPANISH = """
Este documento esta escrito enteramente en castellano porque el verificador
necesita un caso donde la respuesta correcta sea que la regla se incumple. Sin
un caso asi, cualquier funcion que devuelva siempre la respuesta favorable
pasaria todas las comprobaciones sin medir absolutamente nada de nada, y el
informe resultante no distinguiria un repositorio limpio de uno sin revisar.
"""

ENGLISH = """
This document is written entirely in English, and it is the control: without a
case whose correct answer is that the rule holds, every assertion in this file
would also pass against a checker that simply reports a violation for whatever
it is handed, which would be exactly as useless as the opposite mistake.
"""


def declare(tmp_path: Path, body: str) -> Path:
    (tmp_path / ".agent-rules.toml").write_text(body, encoding="utf-8")
    return tmp_path


LANGUAGE_RULE = """
[[rule]]
name = "english-only"
kind = "language"
expect = "english"
rule = "Everything is written in English."
exempt = ["quoted/**"]
"""


# --- reading the declaration ------------------------------------------------


def test_a_repository_with_no_declaration_is_not_an_error(tmp_path: Path) -> None:
    """Rule: most repositories have declared nothing, and that is a valid answer.

    The distinction matters in a report: "has not been given rules" is not the
    same claim as "has no standards", and an auditor that cannot tell them apart
    invents findings to fill the silence.
    """
    declaration = rules.load(tmp_path)

    assert declaration.rules == ()
    assert declaration.declaration_file is None


def test_prose_documents_are_listed_to_be_read_not_parsed(tmp_path: Path) -> None:
    """Rule: prose is a source to read, never a rule we pretend to have parsed.

    Manufacturing a structured rule out of a paragraph is how an auditor ends up
    enforcing something nobody wrote — a failure already on this repository's
    record.
    """
    (tmp_path / "AGENTS.md").write_text("# how we work\n", encoding="utf-8")
    (tmp_path / "CONTRIBUTING.md").write_text("# contributing\n", encoding="utf-8")

    declaration = rules.load(tmp_path)

    assert declaration.prose_sources == ("AGENTS.md", "CONTRIBUTING.md")
    assert declaration.rules == (), "prose must not become a rule"


def test_a_malformed_declaration_fails_loudly(tmp_path: Path) -> None:
    """Rule: a broken config must not read as "no rules".

    Silently returning an empty declaration would report a repository with a
    typo in its config as one that never had standards — a clean bill of health
    produced by the failure itself.
    """
    declare(tmp_path, "[[rule]\nname = 'oops'")

    with pytest.raises(ValidationError, match="does not parse"):
        rules.load(tmp_path)


def test_an_unknown_kind_is_refused_rather_than_ignored(tmp_path: Path) -> None:
    declare(tmp_path, '[[rule]]\nname = "x"\nkind = "vibes"\nrule = "be nice"\n')

    with pytest.raises(ValidationError, match="unknown kind"):
        rules.load(tmp_path)


def test_a_rule_with_no_words_is_refused(tmp_path: Path) -> None:
    """An auditor quotes the rule back to the reader; an empty one cannot be
    reported, so it would be enforced invisibly or not at all."""
    declare(tmp_path, '[[rule]]\nname = "x"\nkind = "process"\n')

    with pytest.raises(ValidationError, match="no `rule` text"):
        rules.load(tmp_path)


# --- scope, and the exception that is part of the rule ----------------------


def test_an_exemption_wins_over_an_inclusion() -> None:
    """Rule: a path matched by both is exempt.

    Resolving the overlap the other way makes every exception silently inert —
    and an exception is written precisely to carve something out. "Everything is
    English" and "everything is English except what was quoted from a ticket"
    are different rules, and holding a repository to the first is wrong.
    """
    rule = rules.Rule(
        name="r",
        kind="language",
        rule="english",
        applies_to=["docs/**"],
        exempt=["docs/quoted/**"],
    )

    assert rule.governs("docs/guide.md")
    assert not rule.governs("docs/quoted/ticket.md")


def test_no_scope_means_the_whole_repository() -> None:
    """The control for the test above: without it, an always-False `governs`
    would satisfy every exemption assertion here."""
    rule = rules.Rule(name="r", kind="language", rule="english")

    assert rule.governs("anything/at/all.py")


# --- measuring, and refusing to pretend -------------------------------------


def test_a_file_in_the_wrong_language_is_reported(tmp_path: Path) -> None:
    declare(tmp_path, LANGUAGE_RULE)
    (tmp_path / "es.md").write_text(SPANISH, encoding="utf-8")

    result = rules.check(tmp_path, rules.load(tmp_path))

    assert [v.path for v in result.violations] == ["es.md"]
    assert "spanish" in result.violations[0].detail


def test_a_file_in_the_right_language_is_not_reported(tmp_path: Path) -> None:
    """The control. Without it, a checker that flags everything passes the test
    above."""
    declare(tmp_path, LANGUAGE_RULE)
    (tmp_path / "en.md").write_text(ENGLISH, encoding="utf-8")

    result = rules.check(tmp_path, rules.load(tmp_path))

    assert result.violations == []
    assert result.checked["english-only"] == 1, "it must have actually weighed the file"


def test_an_exempt_file_is_left_alone(tmp_path: Path) -> None:
    """The user-facing case this whole design exists for: text quoted into a
    ticket or a chat keeps the language it was written in."""
    declare(tmp_path, LANGUAGE_RULE)
    (tmp_path / "quoted").mkdir()
    (tmp_path / "quoted" / "ticket.md").write_text(SPANISH, encoding="utf-8")

    result = rules.check(tmp_path, rules.load(tmp_path))

    assert result.violations == []
    assert result.checked["english-only"] == 0


def test_the_number_of_files_weighed_is_always_reported(tmp_path: Path) -> None:
    """Rule: "no violations" over zero files is not a pass.

    The two are indistinguishable without the count, and that is the single most
    common way a gate reports success while measuring nothing.
    """
    declare(tmp_path, LANGUAGE_RULE)

    result = rules.check(tmp_path, rules.load(tmp_path))

    assert result.violations == []
    assert result.checked == {"english-only": 0}


def test_a_rule_this_tool_cannot_measure_is_named_not_passed(tmp_path: Path) -> None:
    """Rule: an unmeasurable rule is listed as unmeasured.

    A silent pass and a real pass look identical in a report, and only one of
    them is true. Naming it sends the auditor to read the rule instead of
    trusting the blank.
    """
    declare(tmp_path, '[[rule]]\nname = "no-names"\nkind = "naming"\nrule = "no proper nouns"\n')

    result = rules.check(tmp_path, rules.load(tmp_path))

    assert result.unmeasurable == ["no-names"]
    assert "no-names" not in result.checked


def test_a_file_too_short_to_judge_is_skipped_rather_than_guessed(tmp_path: Path) -> None:
    """Rule: "not enough text" is a third answer, distinct from both verdicts.

    A three-word comment has no measurable language. Counting it either way is
    how a checker produces confident findings about nothing.
    """
    declare(tmp_path, LANGUAGE_RULE)
    (tmp_path / "tiny.md").write_text("hola\n", encoding="utf-8")

    result = rules.check(tmp_path, rules.load(tmp_path))

    assert result.violations == []
    assert result.checked["english-only"] == 0, "a skipped file must not count as weighed"


def test_only_comments_are_weighed_in_source_not_identifiers(tmp_path: Path) -> None:
    """Rule: identifiers are excluded from the language measurement.

    A ratio over identifiers is dominated by keywords and library names in
    whatever language the ecosystem uses, so it reports a thoroughly Spanish
    file as English. Comments are where the rule is really broken, and the only
    place the measurement is honest.
    """
    source = f'"""{SPANISH}"""\n\ndef get_user_by_id(user_id: int) -> None:\n    return None\n'

    prose = rules.prose_of(source, ".py")

    assert "castellano" in prose
    assert "get_user_by_id" not in prose
    assert rules.language_of(prose)[0] == "spanish"
