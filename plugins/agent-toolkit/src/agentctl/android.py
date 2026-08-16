"""Drive any Android app on a headless emulator, over adb.

On a Linux host with ``/dev/kvm`` a headless emulator boots fast enough to drive
from an agent loop, over SSH, with no display attached — which makes "look at
the screen and decide what to tap" a thing a script can do.

The interaction loop
--------------------
``shot`` → look at the PNG → decide coordinates → ``tap``/``text`` → repeat.

Three things that silently corrupt a run, all handled here:

* **Coordinates are AVD-native.** The default AVD geometry is a Pixel 6 at
  1080x2400, and every coordinate is in that space. A screenshot shown to you
  downscaled (say at 900 px wide) needs its coordinates multiplied by
  ``native / shown`` first — :func:`scale_point` does that arithmetic, and
  forgetting it puts every tap in the wrong place by a consistent ratio, which
  looks like the app ignoring you.
* **``adb shell input text X`` runs X through the *device* shell.** ``$ & ( ) ;
  ' "`` get eaten or interpreted. This corrupted a password once and the app
  answered "usuario y/o contraseña incorrectos", which reads exactly like a
  wrong credential. Spaces become ``%s`` **and** the whole argument is
  shell-quoted — see :func:`quote_input_text`.
* **The soft keyboard shifts the layout.** Tapping the next field while the
  keyboard is open lands the tap somewhere else — classically appending the
  confirm-password into the password field. Always hide the keyboard
  (``keyevent 4``) between a type and the next tap.

Secrets: anything typed with :meth:`Emulator.type_text` is never echoed. But a
screenshot taken right afterwards shows every unmasked field in clear — do not
share those PNGs unedited.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from agentctl.errors import AgentctlError, ConfigError, UsageError


def require_emulator() -> Path:
    """Locate the `emulator` binary, the same three ways as `adb`."""
    explicit = os.environ.get("ANDROID_EMULATOR")
    if explicit:
        return Path(explicit)
    sdk = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")
    if sdk and (candidate := Path(sdk) / "emulator" / "emulator").is_file():
        return candidate
    found = shutil.which("emulator")
    if not found:
        raise ConfigError(
            "the `emulator` binary was not found",
            detail="install it with sdkmanager, or set ANDROID_EMULATOR / ANDROID_SDK_ROOT",
        )
    return Path(found)


def require_adb() -> str:
    """Locate `adb`: `ANDROID_ADB`, then the SDK, then `PATH`.

    Three places because the SDK is installed three different ways and a tool
    that only checks `PATH` reports a working machine as broken.
    """
    explicit = os.environ.get("ANDROID_ADB")
    if explicit:
        return explicit
    sdk = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")
    if sdk and (candidate := Path(sdk) / "platform-tools" / "adb").is_file():
        return str(candidate)
    found = shutil.which("adb")
    if not found:
        raise ConfigError(
            "adb not found",
            detail="set ANDROID_ADB, or ANDROID_SDK_ROOT, or put adb on PATH",
        )
    return found


ADB_TIMEOUT = 60.0
"""No adb call may block forever.

`adb logcat -d` prints "- waiting for device -" and waits indefinitely when no
emulator is attached. These are read-only MCP tools: an agent that calls one
would hang permanently.
"""


DEFAULT_SERIAL = "emulator-5554"
DEFAULT_AVD = os.environ.get("ANDROID_AVD", "")
"""The AVD to boot. Unset by default: booting somebody else's emulator is a
surprise, and there is no sensible name to guess."""
DEFAULT_APK_RELATIVE = os.environ.get("ANDROID_APK", "build/app/outputs/flutter-apk/app-debug.apk")
"""Where a debug build lands. The default is Flutter's; override for Gradle."""

NATIVE_RESOLUTION = (1080, 2400)
"""Pixel 6 — the geometry every coordinate map in this package assumes."""

BOOT_TIMEOUT_SECONDS = 240
BOOT_POLL_SECONDS = 4

# Headless, no snapshot, software GPU: the combination that boots reliably with
# no display attached. `-accel on` uses /dev/kvm, without which boot takes
# minutes rather than seconds.
EMULATOR_FLAGS = (
    "-no-window",
    "-no-audio",
    "-no-boot-anim",
    "-gpu",
    "swiftshader_indirect",
    "-no-snapshot",
    "-accel",
    "on",
)

# Android keyevents used across the flows.
KEY_BACK = 4
"""Also closes the soft keyboard — the reason it appears between every field."""
KEY_ENTER = 66
KEY_MOVE_END = 123
KEY_DEL = 67


def scale_point(
    x: int,
    y: int,
    *,
    shown: tuple[int, int],
    native: tuple[int, int] = NATIVE_RESOLUTION,
) -> tuple[int, int]:
    """Convert coordinates read off a downscaled screenshot to AVD-native ones.

    Reading a 1080x2400 screenshot that a tool rendered at 900x2000 and tapping
    the coordinates as-is misses every target by 20%.
    """
    if shown[0] <= 0 or shown[1] <= 0:
        raise ConfigError("the shown resolution must be positive")
    return round(x * native[0] / shown[0]), round(y * native[1] / shown[1])


def quote_input_text(value: str) -> str:
    """Make ``value`` survive ``adb shell input text``.

    Spaces are the argument separator for ``input``, so they become ``%s``;
    everything else is quoted for the device's shell. Both are required —
    quoting alone turns a space into a literal space that ``input`` then splits
    on, and escaping alone leaves ``$``/``&``/``;`` to the shell.
    """
    return shlex.quote(value.replace(" ", "%s"))


@dataclass
class Emulator:
    """An adb connection to one emulator (or attached device)."""

    serial: str = DEFAULT_SERIAL
    adb: str = ""
    """Path to `adb`. Resolved on first use when empty."""

    project: Path | None = None
    """The app project, for `default_apk()`. No default: the build output lives
    wherever the build system puts it, and guessing produces a "file not found"
    that reads like a failed build."""

    # --- plumbing ----------------------------------------------------------
    def _run(self, args: list[str], *, check: bool = True) -> str:
        adb = self.adb or require_adb()
        try:
            completed = subprocess.run(
                [str(adb), "-s", self.serial, *args],
                capture_output=True,
                text=True,
                timeout=ADB_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgentctlError(
                f"adb {' '.join(args[:2])} timed out after {ADB_TIMEOUT}s on {self.serial}",
                detail="is the emulator running? `agentctl android boot`",
            ) from exc
        if check and completed.returncode != 0:
            raise AgentctlError(
                f"adb {' '.join(args[:2])} failed on {self.serial}",
                detail=(completed.stderr or completed.stdout).strip()[:500] or None,
            )
        return completed.stdout

    def _run_bytes(self, args: list[str]) -> bytes:
        adb = self.adb or require_adb()
        completed = subprocess.run(
            [str(adb), "-s", self.serial, *args],
            capture_output=True,
            timeout=ADB_TIMEOUT,
            check=False,
        )
        if completed.returncode != 0:
            raise AgentctlError(
                f"adb {' '.join(args[:2])} failed on {self.serial}",
                detail=completed.stderr.decode(errors="replace").strip()[:500] or None,
            )
        return completed.stdout

    # --- lifecycle ---------------------------------------------------------
    def boot(self, avd: str = DEFAULT_AVD, *, timeout: int = BOOT_TIMEOUT_SECONDS) -> int:
        """Start the AVD headless and block until ``sys.boot_completed`` is 1.

        Returns the emulator's pid. The process outlives this command on
        purpose: every other subcommand reuses the running emulator, and
        rebooting between taps would make a flow unworkably slow.
        """
        emulator = require_emulator()
        if not emulator.is_file():
            raise ConfigError(
                f"emulator binary not found at {emulator}",
                detail="install it with sdkmanager, or set ANDROID_HOME",
            )
        process = subprocess.Popen(
            [str(emulator), "-avd", avd, *EMULATOR_FLAGS],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"emulator '{avd}' launching (pid {process.pid})", file=sys.stderr)
        self._run(["wait-for-device"], check=False)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._run(["shell", "getprop", "sys.boot_completed"], check=False).strip() == "1":
                return process.pid
            time.sleep(BOOT_POLL_SECONDS)
        raise AgentctlError(f"emulator '{avd}' did not finish booting within {timeout}s")

    def kill(self) -> None:
        """Shut the emulator down (``adb emu kill``)."""
        self._run(["emu", "kill"], check=False)

    def install(self, apk: Path) -> str:
        """``adb install -r`` — reinstall keeping app data."""
        if not apk.is_file():
            raise UsageError(
                f"APK not found: {apk}",
                detail=("build it first, or pass --apk --dart-define .config/dev.env.json"),
            )
        return self._run(["install", "-r", str(apk)]).strip()

    def default_apk(self) -> Path:
        if self.project is None:
            raise ConfigError(
                "no project: pass --apk, or construct Emulator(project=…)",
                detail="the built APK lives under the project, and there is none to assume",
            )
        return self.project / DEFAULT_APK_RELATIVE

    def packages(self) -> list[str]:
        out = self._run(["shell", "pm", "list", "packages"], check=False)
        return [line.replace("package:", "").strip() for line in out.splitlines() if line.strip()]

    def detect_package(self, match: str = "") -> str:
        match = (match or os.environ.get("ANDROID_PACKAGE_MATCH", "")).lower()
        if not match:
            raise ConfigError(
                "no package filter: set ANDROID_PACKAGE_MATCH or pass --package",
                detail="an empty filter would match the first system package on the list",
            )
        """Find the installed package whose id contains `ANDROID_PACKAGE_MATCH`.

        The app has no Gradle flavors, so the base install is
        ``com.example.app`` while a branded build carries the
        brand's own id — matching on the vendor prefixes covers both.
        """
        for package in self.packages():
            lowered = package.lower()
            if match and match in lowered:
                return package
        raise UsageError(
            f"no installed package matches {match!r}",
            detail="set ANDROID_PACKAGE_MATCH, or pass --package explicitly",
        )

    def launch(self, package: str | None = None) -> str:
        target = package or self.detect_package()
        self._run(["shell", "monkey", "-p", target, "-c", "android.intent.category.LAUNCHER", "1"])
        return target

    def clear(self, package: str) -> None:
        """Wipe app data — the reliable way back to the welcome screen."""
        self._run(["shell", "pm", "clear", package], check=False)

    # --- interaction -------------------------------------------------------
    def screenshot(self, out_path: Path) -> Path:
        raw = self._run_bytes(["exec-out", "screencap", "-p"])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(raw)
        return out_path

    def tap(self, x: int, y: int, *, wait: float = 0.0) -> None:
        self._run(["shell", "input", "tap", str(x), str(y)])
        if wait:
            time.sleep(wait)

    def tap_named(
        self, coords: dict[str, tuple[int, int]], name: str, *, wait: float = 2.0
    ) -> None:
        if name not in coords:
            raise ConfigError(f"unknown tap target '{name}'")
        x, y = coords[name]
        self.tap(x, y, wait=wait)

    def type_text(self, value: str, *, wait: float = 0.0) -> None:
        """Type into the focused field. The value is never echoed."""
        self._run(["shell", f"input text {quote_input_text(value)}"])
        if wait:
            time.sleep(wait)

    def key(self, code: int, *, wait: float = 0.0) -> None:
        self._run(["shell", "input", "keyevent", str(code)])
        if wait:
            time.sleep(wait)

    def hide_keyboard(self, *, wait: float = 2.0) -> None:
        """Close the soft keyboard so the next tap hits what you aimed at."""
        self.key(KEY_BACK, wait=wait)

    def clear_field(self, *, characters: int = 30) -> None:
        """Empty the focused field: jump to the end, then backspace over it."""
        self.key(KEY_MOVE_END)
        for _ in range(characters):
            self.key(KEY_DEL)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, *, duration_ms: int = 300) -> None:
        self._run(["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms)])

    def logcat(self, *, grep: str | None = None, tail: int = 8000) -> str:
        out = self._run(["logcat", "-d"], check=False)
        if grep:
            out = "\n".join(line for line in out.splitlines() if grep.lower() in line.lower())
        return out[-tail:]

    # --- flows -------------------------------------------------------------
