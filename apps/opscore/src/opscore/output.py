"""The agent contract: one renderer, two audiences.

Every command writes through this module. With ``--json`` it emits a single
envelope on stdout and nothing else, so Claude/Codex (or the MCP bridge) can
parse the result without scraping formatted text. Without it, humans get Rich.

Envelope shape::

    {"ok": true,  "command": "lessons list", "data": {...}}
    {"ok": false, "command": "lessons list", "error": "ApiError", "message": "..."}

Diagnostics (``info``/``warn``/``step``) always go to **stderr**, so they never
corrupt the JSON on stdout.
"""

from __future__ import annotations

import contextvars
import json
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console
from rich.table import Table

from opscore.errors import BridgeError


@dataclass
class Output:
    """Renderer bound to the current command invocation."""

    json_mode: bool = False
    quiet: bool = False
    command: str = ""
    command_resolver: Callable[[], str] | None = field(default=None, repr=False)
    """Returns the full command path, known only once the subcommand is bound.

    The root callback runs *before* Click resolves the leaf, so ``command`` set
    there is only the group (``"logs"`` for ``yourtool logs read``) — two different
    commands then produce indistinguishable envelopes. The CLI injects a
    resolver that reads the live Click context at emit time instead.
    """
    _stdout: Console = field(default_factory=lambda: Console(), repr=False)
    _stderr: Console = field(default_factory=lambda: Console(stderr=True), repr=False)
    _emitted: bool = field(default=False, repr=False)
    """Whether an envelope already went to stdout. Exactly one is allowed."""

    @property
    def emitted(self) -> bool:
        return self._emitted

    def _command_path(self) -> str:
        """The command this envelope belongs to, as fully as it is known."""
        if self.command_resolver is not None:
            resolved = self.command_resolver()
            if resolved:
                return resolved
        return self.command

    # --- diagnostics (stderr, never part of the machine payload) -----------
    def info(self, message: str) -> None:
        if not self.quiet and not self.json_mode:
            self._stderr.print(message)

    def step(self, message: str) -> None:
        if not self.quiet and not self.json_mode:
            self._stderr.print(f"[dim]→[/dim] {message}")

    def warn(self, message: str) -> None:
        if not self.quiet:
            self._stderr.print(f"[yellow]warning[/yellow] {message}")

    def error(self, message: str) -> None:
        self._stderr.print(f"[red]error[/red] {message}")

    # --- results (stdout) ---------------------------------------------------
    def result(
        self,
        data: Any,
        *,
        human: str | None = None,
        ok: bool = True,
        message: str | None = None,
        error: str | None = None,
    ) -> None:
        """Emit the command's result.

        Args:
            data: JSON-serialisable payload, used in ``--json`` mode.
            human: optional pre-rendered text for humans. When omitted the
                payload itself is pretty-printed.
            ok: ``False`` for a command that produced findings *and* failed —
                a gate, for instance. Keeps the failure in the same envelope
                as the data instead of emitting a second one.
            message: why it failed, when ``ok`` is ``False``.
            error: the failure class, defaulting to ``CheckFailed``. Every
                ``ok: false`` envelope carries one, whether it came from a
                raised error or from a gate reporting its own findings, so a
                caller can branch on ``error`` without special-casing.
        """
        if self.json_mode:
            payload: dict[str, Any] = {"ok": ok, "command": self._command_path(), "data": data}
            if not ok:
                payload["error"] = error or "CheckFailed"
            if message is not None:
                payload["message"] = message
            self._emit(payload)
            return
        if human is not None:
            self._stdout.print(human)
        elif isinstance(data, str):
            self._stdout.print(data)
        else:
            self._stdout.print_json(json.dumps(data, default=str))

    def table(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        columns: Sequence[str] | None = None,
        title: str | None = None,
        ok: bool = True,
        message: str | None = None,
        error: str | None = None,
    ) -> None:
        """Render ``rows`` as a table for humans, or as a JSON list for agents."""
        if self.json_mode:
            self.result(list(rows), ok=ok, message=message, error=error)
            return
        if not rows:
            self._stdout.print("[dim](no rows)[/dim]")
            return
        cols = list(columns) if columns else list(rows[0].keys())
        table = Table(title=title, header_style="bold")
        for col in cols:
            table.add_column(col)
        for row in rows:
            table.add_row(*("" if row.get(c) is None else str(row.get(c)) for c in cols))
        self._stdout.print(table)

    def failure(self, exc: BridgeError) -> None:
        """Report a failure.

        In JSON mode the envelope only goes out if the command has not already
        emitted one — a gate that printed its findings with ``ok=False`` has
        already said everything, and a second envelope would make stdout
        unparseable. The exit code carries the failure either way.
        """
        if self.json_mode:
            if not self._emitted:
                self._emit({"ok": False, "command": self._command_path(), **exc.as_dict()})
            else:
                self.error(exc.message)
            return
        self.error(exc.message)
        if exc.detail:
            self._stderr.print(f"[dim]{exc.detail}[/dim]")

    def _emit(self, payload: Mapping[str, Any]) -> None:
        sys.stdout.write(json.dumps(payload, default=str, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        self._emitted = True


_current: contextvars.ContextVar[Output | None] = contextvars.ContextVar(
    "opscore_output", default=None
)


def set_output(output: Output) -> None:
    """Bind the renderer for the current invocation (called by the CLI root)."""
    _current.set(output)


def get_output() -> Output:
    """Return the renderer bound to the current invocation.

    Falls back to a plain human renderer so library code can call this even
    when it was imported outside a CLI invocation (tests, notebooks).
    """
    output = _current.get()
    if output is None:
        output = Output()
        _current.set(output)
    return output


def joined(items: Iterable[Any], sep: str = ", ") -> str:
    """Small helper for human strings built from iterables."""
    return sep.join(str(i) for i in items)
