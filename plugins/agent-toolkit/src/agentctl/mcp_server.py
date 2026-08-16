"""The MCP surface: the same two commands, described for an agent.

Written against the module functions rather than by shelling out to the CLI.
Spawning a subprocess per call would work and is what a thinner bridge does, but
it buys nothing here — there is no process isolation to gain when every command
is a read, and it costs an interpreter start on every tool call.

**Every tool is read-only, because every command is.** There is no
`--allow-writes` flag to grant and no write path to guard. That is a stronger
property than a guarded write and a cheaper one to verify: the reason nothing can
be damaged is that nothing here can write, not that a check is watching.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from agentctl import detect as detect_module
from agentctl import strays as strays_module

SERVER_NAME = "agentctl"

_ROOT_PROPERTY = {
    "type": "string",
    "description": "Repository path. Defaults to the directory the server was started in.",
}

TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "repo_detect",
        "description": (
            "Report which practices a repository has adopted — spec-driven development, a "
            "debt register, agent tooling, verified-state blocks, declared gates, content "
            "roots — each with the paths that prove it. Run this BEFORE auditing anything: "
            "a rule the project never adopted is not a finding, and reporting one teaches "
            "the reader to ignore the report."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "root": _ROOT_PROPERTY,
                "only": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Restrict to these practice names.",
                },
            },
        },
    },
    {
        "name": "repo_strays",
        "description": (
            "Find executables (.py, .sh, .mjs, .js and friends) sitting outside the "
            "repository's code directories, and say which are declared exceptions and "
            "which are nobody's decision yet. Returns measured=false when no conventional "
            "code directory exists, because then 'outside the code' has no meaning."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "root": _ROOT_PROPERTY,
                "include_declared": {
                    "type": "boolean",
                    "description": "Also return the declared exceptions.",
                },
            },
        },
    },
)


def _root(arguments: dict[str, Any], default: Path) -> Path:
    raw = arguments.get("root")
    return Path(raw).expanduser() if isinstance(raw, str) and raw else default


def _call(name: str, arguments: dict[str, Any], default_root: Path) -> tuple[str, bool]:
    """Run one tool. Returns `(payload, is_error)`.

    A missing directory is an error the caller can act on; anything else is a
    result, including "nothing found" and "could not be measured". Collapsing
    those two is how an unmeasured repository reads as a clean one.
    """
    root = _root(arguments, default_root)
    if not root.is_dir():
        return json.dumps({"ok": False, "error": "NotADirectory", "message": str(root)}), True

    if name == "repo_detect":
        only = arguments.get("only")
        result = detect_module.detect(root, only=only if isinstance(only, list) else None)
        return json.dumps({"ok": True, "data": result.as_dict()}, indent=2), False

    if name == "repo_strays":
        if not detect_module.code_roots(root):
            payload = {"code_roots": [], "strays": [], "undeclared": 0, "measured": False}
            return json.dumps({"ok": True, "data": payload}, indent=2), False
        found = strays_module.find(root)
        open_items = strays_module.undeclared(found)
        shown = found if arguments.get("include_declared") else open_items
        payload = {
            "code_roots": list(detect_module.code_roots(root)),
            "strays": [s.as_dict() for s in shown],
            "undeclared": len(open_items),
            "measured": True,
        }
        return json.dumps({"ok": True, "data": payload}, indent=2), False

    return json.dumps({"ok": False, "error": "UnknownTool", "message": name}), True


def serve(*, default_root: Path) -> None:
    """Run the server on stdio until the client disconnects."""
    try:
        import mcp.types as types
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
    except ImportError as exc:  # pragma: no cover - dependency is declared
        print(f"the MCP SDK is not installed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    server: Any = Server(SERVER_NAME)

    async def handle_list_tools(_ctx: Any, _params: Any) -> Any:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=tool["name"],
                    description=tool["description"],
                    input_schema=tool["schema"],
                )
                for tool in TOOLS
            ]
        )

    async def handle_call_tool(_ctx: Any, params: Any) -> Any:
        # The walk is blocking; keep the event loop free for the client.
        payload, is_error = await asyncio.to_thread(
            _call, params.name, params.arguments or {}, default_root
        )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=payload)],
            is_error=is_error,
        )

    server.add_request_handler("tools/list", types.PaginatedRequestParams, handle_list_tools)
    server.add_request_handler("tools/call", types.CallToolRequestParams, handle_call_tool)

    async def _run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(_run())
