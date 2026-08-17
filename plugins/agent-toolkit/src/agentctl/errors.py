"""The failure shapes this tool can produce, and their exit codes.

Deliberately small. A tool that measures things has two ways to fail — it was
asked for something impossible, or the environment it needs is not there — and
inventing a taxonomy beyond that is inventing precision nobody uses.

Every one carries an optional `detail`, and it is not decoration: an error that
says what went wrong without saying what to do about it makes the reader search
for the fix. "klembord is not installed" is half an error; "install the
clipboard extra" is the other half.
"""

from __future__ import annotations


class AgentctlError(Exception):
    """Base for anything this tool raises on purpose."""

    exit_code = 1

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.message} — {self.detail}" if self.detail else self.message


class ConfigError(AgentctlError):
    """The environment cannot do what was asked — a missing binary, no display."""

    exit_code = 2


class UsageError(AgentctlError):
    """The request itself is impossible, whatever the environment."""

    exit_code = 3


class NotFoundError(AgentctlError):
    """What was asked for does not exist — a file, a page, a package.

    Separate from `UsageError` because the caller often wants to branch: a
    missing optional input is normal, a malformed request is not.
    """

    exit_code = 4


class ValidationError(AgentctlError):
    """Input reached the tool in a shape it cannot work with."""

    exit_code = 5


class ApiError(AgentctlError):
    """Something this tool shells out to, or calls, failed on its own terms."""

    exit_code = 6
