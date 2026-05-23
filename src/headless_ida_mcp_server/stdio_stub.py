# -*- coding: utf-8 -*-
"""Stdio MCP stub for headless-ida daemon.

Always responsive (CC spawns via stdio).  Starts with a single ``start_daemon``
tool.  The agent calls it to explicitly bring up the daemon; once connected,
``send_tool_list_changed()`` notifies CC to re-fetch, and the full real tool
surface is transparently proxied thereafter.

The daemon uses ``start_new_session=True`` — it outlives the CC process.
Subsequent sessions find a warm daemon already running.
"""

import asyncio
import os
import subprocess
import sys
from typing import Any, Sequence

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.fastmcp import FastMCP
from mcp.server.stdio import stdio_server
from mcp.server.lowlevel.server import NotificationOptions
from mcp.types import TextContent, Tool as MCPTool

DAEMON_PORT = int(os.environ.get("HEADLESS_IDA_PORT", "8392"))
DAEMON_HOST = os.environ.get("HEADLESS_IDA_HOST", "127.0.0.1")
DAEMON_URL = f"http://{DAEMON_HOST}:{DAEMON_PORT}/mcp"

_STARTUP_LOCK = asyncio.Lock()
_DAEMON_READY = False


def _start_daemon_impl() -> None:
    subprocess.Popen(
        [sys.executable, "-u", "-m", "headless_ida_mcp_server"],
        env={
            **os.environ,
            "IDA_INSTALL_DIR": os.environ.get("IDA_INSTALL_DIR", "/opt/ida-pro-9.3"),
            "TRANSPORT": "streamable-http",
            "PORT": str(DAEMON_PORT),
            "HOST": DAEMON_HOST,
        },
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def _probe_daemon() -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(DAEMON_HOST, DAEMON_PORT), timeout=1.5
        )
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


async def _fetch_real_tools() -> list[MCPTool]:
    async with streamablehttp_client(DAEMON_URL) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.list_tools()
            return list(result.tools)


class HeadlessIdaStub(FastMCP):
    def __init__(self) -> None:
        super().__init__("headless-ida")
        self._real_tools: list[MCPTool] | None = None

    async def run_stdio_async(self) -> None:
        async with stdio_server() as (read_stream, write_stream):
            await self._mcp_server.run(
                read_stream,
                write_stream,
                self._mcp_server.create_initialization_options(
                    notification_options=NotificationOptions(tools_changed=True),
                ),
            )

    async def _activate_daemon(self) -> str:
        """Start or probe the daemon; fetch real tools; notify CC to re-list."""
        global _DAEMON_READY
        if _DAEMON_READY or await _probe_daemon():
            _DAEMON_READY = True
            if self._real_tools is None:
                self._real_tools = await _fetch_real_tools()
            await self._notify_tools_changed()
            return "running"

        async with _STARTUP_LOCK:
            if _DAEMON_READY or await _probe_daemon():
                _DAEMON_READY = True
                if self._real_tools is None:
                    self._real_tools = await _fetch_real_tools()
                await self._notify_tools_changed()
                return "running"

            _start_daemon_impl()
            for _ in range(120):
                await asyncio.sleep(1)
                if await _probe_daemon():
                    _DAEMON_READY = True
                    self._real_tools = await _fetch_real_tools()
                    await self._notify_tools_changed()
                    return "started"
            return "error: daemon failed to start within 120 s"

    async def _notify_tools_changed(self) -> None:
        try:
            session = self._mcp_server.request_context.session
        except (LookupError, AttributeError):
            return
        if session is not None:
            try:
                await session.send_tool_list_changed()
            except Exception:
                pass

    async def list_tools(self) -> list[MCPTool]:
        if self._real_tools is None:
            return [
                MCPTool(
                    name="start_daemon",
                    description=(
                        "Start the headless-ida daemon with streamable-http on "
                        "port 8392. Call this once before using any other "
                        "headless IDA tools. Returns 'started' if newly "
                        "spawned, 'running' if already up."
                    ),
                    inputSchema={"type": "object", "properties": {}},
                )
            ]
        return list(self._real_tools)

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> Sequence[Any] | dict[str, Any]:
        if name == "start_daemon":
            status = await self._activate_daemon()
            return [TextContent(type="text", text=status)]

        async with streamablehttp_client(DAEMON_URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
                return result.content


def main() -> None:
    stub = HeadlessIdaStub()
    stub.run(transport="stdio")


if __name__ == "__main__":
    main()
