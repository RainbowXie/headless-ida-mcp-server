# -*- coding: utf-8 -*-
"""Demo manifest used by the plugin contract test suite."""
from __future__ import annotations

from typing import Annotated, Optional


# Module-level handlers. Tests rely on identity (e.g. asserting that the
# handler attribute on the registered tool is the same object), so these
# stay top-level rather than lambdas.


def ping(message: Annotated[str, "Echo back this message"] = "pong") -> dict:
    return {"echo": message}


def slow(seconds: int = 5) -> dict:
    import time

    time.sleep(seconds)
    return {"slept": seconds}


def add(
    a: int,
    b: int,
    note: Annotated[Optional[str], "Optional note"] = None,
) -> dict:
    return {"sum": a + b, "note": note}


def rename_function(addr: int, name: str) -> dict:
    return {"addr": addr, "name": name}


def patch_bytes(addr: int, value: bytes) -> dict:
    return {"addr": addr, "size": len(value)}


def _internal_helper() -> dict:
    return {"internal": True}


def raises_tool_error() -> dict:
    from headless_ida_mcp_server.plugins import ToolError

    raise ToolError(-12, "bad input")


def raises_value_error() -> dict:
    raise ValueError("nope")


PLUGIN = {
    "name": "demo",
    "description": "Demo plugin used in tests",
    "version": "0.1",
    "categories": ["test"],
}

TOOLS = [
    {
        "name": "ping",
        "handler": ping,
        "description": "Ping/pong echo",
        "tags": ["kind:read"],
    },
    {
        "name": "slow",
        "handler": slow,
        "description": "Sleep N seconds (timeout test)",
        "tags": ["kind:read"],
        "timeout": 1,
    },
    {
        "name": "add",
        "handler": add,
        "description": "Add two integers",
        "tags": ["kind:read"],
    },
    {
        "name": "rename_function",
        "handler": rename_function,
        "description": "Rename a function (kind:write -> auto undo)",
        "tags": ["kind:write"],
    },
    {
        "name": "patch_bytes",
        "handler": patch_bytes,
        "description": "Patch raw bytes (kind:unsafe -> no auto undo)",
        "tags": ["kind:unsafe"],
    },
    {
        "name": "internal",
        "handler": _internal_helper,
        "description": "mcp:false hidden tool",
        "tags": ["kind:read"],
        "mcp": False,
    },
    {
        "name": "raise_tool_error",
        "handler": raises_tool_error,
        "description": "Raises ToolError(-12, 'bad input')",
        "tags": ["kind:read"],
    },
    {
        "name": "raise_value_error",
        "handler": raises_value_error,
        "description": "Raises ValueError('nope')",
        "tags": ["kind:read"],
    },
]
