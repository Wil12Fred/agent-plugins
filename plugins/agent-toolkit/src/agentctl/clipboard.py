"""Put text on the clipboard, which is harder than it sounds.

Two backends, picked by what is actually running rather than by assumption.

**Klipper (KDE), preferred.** This is a Plasma session, and Plasma ships
Klipper, whose D-Bus interface takes ownership itself and keeps the entry in
the clipboard *history*. Nothing has to stay alive afterwards. Reached through
``qdbus6``/``qdbus``, present here as part of Plasma.

**XWayland selection hold, fallback.** There is no ``wl-copy``, ``xclip`` or
``xsel`` installed. What does exist is an XWayland server (``DISPLAY=:1``)
whose X11 PRIMARY/CLIPBOARD selections the compositor mirrors into the Wayland
clipboard — and the pure-Python ``klembord`` (python-xlib backend) can own
those selections with no external binary at all. The catch is how X11 works:
**a selection lives only while the process that owns it stays alive.** Setting
the clipboard and exiting leaves an empty clipboard, so this path holds the
selection open and blocks, exactly as ``wl-copy`` does.

That catch is not theoretical. On 2026-08-15 this command was used to hand over
an SSH public key with ``--hold-seconds 120``; the paste came later, the hold
had expired, and Klipper's history showed **an empty entry at the top** where
the key should have been. The clipboard reported success and carried nothing.
Hence two things here: Klipper first, and — on either backend that can be
read back — **the write is verified by reading it back**, because "the command
exited 0" was exactly the evidence that lied.

``klembord.set_text()`` mangles non-ASCII (accents disappear), so the raw
``UTF8_STRING`` / ``text/plain;charset=utf-8`` targets are registered directly.
For Spanish text that is not cosmetic: it is the difference between "Aquí está"
and "Aqu est".
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from enum import StrEnum
from pathlib import Path

from agentctl.errors import ConfigError

PID_FILE = Path(os.environ.get("TMPDIR", "/tmp")) / "agentctl-clipboard-owner.pid"
DEFAULT_DISPLAY = ":1"
POLL_SECONDS = 3600

QDBUS_BINARIES = ("qdbus6", "qdbus")
KLIPPER_SERVICE = "org.kde.klipper"
KLIPPER_OBJECT = "/klipper"
KLIPPER_TIMEOUT = 10


class Backend(StrEnum):
    """Which mechanism puts the text on the clipboard."""

    AUTO = "auto"
    KLIPPER = "klipper"
    X11 = "x11"

    @property
    def blocks(self) -> bool:
        """Whether using this backend means holding the process open."""
        return self is Backend.X11


def find_qdbus() -> str | None:
    """Return the qdbus binary to talk to Klipper with, if there is one."""
    for name in QDBUS_BINARIES:
        found = shutil.which(name)
        if found:
            return found
    return None


def klipper_call(qdbus: str, method: str, *args: str) -> str:
    """Call a Klipper D-Bus method and return its stdout, stripped.

    Raises:
        ConfigError: Klipper did not answer.
    """
    try:
        completed = subprocess.run(
            [qdbus, KLIPPER_SERVICE, KLIPPER_OBJECT, method, *args],
            capture_output=True,
            text=True,
            timeout=KLIPPER_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError(f"klipper {method} did not answer", detail=str(exc)) from exc
    if completed.returncode != 0:
        raise ConfigError(
            f"klipper {method} exited {completed.returncode}",
            detail=(completed.stderr or completed.stdout).strip()[:500],
        )
    return completed.stdout.strip()


def klipper_reachable(qdbus: str) -> bool:
    """Whether Klipper is actually on the session bus.

    Probed by calling it, not by looking for the Plasma process: a KDE session
    with Klipper's D-Bus interface disabled looks identical from the outside.
    """
    try:
        klipper_call(qdbus, "getClipboardContents")
    except ConfigError:
        return False
    return True


def resolve_backend(preferred: Backend = Backend.AUTO) -> Backend:
    """Decide which backend will be used, before anything is written.

    The caller needs this up front: the X11 backend blocks, so a ``--json``
    envelope has to be emitted before the call rather than after it.

    Raises:
        ConfigError: ``klipper`` was demanded and is not reachable.
    """
    if preferred is Backend.X11:
        return Backend.X11

    qdbus = find_qdbus()
    if qdbus and klipper_reachable(qdbus):
        return Backend.KLIPPER
    if preferred is Backend.KLIPPER:
        raise ConfigError(
            "Klipper is not reachable on the session bus",
            detail=(
                "no qdbus6/qdbus binary, or org.kde.klipper did not answer. "
                "Use --backend x11 to own the XWayland selection instead."
            ),
        )
    return Backend.X11


def copy_via_klipper(text: str, qdbus: str | None = None) -> None:
    """Hand the text to Klipper, then read it back to prove it landed.

    Raises:
        ConfigError: Klipper is unreachable, or kept something else.
    """
    binary = qdbus or find_qdbus()
    if binary is None:
        raise ConfigError(
            "no qdbus binary found",
            detail=f"looked for: {', '.join(QDBUS_BINARIES)}",
        )
    klipper_call(binary, "setClipboardContents", text)
    landed = klipper_call(binary, "getClipboardContents")
    # Compared stripped: qdbus prints the value with a trailing newline of its
    # own, and a file read with a final newline would otherwise never match.
    if landed != text.strip():
        raise ConfigError(
            "the clipboard does not hold what was just written",
            detail=f"wrote {len(text.strip())} characters, read back {len(landed)}",
        )


def replace_previous_owner(pid_file: Path = PID_FILE) -> int | None:
    """Kill a prior holder started by this command, so only one owner exists."""
    try:
        old_pid = int(pid_file.read_text().strip())
        os.kill(old_pid, signal.SIGTERM)
    except (FileNotFoundError, ProcessLookupError, ValueError, PermissionError):
        return None
    return old_pid


def encode_targets(text: str) -> dict[str, bytes]:
    """Build the X11 selection targets, preserving UTF-8 exactly."""
    encoded = text.encode("utf-8")
    return {"UTF8_STRING": encoded, "text/plain;charset=utf-8": encoded}


def copy(
    text: str,
    *,
    hold_seconds: int = 0,
    display: str | None = None,
    backend: Backend = Backend.AUTO,
) -> Backend:
    """Put ``text`` on the clipboard, blocking only if the X11 path is used.

    Args:
        text: what to place on the clipboard.
        hold_seconds: X11 backend only — release after this long; ``0`` holds
            indefinitely. Klipper needs no holding at all.
        display: X display to use; defaults to ``$DISPLAY`` or ``:1``.
        backend: which mechanism to use; ``AUTO`` prefers Klipper.

    Returns:
        The backend that was actually used.

    Raises:
        ConfigError: the chosen backend is unavailable (klembord is not
            installed, or Klipper did not answer).
    """
    chosen = resolve_backend(backend)
    if chosen is Backend.KLIPPER:
        copy_via_klipper(text)
        return chosen

    copy_via_x11(text, hold_seconds=hold_seconds, display=display)
    return chosen


def copy_via_x11(text: str, *, hold_seconds: int = 0, display: str | None = None) -> None:
    """Own the X11 selection with ``text`` and block until killed.

    Raises:
        ConfigError: klembord is not installed (``uv sync --extra clipboard``).
    """
    os.environ.setdefault("DISPLAY", display or DEFAULT_DISPLAY)
    if display:
        os.environ["DISPLAY"] = display

    try:
        import klembord
    except ImportError as exc:  # pragma: no cover - depends on the install extra
        raise ConfigError(
            "klembord is not installed",
            detail="install the extra: uv sync --all-packages --extra clipboard",
        ) from exc

    replace_previous_owner()
    klembord.init()
    klembord.set(encode_targets(text))
    PID_FILE.write_text(str(os.getpid()))

    signal.signal(signal.SIGTERM, lambda *_: raise_exit())
    deadline = time.monotonic() + hold_seconds if hold_seconds else None
    while deadline is None or time.monotonic() < deadline:
        time.sleep(min(POLL_SECONDS, hold_seconds or POLL_SECONDS))


def raise_exit() -> None:
    """SIGTERM handler: leave cleanly so the selection is released."""
    raise SystemExit(0)
