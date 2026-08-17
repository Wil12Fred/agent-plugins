"""Error taxonomy shared by every CLI.

Each error carries the exit code the CLI should return, so an agent reading
``$?`` can tell "the ticket does not exist" (4) from "the token is dead" (3)
from "you tried to write to prod without confirming" (5).
"""

from __future__ import annotations


def _command_path_now() -> str:
    """The running command's path, or "" outside a CLI.

    Imported lazily so `opscore.errors` stays importable in a library with no
    CLI framework installed, and so this module does not import `output` at
    module scope (which imports it back).
    """
    from opscore.output import click_command_path

    return click_command_path()


class BridgeError(Exception):
    """Base class for every expected, reportable failure."""

    exit_code: int = 1

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.command_path = _command_path_now()
        """Which command was running when this was raised.

        Recorded here, at construction, because that is the only moment it is
        knowable. An error is raised *inside* the command body, where Click's
        context stack is live; by the time the entry point catches it the stack
        has unwound, so the renderer asking "which command am I in?" is asking
        after the answer is gone. That produced envelopes where the success path
        named the command and the failure path did not — and the failure path is
        the one where a caller needs the name.
        """

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"error": type(self).__name__, "message": self.message}
        if self.detail:
            payload["detail"] = self.detail
        return payload


class ConfigError(BridgeError):
    """A required setting or secret is missing."""

    exit_code = 2


class AuthenticationError(BridgeError):
    """Login failed or the token was rejected.

    Deliberately **not** an :class:`ApiError` subclass — a rejected credential
    is a different problem from an upstream fault, and collapsing them once made
    a probe report a working check as removed. It carries ``status_code`` all
    the same, so a caller that needs to tell 401 from 403 does not have to parse
    the message.
    """

    exit_code = 3

    def __init__(
        self,
        message: str,
        *,
        detail: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message, detail=detail)
        self.status_code = status_code

    def as_dict(self) -> dict[str, object]:
        payload = super().as_dict()
        if self.status_code is not None:
            payload["status_code"] = self.status_code
        return payload


class NotFoundError(BridgeError):
    """The requested resource does not exist."""

    exit_code = 4


class GuardError(BridgeError):
    """A safety guard refused the operation (prod write, forbidden SQL, ...)."""

    exit_code = 5


class ApiError(BridgeError):
    """An upstream HTTP call failed."""

    exit_code = 6

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message, detail=body)
        self.status_code = status_code

    def as_dict(self) -> dict[str, object]:
        payload = super().as_dict()
        if self.status_code is not None:
            payload["status_code"] = self.status_code
        return payload


class UpstreamTimeoutError(ApiError):
    """The call did not answer in time. **This is not proof it did not happen.**

    Named for what timed out rather than shadowing the builtin ``TimeoutError``:
    a module that imports one and catches the other gets a retry guard that
    silently never fires, and this workspace already had a bare
    ``raise TimeoutError(...)`` sitting beside browser retry logic.

    A write can fan out into background work and take longer than the client
    waits — creating a lesson has been observed exceeding 30s *while
    succeeding*. Distinguishing this from a clean failure is the whole point:
    a blind retry on a non-idempotent write double-creates. Verify with a
    follow-up GET before retrying.

    Subclasses :class:`ApiError`, so existing handlers keep working.
    """

    exit_code = 8


class UnexpectedError(BridgeError):
    """An error the CLI does not model.

    Exists so that even a crash answers with an envelope: a ``--json`` caller
    that gets a traceback on stdout has no way to tell failure from corruption.
    """

    exit_code = 70


class QueryError(BridgeError):
    """The database rejected the statement (bad identifier, syntax, timeout)."""

    exit_code = 9


class ValidationError(BridgeError):
    """User input did not pass validation."""

    exit_code = 7
