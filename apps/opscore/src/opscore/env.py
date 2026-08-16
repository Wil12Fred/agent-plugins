"""Where configuration comes from, and where the project starts.

Two functions, and both exist because the bridge shells out. A value that lives
only in a typed settings object is invisible to the `tmux` and `claude`
subprocesses this thing spawns, so the `.env` is exported into the process
environment once and everything downstream — typed reads, `os.environ`, child
processes — sees the same values.

**Real environment variables always win.** The file only fills in what is
missing, so `LOG_LEVEL=debug yourtool run` overrides the file without
editing it, which is what you want when testing against a second workspace.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_FILENAME = ".env"

ROOT_MARKERS = (".git", "pyproject.toml")
"""What makes a directory the project root. Checked walking upward from the cwd."""


def project_root(start: Path | None = None) -> Path:
    """The nearest ancestor that looks like a project root, or the cwd.

    Used to find the `.env` and, in one place, to locate the installed entry
    point when writing a service unit. Never assume a fixed layout: this runs
    from wherever the operator started it.
    """
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if any((candidate / marker).exists() for marker in ROOT_MARKERS):
            return candidate
    return current


def load_env_file(path: Path | None = None) -> int:
    """Export a `.env` into the process environment. Returns how many were set.

    Looks at `OPSCORE_ENV_FILE` first, then the project root's `.env`. A
    missing file is not an error — the bridge is perfectly usable with the
    variables exported some other way, and refusing to start without a file
    would break every containerised deployment.
    """
    override = os.environ.get("OPSCORE_ENV_FILE")
    env_file = path or (Path(override) if override else project_root() / ENV_FILENAME)
    if not env_file.is_file():
        return 0

    exported = 0
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
            exported += 1
    return exported
