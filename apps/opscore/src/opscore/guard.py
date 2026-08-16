"""Write guards.

Anything that leaves this process — a message another human will read, a
keystroke sent into somebody's live session — goes through one gate, so the
decision is made in one place and phrased consistently.

Rules:

* a mutating command with **no** flags refuses. It does not silently rehearse
  — ``dry_run`` defaults to ``False``, and an unconfirmed write raises
  :class:`GuardError`. "Defaults to a dry run" would be a friendlier promise
  and a false one: a caller who believed it would read a refusal as a
  successful rehearsal;
* ``--dry-run`` asks for that rehearsal explicitly, and returns ``False`` so
  the command reports what it *would* do;
* an actual write needs ``--confirm-prod-write`` (or the caller passing
  ``confirmed=True``), whatever the target environment;
* the refusal states what the write actually touches — see
  :class:`Consequence`. A wrong-but-scary warning trains people to skim.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from opscore.errors import GuardError
from opscore.output import get_output


class Consequence(StrEnum):
    """What a write actually touches — which decides what the refusal says.

    A wrong-but-scary warning trains people to skim. The guard is right in
    every case; the sentence has to match the case.
    """

    REPOSITORY = "repository"
    """Files in this working tree — specs, docs, the manifest."""

    EXTERNAL = "external"
    """A third party sees it: a Slack message, an API call somebody receives."""

    SESSION = "session"
    """Keystrokes are sent into a live agent session that somebody else may be
    watching. Not destructive, but not invisible either — the operator sees text
    appear that they did not type."""

    LOCAL_PROCESS = "local_process"
    """Processes on this machine are started or killed. Nothing leaves it."""

    WORKSTATION = "workstation"
    """Configuration on this machine *outside* the working tree.

    Added for skill installation, which writes symlinks into `~/.claude/skills`
    and `~/.codex/skills`. `REPOSITORY` would have been the convenient label and
    the wrong one: reverting the commit does not undo it, and `git status` never
    shows it. A warning that misnames what it touches teaches people to skim.
    """


CONSEQUENCE_NOTICE: dict[Consequence, str] = {
    Consequence.REPOSITORY: "this rewrites files in the working tree",
    Consequence.EXTERNAL: "this is visible to people outside this machine and cannot be un-sent",
    Consequence.SESSION: (
        "this types into a live agent session somebody may be watching — they will see "
        "text appear that they did not write"
    ),
    Consequence.LOCAL_PROCESS: "this starts or kills processes on this machine",
    Consequence.WORKSTATION: (
        "this changes configuration on this machine outside the working tree — "
        "git will not show it and reverting the commit will not undo it"
    ),
}


@dataclass(frozen=True)
class WriteIntent:
    """A described mutation, evaluated before it is performed."""

    action: str
    """Human description, e.g. "delete 12 lesson records at establishment 2213"."""

    target: str
    """What is being mutated, e.g. "lessons/1845877"."""

    consequence: Consequence = Consequence.EXTERNAL
    """What this write touches. Decides the wording of the refusal."""

    def describe(self) -> str:
        return f"{self.action} → {self.target}"

    @property
    def notice(self) -> str:
        return CONSEQUENCE_NOTICE[self.consequence]


def check_write(
    intent: WriteIntent,
    *,
    dry_run: bool,
    confirmed: bool,
) -> bool:
    """Decide whether a mutation may proceed.

    Args:
        intent: what the command is about to do.
        dry_run: the caller asked for a rehearsal.
        confirmed: ``--confirm-prod-write`` was passed.

    Returns:
        ``True`` when the caller should perform the write, ``False`` when the
        run was a rehearsal.

    Raises:
        GuardError: a real write was requested without confirmation.
    """
    out = get_output()

    if dry_run:
        out.warn(f"dry-run: would {intent.describe()}")
        return False

    if not confirmed:
        raise GuardError(
            f"refusing to {intent.describe()} without --confirm-prod-write",
            detail=intent.notice,
        )

    out.warn(f"write: {intent.describe()} — {intent.notice}")
    return True
