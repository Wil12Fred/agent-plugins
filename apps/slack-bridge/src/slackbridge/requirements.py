"""What has to be installed for this to work, checked before it starts.

The bridge shells out to other programs, and until this module existed it found
that out **one message at a time**: `serve` started, connected to Slack,
reported itself healthy, and then failed on the first dispatch because `tmux`
was not installed. From the outside that is indistinguishable from the agent
ignoring you.

So the check moves to startup, and it makes one distinction that matters:

**Required** — the bridge cannot do its job. It refuses to start and says which
program is missing and how to install it. Starting anyway would be pretending.

**Degraded** — something works less well. It starts, and says out loud what it
will not be able to do. Refusing here would be worse than the problem: nobody
needs KDE to drive a session from their phone.

Every message names the fix, not just the fault. "tmux is not installed" is half
an error; the other half is the command that installs it.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class Level(StrEnum):
    REQUIRED = "required"
    """Without it the bridge cannot do its job, so it refuses to start."""

    DEGRADED = "degraded"
    """Without it something works less well. It starts and says so."""


@dataclass(frozen=True)
class Requirement:
    """One external program the bridge shells out to.

    Attributes:
        name: what to call it in a message.
        candidates: binaries that satisfy it — **any one** is enough. Some
            programs ship under more than one name (`qdbus6` on newer Plasma,
            `qdbus` before it), and a check that knows only one reports a
            perfectly working machine as broken.
        purpose: what stops working without it, in the operator's terms.
        install: how to get it. Distribution-specific, so it names more than one.
        level: refuse, or start and warn.
    """

    name: str
    candidates: tuple[str, ...]
    purpose: str
    install: str
    level: Level

    def found(self, which: object = None) -> str | None:
        """The first candidate present on `PATH`, or None."""
        lookup = which if callable(which) else shutil.which
        for candidate in self.candidates:
            if lookup(candidate):
                return candidate
        return None


REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement(
        name="an agent CLI",
        candidates=("claude", "codex"),
        purpose="there is nothing to drive — the bridge relays messages to a coding agent",
        install=(
            "install Claude Code (https://claude.com/claude-code) or Codex, "
            "or point CLAUDE_BIN / CODEX_BIN at an existing one"
        ),
        level=Level.REQUIRED,
    ),
    Requirement(
        name="tmux",
        candidates=("tmux",),
        purpose="typing a reply into a session that is already running",
        install="apt install tmux · pacman -S tmux · brew install tmux",
        level=Level.REQUIRED,
    ),
    Requirement(
        name="qdbus",
        candidates=("qdbus6", "qdbus"),
        purpose=(
            "finding sessions running in a KDE terminal window. Without it only "
            "sessions inside tmux are discoverable"
        ),
        install="part of Plasma: apt install qdbus-qt6 · pacman -S qt6-tools",
        level=Level.DEGRADED,
    ),
)


@dataclass(frozen=True)
class Result:
    requirement: Requirement
    binary: str | None

    @property
    def satisfied(self) -> bool:
        return self.binary is not None

    @property
    def blocking(self) -> bool:
        return not self.satisfied and self.requirement.level is Level.REQUIRED

    def as_dict(self) -> dict[str, object]:
        return {
            "requirement": self.requirement.name,
            "level": str(self.requirement.level),
            "satisfied": self.satisfied,
            "binary": self.binary,
            "purpose": self.requirement.purpose,
            "install": None if self.satisfied else self.requirement.install,
        }


def check(
    requirements: Iterable[Requirement] = REQUIREMENTS, *, which: object = None
) -> list[Result]:
    """Look for each requirement. `which` is injectable so this is testable offline."""
    return [Result(requirement=r, binary=r.found(which)) for r in requirements]


def missing(results: Iterable[Result]) -> list[Result]:
    return [r for r in results if not r.satisfied]


def blocking(results: Iterable[Result]) -> list[Result]:
    return [r for r in results if r.blocking]


def explain(results: Iterable[Result]) -> str:
    """The refusal, naming every missing requirement and how to install each.

    Every one, not the first: an operator who fixes `tmux`, restarts, and is then
    told about `qdbus` has been made to do the work twice for no reason.
    """
    lines = []
    for result in missing(results):
        # No square brackets: this text is rendered through Rich, which reads
        # `[required]` as a style tag and silently swallows it — the marker
        # vanished from the very message that exists to be read.
        marker = "REQUIRED" if result.requirement.level is Level.REQUIRED else "optional"
        lines.append(
            f"  {marker}: {result.requirement.name} — {result.requirement.purpose}\n"
            f"      install: {result.requirement.install}"
        )
    return "\n".join(lines)
