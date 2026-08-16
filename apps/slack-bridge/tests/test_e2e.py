"""End-to-end: does this actually work on the machine it is installed on?

**These did not exist before.** The bridge shipped with 55 unit tests and not one
that touched a real binary, a real token or a real session — so every green run
proved the parsing was right and said nothing about whether the thing could run
at all. That gap is exactly how a missing `tmux` survived to be discovered one
Slack message at a time.

Everything here is **read-only**. Nothing posts to Slack, nothing dispatches to a
session, nothing writes. The one test that would need to post is described at the
bottom and deliberately not written, because a test that spams a real channel
gets muted, and a muted test is worse than none.

Run them with `pytest -m integration`. They are excluded from the default run
because they depend on the machine: a missing token or a missing binary **skips
with a reason**, it does not fail. A skip that says why is information; a failure
that only means "not configured here" trains people to ignore red.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from slackbridge import requirements

pytestmark = pytest.mark.integration

BINARY = "slackbridge"


def run(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """Invoke the installed entry point, the way an operator or systemd would.

    Through the binary rather than by importing: the entry point, the `.env`
    loading and the error handler are all part of what "does it work" means, and
    importing the module skips every one of them.
    """
    binary = shutil.which(BINARY) or str(Path(__file__).resolve().parents[1] / ".venv/bin" / BINARY)
    if not Path(binary).exists():
        pytest.skip(f"{BINARY} is not installed in this environment")
    return subprocess.run(
        [binary, *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def envelope(result: subprocess.CompletedProcess[str]) -> dict:
    """Parse the one JSON envelope on stdout, or fail saying what was there instead."""
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:  # pragma: no cover - only on a real failure
        pytest.fail(f"stdout was not one JSON envelope:\n{result.stdout[:2000]}")


# --------------------------------------------------------------------------- #
# It runs at all
# --------------------------------------------------------------------------- #


def test_the_entry_point_exists_and_answers() -> None:
    """The most basic claim, and one no unit test makes: the binary runs."""
    result = run("--help")

    assert result.returncode == 0
    assert "slackbridge" in result.stdout


def test_health_reports_the_machine_it_is_actually_on() -> None:
    """Rule: health must describe this machine, not a fixture.

    Its whole purpose is answering "why did my reply do nothing" before anyone
    reads a log, so a health check that cannot see the real binaries is theatre.
    """
    data = envelope(run("--json", "health"))["data"]

    assert set(data) >= {"claude_ok", "codex_ok", "live", "requirements", "can_start"}
    assert isinstance(data["live"], int)
    names = {r["requirement"] for r in data["requirements"]}
    assert names == {r.name for r in requirements.REQUIREMENTS}


def test_health_agrees_with_the_machine_about_every_requirement() -> None:
    """Cross-check: the reported table must match what is really on PATH.

    A self-consistent report is not evidence — the module could agree with itself
    while looking in the wrong place. This compares its answer against `shutil`
    independently.
    """
    data = envelope(run("--json", "health"))["data"]

    for row in data["requirements"]:
        found = any(shutil.which(c) for c in _candidates(row["requirement"]))
        assert row["satisfied"] is found, f"{row['requirement']}: reported {row}, PATH says {found}"


def _candidates(name: str) -> tuple[str, ...]:
    for requirement in requirements.REQUIREMENTS:
        if requirement.name == name:
            return requirement.candidates
    raise AssertionError(f"health reported an unknown requirement: {name}")


def test_can_start_is_false_exactly_when_something_required_is_missing() -> None:
    data = envelope(run("--json", "health"))["data"]
    blocked = [r for r in data["requirements"] if r["level"] == "required" and not r["satisfied"]]

    assert data["can_start"] is (not blocked)


# --------------------------------------------------------------------------- #
# The preflight, against the real machine
# --------------------------------------------------------------------------- #


def test_serve_refuses_when_a_required_program_is_missing(tmp_path: Path) -> None:
    """Rule: it must refuse *before* connecting, and say how to fix it.

    Driven with a PATH that genuinely lacks `tmux` rather than a mock, because
    the failure being prevented is environmental — a mocked `which` proves the
    branch, not that the branch is reached from a real invocation.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("claude", "python3", "sh"):
        real = shutil.which(name)
        if real:
            (fake_bin / name).symlink_to(real)
    if not (fake_bin / "claude").exists():
        pytest.skip("no agent CLI installed, so the required-agent case would fire first")

    binary = shutil.which(BINARY) or str(Path(__file__).resolve().parents[1] / ".venv/bin" / BINARY)
    result = subprocess.run(
        [binary, "serve", "--confirm-prod-write"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, "PATH": str(fake_bin)},
    )

    assert result.returncode == 2, f"expected a refusal, got {result.returncode}"
    combined = result.stdout + result.stderr
    assert "tmux" in combined
    assert "install" in combined, "a refusal without the fix is half an error"
    assert "Traceback" not in combined, "our own errors must render as messages"


def test_a_refusal_names_every_missing_program_not_only_the_blocking_one(tmp_path: Path) -> None:
    """An operator who fixes one, restarts, and is told about the next has been
    made to do the work twice."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    real = shutil.which("claude")
    if not real:
        pytest.skip("no agent CLI installed")
    (fake_bin / "claude").symlink_to(real)

    binary = shutil.which(BINARY) or str(Path(__file__).resolve().parents[1] / ".venv/bin" / BINARY)
    result = subprocess.run(
        [binary, "serve", "--confirm-prod-write"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, "PATH": str(fake_bin)},
    )
    combined = result.stdout + result.stderr

    assert "tmux" in combined
    assert "qdbus" in combined, "the optional one is reported too, or it is discovered later"


# --------------------------------------------------------------------------- #
# Configuration, resolved for real — still without a network call
# --------------------------------------------------------------------------- #


def test_check_builds_the_app_without_touching_slack() -> None:
    """Rule: `check` proves the wiring offline.

    It is the command an operator runs before `serve`, so if it needed the
    network it could not do its job — you cannot debug a token by needing the
    token to work.
    """
    if not os.environ.get("SLACK_BOT_TOKEN"):
        pytest.skip("SLACK_BOT_TOKEN is not set, so there is no app to build")

    result = run("--json", "check")
    data = envelope(result)

    assert data["ok"] is True
    assert result.returncode == 0


def test_no_token_ever_reaches_stdout() -> None:
    """Rule: a token in a transcript outlives the session that printed it.

    The commands redact; this proves the redaction from the outside, on whatever
    is actually configured here.
    """
    for token_var in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_TOKEN"):
        secret = os.environ.get(token_var)
        if not secret or len(secret) < 12:
            continue
        for command in (["--json", "health"], ["--help"]):
            result = run(*command)
            assert secret not in result.stdout, f"{token_var} leaked into stdout of {command}"
            assert secret not in result.stderr, f"{token_var} leaked into stderr of {command}"


def test_the_service_unit_resolves_paths_on_this_machine() -> None:
    """Rule: the unit is generated, not shipped, because paths differ per machine.

    A unit copied from someone else's machine starts and then cannot find either
    agent binary — the exact failure the command exists to prevent.
    """
    result = run("service-unit")

    assert result.returncode == 0
    assert "ExecStart=" in result.stdout
    exec_line = next(line for line in result.stdout.splitlines() if line.startswith("ExecStart="))
    binary = exec_line.removeprefix("ExecStart=").split()[0]
    assert Path(binary).is_absolute(), "a relative ExecStart does not survive systemd"
    assert Path(binary).exists(), f"the unit points at {binary}, which is not there"


# --------------------------------------------------------------------------- #
# Sessions, read-only
# --------------------------------------------------------------------------- #


def test_listing_sessions_reads_the_real_agent_state() -> None:
    """Rule: session discovery is the half that cannot be unit-tested.

    It reads another program's on-disk state, so a fixture proves only that the
    parser handles the shape somebody wrote down. An empty list is a pass — the
    claim is that it reads without error, not that sessions exist.
    """
    if not shutil.which(os.environ.get("CLAUDE_BIN", "claude")):
        pytest.skip("no Claude CLI installed")

    result = run("--json", "sessions", "list", timeout=120)
    data = envelope(result)

    assert data["ok"] is True
    assert isinstance(data["data"], (list, dict))


# --------------------------------------------------------------------------- #
# What is deliberately not tested here
# --------------------------------------------------------------------------- #
#
# A full round trip — post to the channel, let the listener dispatch it, read the
# reply back — would be the only test that proves the whole thing. It is not
# here, on purpose:
#
#   * it posts to a real channel, and a test that spams a workspace gets muted;
#   * it dispatches to a real agent session, which costs tokens and can take
#     minutes;
#   * it needs a workspace nobody else is using, which no CI has.
#
# If it is ever written it belongs behind its own marker and its own confirmation
# flag, never in `-m integration`. Recording *why* it is absent matters more than
# the absence: a reader who does not find it should learn that it was considered,
# not assume it was forgotten.
