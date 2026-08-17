"""`jira create` — the contract AGENTS.md imposes on a new ticket.

The command shipped with no tests, its `--assignee` optional despite a commit
message claiming it was required, and `--field` sending a string where the one
example in its own help text (a sprint id) needs an array.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

# These shell out to the installed binary and reach JIRA: resolving an assignee
# is an API call, so the payload cannot be built without credentials. They were
# unit tests in the repository this came from only because a configured `.env`
# happened to be sitting next to them — which is the quiet way an integration
# test gets counted as coverage it does not provide.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("JIRA_SITE"),
        reason="JIRA_SITE is not configured; these reach a real JIRA",
    ),
]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    # `--json` is a root option now, so it goes before the subcommand: a flag
    # that exists on some commands and not others is one a caller cannot loop
    # over. The tests pass it inline, so it is hoisted here.
    inline = [a for a in args if a != "--json"]
    root = ["--json"] if "--json" in args else []
    return subprocess.run(
        [str(pathlib.Path(sys.executable).parent / "jiractl"), *root, "create", *inline],
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_ticket_cannot_be_created_without_an_owner() -> None:
    # AGENTS.md's placement rule: assignee is mandatory. An issue created with
    # no owner is one nobody picks up.
    completed = _run("--project", "OPER", "--summary", "x", "--body", "y", "--json")
    assert completed.returncode != 0
    envelope = json.loads(completed.stdout)
    assert envelope["error"] == "ValidationError"
    assert "assignee" in envelope["message"]


def test_a_field_value_that_looks_like_json_is_sent_as_json() -> None:
    """Sprint is `customfield_10020` and takes an array of ints, not "2041"."""
    completed = _run(
        "--project",
        "OPER",
        "--summary",
        "x",
        "--body",
        "y",
        "--assignee",
        "cs",
        "--field",
        "customfield_10020=[2041]",
        "--dry-run",
        "--json",
    )
    fields = json.loads(completed.stdout)["data"]["fields"]
    assert fields["customfield_10020"] == [2041], "a string here silently sets no sprint"


def test_a_plain_field_value_stays_a_string() -> None:
    completed = _run(
        "--project",
        "OPER",
        "--summary",
        "x",
        "--body",
        "y",
        "--assignee",
        "cs",
        "--field",
        "customfield_10185=Cath",
        "--dry-run",
        "--json",
    )
    assert json.loads(completed.stdout)["data"]["fields"]["customfield_10185"] == "Cath"


def test_creating_is_guarded_and_named_external() -> None:
    completed = _run(
        "--project", "OPER", "--summary", "x", "--body", "y", "--assignee", "cs", "--json"
    )
    envelope = json.loads(completed.stdout)
    assert envelope["error"] == "GuardError"
    assert "un-sent" in (envelope.get("detail") or ""), "a new ticket is visible to other people"
