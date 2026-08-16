"""Preflight: refuse to start without what the bridge cannot work without.

Before this existed, `serve` connected to Slack, reported itself healthy, and
failed on the first dispatch because `tmux` was missing — which from Slack is
indistinguishable from the agent ignoring you.

Each test names the rule it enforces.
"""

from __future__ import annotations

import pytest

from slackbridge import requirements
from slackbridge.requirements import Level


def which(*present: str):
    """A stand-in for `shutil.which` where only `present` exists."""
    available = set(present)
    return lambda name: f"/usr/bin/{name}" if name in available else None


def result_for(name: str, *present: str) -> requirements.Result:
    found = {r.requirement.name: r for r in requirements.check(which=which(*present))}
    return found[name]


# --------------------------------------------------------------------------- #
# Required versus degraded — the distinction the whole module is for
# --------------------------------------------------------------------------- #


def test_a_missing_required_program_blocks_the_start() -> None:
    """Rule: without tmux the bridge cannot type into a session, so it must not start.

    Starting anyway is pretending to work, and the operator finds out one
    message at a time.
    """
    results = requirements.check(which=which("claude", "qdbus6"))

    blocked = requirements.blocking(results)
    assert [r.requirement.name for r in blocked] == ["tmux"]


def test_a_missing_optional_program_does_not_block() -> None:
    """Rule: nobody needs KDE to drive a session from their phone.

    Refusing here would be worse than the problem it reports.
    """
    results = requirements.check(which=which("claude", "tmux"))

    assert requirements.blocking(results) == []
    assert [r.requirement.name for r in requirements.missing(results)] == ["qdbus"]


def test_a_missing_optional_program_is_still_reported() -> None:
    """The other half: degraded must not mean silent.

    A reduced capability nobody was told about is the same failure one level
    down — the reply just quietly does less than expected.
    """
    results = requirements.check(which=which("claude", "tmux"))
    assert "qdbus" in requirements.explain(results)


def test_everything_present_blocks_nothing_and_reports_nothing() -> None:
    """The control. Without it, 'always blocking' would pass the tests above."""
    results = requirements.check(which=which("claude", "codex", "tmux", "qdbus6"))

    assert requirements.blocking(results) == []
    assert requirements.missing(results) == []
    assert requirements.explain(results) == ""


# --------------------------------------------------------------------------- #
# "Any of these will do"
# --------------------------------------------------------------------------- #


def test_either_agent_cli_satisfies_the_requirement() -> None:
    """Rule: the bridge drives Claude or Codex. Having one is enough."""
    assert result_for("an agent CLI", "codex", "tmux").satisfied
    assert result_for("an agent CLI", "claude", "tmux").satisfied


def test_no_agent_cli_at_all_blocks() -> None:
    """The control: 'any of' must still be able to fail."""
    assert result_for("an agent CLI", "tmux").blocking


def test_the_newer_qdbus_name_counts() -> None:
    """Rule: Plasma renamed it to `qdbus6`, and a check knowing one name reports
    a perfectly working machine as broken."""
    assert result_for("qdbus", "claude", "tmux", "qdbus6").binary == "qdbus6"
    assert result_for("qdbus", "claude", "tmux", "qdbus").binary == "qdbus"


# --------------------------------------------------------------------------- #
# The message
# --------------------------------------------------------------------------- #


def test_the_marker_avoids_square_brackets() -> None:
    """Rule: this text goes through Rich, which reads `[required]` as a style tag.

    It ate the marker out of the one message written to be read — caught by
    running the command, not by a test, which is why there is now a test.
    """
    text = requirements.explain(requirements.check(which=which()))

    assert "REQUIRED:" in text
    assert "[required]" not in text


def test_the_refusal_names_how_to_install_what_is_missing() -> None:
    """Rule: "tmux is not installed" is half an error.

    The other half is the command that fixes it. Without it the reader goes
    looking, and the tool has made them do the work.
    """
    text = requirements.explain(requirements.check(which=which("claude")))

    assert "install:" in text
    assert "pacman -S tmux" in text


def test_the_refusal_lists_every_missing_program_not_just_the_first() -> None:
    """Rule: an operator who fixes one, restarts, and is told about the next has
    been made to do the work twice for no reason."""
    text = requirements.explain(requirements.check(which=which()))

    assert "an agent CLI" in text
    assert "tmux" in text
    assert "qdbus" in text


@pytest.mark.parametrize("requirement", requirements.REQUIREMENTS, ids=lambda r: r.name)
def test_every_requirement_says_what_breaks_and_how_to_fix_it(
    requirement: requirements.Requirement,
) -> None:
    """Rule applied to the table itself: a row without a purpose or an install
    hint produces exactly the half-error this module exists to avoid."""
    assert requirement.purpose.strip()
    assert requirement.install.strip()
    assert requirement.candidates
    assert requirement.level in (Level.REQUIRED, Level.DEGRADED)
