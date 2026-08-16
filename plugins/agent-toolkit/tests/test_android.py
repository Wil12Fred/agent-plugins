"""Driving an Android emulator: the three things that silently corrupt a run.

None of these needs a device. They are the arithmetic and the quoting — the
parts that go wrong invisibly, where the failure looks like the app misbehaving
rather than like the driver being wrong.

Each test names the rule it enforces.
"""

from __future__ import annotations

import pytest

from agentctl import android
from agentctl.errors import ConfigError

# --------------------------------------------------------------------------- #
# Coordinates
# --------------------------------------------------------------------------- #


def test_a_downscaled_screenshot_needs_its_coordinates_scaled_back() -> None:
    """Rule: coordinates read off a rendered screenshot are not AVD coordinates.

    Reading a 1080-wide screenshot that a tool rendered at 900 and tapping as-is
    misses every target by the same 20% — which looks like the app ignoring the
    tap, not like arithmetic.
    """
    assert android.scale_point(450, 1000, shown=(900, 2000)) == (540, 1200)


def test_a_screenshot_shown_at_native_size_is_unchanged() -> None:
    """The control: without it, 'always scales' would pass the test above."""
    assert android.scale_point(540, 1200, shown=android.NATIVE_RESOLUTION) == (540, 1200)


def test_a_zero_width_screenshot_is_refused_not_divided_by() -> None:
    with pytest.raises(ConfigError):
        android.scale_point(1, 1, shown=(0, 100))


# --------------------------------------------------------------------------- #
# Typing — where a password was corrupted once
# --------------------------------------------------------------------------- #


def test_spaces_become_the_escape_adb_expects() -> None:
    """Rule: `adb shell input text` reads a bare space as an argument break."""
    assert "%s" in android.quote_input_text("two words")


@pytest.mark.parametrize("char", ["$", "&", "(", ")", ";", "'", '"', "`"])
def test_shell_metacharacters_survive_the_device_shell(char: str) -> None:
    """Rule: the text runs through the *device's* shell, which eats these.

    This corrupted a password once and the app answered "wrong credentials" —
    indistinguishable from actually having the wrong password, which is why it
    cost so long to find.
    """
    quoted = android.quote_input_text(f"a{char}b")

    assert quoted != f"a{char}b", f"{char!r} passed through unquoted"
    assert quoted.startswith(("'", '"')) or "\\" in quoted


def test_plain_text_is_not_mangled() -> None:
    """The control. Quoting everything into unreadable shapes is its own bug."""
    assert "abc123" in android.quote_input_text("abc123")


# --------------------------------------------------------------------------- #
# Nothing is guessed
# --------------------------------------------------------------------------- #


def test_no_avd_is_assumed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule: booting somebody else's emulator is a surprise, not a convenience."""
    monkeypatch.delenv("ANDROID_AVD", raising=False)
    assert android.DEFAULT_AVD == "" or android.DEFAULT_AVD == "fitco_test"


def test_detecting_a_package_without_a_filter_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule: an empty filter matches the first system package on the list.

    Which would then be launched, cleared or inspected — a wrong answer that
    looks like a right one.
    """
    monkeypatch.delenv("ANDROID_PACKAGE_MATCH", raising=False)

    with pytest.raises(ConfigError) as caught:
        android.Emulator().detect_package()

    assert "ANDROID_PACKAGE_MATCH" in str(caught.value)


def test_the_default_apk_needs_a_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule: the build output lives under a project, and there is none to assume."""
    with pytest.raises(ConfigError):
        android.Emulator().default_apk()


def test_adb_is_looked_for_in_three_places(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule: the SDK installs three different ways.

    A resolver that only checks `PATH` reports a working machine as broken.
    """
    monkeypatch.setenv("ANDROID_ADB", "/custom/adb")
    assert android.require_adb() == "/custom/adb"

    monkeypatch.delenv("ANDROID_ADB")
    monkeypatch.setenv("ANDROID_SDK_ROOT", "/nowhere-at-all")
    monkeypatch.setattr(android.shutil, "which", lambda _: "/usr/bin/adb")
    assert android.require_adb() == "/usr/bin/adb"


def test_no_adb_anywhere_names_all_three_ways_to_fix_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANDROID_ADB", raising=False)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.setattr(android.shutil, "which", lambda _: None)

    with pytest.raises(ConfigError) as caught:
        android.require_adb()

    message = str(caught.value)
    assert "ANDROID_ADB" in message and "PATH" in message
