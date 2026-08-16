"""Secret resolution: environment first, then the OS keyring.

Two hard rules, both from AGENTS.md:

* a secret is never a literal in source, and
* a secret value is never printed — not to stdout, not to logs, not into a
  JIRA comment. :func:`redact` exists so error paths can still say *which*
  secret was involved without leaking it.
"""

from __future__ import annotations

import os
import shutil
import subprocess

from opscore.errors import ConfigError
from opscore.output import get_output

# A redaction that shows four leading and four trailing characters gives away
# eight of a nine-character secret. Nothing is revealed below the first
# threshold, and the tail only appears once the value is long enough that a
# prefix+suffix pair is a fraction of it rather than most of it.
_MIN_LENGTH_FOR_ANY_HINT = 12
_MIN_LENGTH_FOR_SUFFIX = 24
_VISIBLE = 4

_DOTENV_LOADED = False


def from_env(name: str) -> str | None:
    """Read ``name`` from the environment, treating blank as absent.

    Loads the repo ``.env`` first. Without this, a caller that resolves a
    secret *before* anything touches ``Settings`` sees it as unset, because
    ``.env`` is only exported into the environment by ``load_env_file()`` —
    which produced a "missing CF_EMAIL" error for a variable that was sitting
    in ``.env`` all along.
    """
    _ensure_dotenv_loaded()
    value = os.environ.get(name)
    return value or None


def _ensure_dotenv_loaded() -> None:
    """Export the repo ``.env`` once, without importing settings at module load.

    The flag is set **after** a successful load. Setting it first meant a
    transient failure (a locked file, a partially written ``.env``) marked the
    load done forever, and every later secret lookup silently saw an empty
    environment. A real failure is reported once rather than swallowed: an
    unreadable ``.env`` is exactly the thing you want named when a credential
    then comes back missing.
    """
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    try:
        from opscore.env import load_env_file

        load_env_file()
    except OSError as exc:
        # Not fatal — the environment may already carry everything needed.
        get_output().warn(f"could not read .env ({exc.strerror}); using the environment as-is")
        return
    _DOTENV_LOADED = True


def from_keyring(service: str, account: str) -> str | None:
    """Read a secret from the GNOME keyring via ``secret-tool``.

    Returns ``None`` when ``secret-tool`` is unavailable or the entry is
    missing, so callers can fall through to another source.
    """
    if not shutil.which("secret-tool"):
        return None
    try:
        result = subprocess.run(
            ["secret-tool", "lookup", "service", service, "account", account],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def resolve(
    *,
    env_var: str,
    keyring: tuple[str, str] | None = None,
    required: bool = True,
    purpose: str | None = None,
) -> str | None:
    """Resolve one secret from ``env_var``, then optionally the keyring.

    Args:
        env_var: environment variable to try first.
        keyring: ``(service, account)`` pair for the ``secret-tool`` fallback.
        required: raise :class:`ConfigError` instead of returning ``None``.
        purpose: what the secret is for, included in the error message.

    Raises:
        ConfigError: when ``required`` and nothing resolved. The message names
            the variable, never the value.
    """
    value = from_env(env_var)
    if value is None and keyring is not None:
        value = from_keyring(*keyring)
    if value is None and required:
        hint = f" ({purpose})" if purpose else ""
        raise ConfigError(
            f"missing secret{hint}: set {env_var} in .env or the environment",
        )
    return value


def redact(value: str | None) -> str:
    """Render a secret safely for humans, never the body.

    The point is to tell *which* credential is loaded, not to reproduce any
    part of it, so short values reveal nothing at all::

        redact(None)                      -> "<unset>"
        redact("s3cret")                  -> "<set>"
        redact("xoxb-1234567890abcdef")   -> "xoxb…"
        redact("xoxb-" + "a" * 40)        -> "xoxb…aaaa"
    """
    if not value:
        return "<unset>"
    if len(value) < _MIN_LENGTH_FOR_ANY_HINT:
        return "<set>"
    if len(value) < _MIN_LENGTH_FOR_SUFFIX:
        return f"{value[:_VISIBLE]}…"
    return f"{value[:_VISIBLE]}…{value[-_VISIBLE:]}"


def warn_if_passed_on_the_command_line(*, flag: str, value: str | None, env_var: str) -> None:
    """Warn that a secret given as a CLI argument is not private.

    Anything in ``argv`` is readable by every process on the machine through
    ``/proc/<pid>/cmdline`` (``ps -ef`` shows it), and the shell records it in
    history. The flags exist because they are convenient for a one-off, so
    this warns rather than refuses — but it names the environment variable
    that keeps the value out of both places.
    """
    if value:
        get_output().warn(
            f"{flag} puts the secret in argv, where `ps` and shell history can see it "
            f"— prefer {env_var} in .env or the environment"
        )
