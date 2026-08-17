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
import importlib
import json
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console
from rich.table import Table

from opscore.errors import BridgeError


def click_command_path() -> str:
    """The command path Click is currently executing, without the program name.

    ``yourtool logs read`` resolves to ``"logs read"``. Read from the live Click
    context stack at emit time, because the root callback runs *before* the leaf
    is bound: a name stamped there is only the group, so every subcommand of a
    group produces an indistinguishable envelope.

    This is the **default** resolver rather than something each CLI passes,
    which is the whole point. The seam existed from the start and not one of the
    four CLIs used it — every envelope they emitted carried ``"command": ""``,
    including the error envelopes, where knowing which command failed is the
    entire value. A default cannot be forgotten.

    **Two places to look, and the obvious one is wrong here.** Typer stopped
    depending on `click` as a separate distribution and now vendors it as
    ``typer._click``, so `import click` raises `ImportError` inside a perfectly
    working Typer CLI. A resolver that knew only the top-level name returned
    "" and looked exactly like "there is no command running" — the failure this
    function exists to prevent, reintroduced one import line lower down.

    Neither import is a dependency: `opscore` is usable in a library with no CLI
    at all, and there it returns "" and carries on.
    """
    globals_module: Any = None
    for name in ("click.globals", "typer._click.globals"):
        try:
            globals_module = importlib.import_module(name)
            break
        except ImportError:
            continue
    if globals_module is None:  # pragma: no cover - no CLI framework installed
        return ""
    paths = []
    context = globals_module.get_current_context(silent=True)
    while context is not None:
        paths.append(" ".join(context.command_path.split()[1:]))
        context = context.parent
    return max(paths, key=len, default="")


@dataclass
class Output:
    """Renderer bound to the current command invocation."""

    json_mode: bool = False
    quiet: bool = False
    command: str = ""
    command_resolver: Callable[[], str] | None = field(default=click_command_path, repr=False)
    """Returns the full command path, known only once the subcommand is bound.

    Defaults to :func:`click_command_path`, so a CLI gets a correctly named
    envelope without doing anything. Pass ``None`` to opt out, or another
    callable to name commands some other way; whatever it returns wins over
    ``command`` when it is non-empty.
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
                # The error knows its own command; the context stack does not, by now.
                named = getattr(exc, "command_path", "") or self._command_path()
                self._emit({"ok": False, "command": named, **exc.as_dict()})
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
