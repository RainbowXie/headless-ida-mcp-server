# -*- coding: utf-8 -*-
"""Stdio MCP stub for headless-ida daemon.

Always responsive (CC spawns via stdio).  Starts with a single ``start_daemon``
tool.  The agent calls it to explicitly bring up the daemon; once connected,
``send_tool_list_changed()`` notifies CC to re-fetch, and the full real tool
surface is transparently proxied thereafter.

Two operating modes:

- Default / manual mode (``--port``/``--host``, or no flags): the daemon is
  spawned with ``start_new_session=True`` — it outlives the stub process and
  later stub sessions find a warm daemon already running.
- Session mode (``--session-mode``): an ephemeral port is allocated at stub
  startup, the daemon child is tracked by this stub, and the stub terminates
  the daemon on exit so concurrent agent sessions never share one IDB.
"""

import argparse
import asyncio
import atexit
import os
import signal
import socket
import subprocess
import sys
from typing import Any, Sequence

from mcp import ClientSession
# 使用 MCP SDK 标准导出的 streamable_http_client（带下划线）作为客户端传输层
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP
from mcp.server.stdio import stdio_server
from mcp.server.lowlevel.server import NotificationOptions
from mcp.types import TextContent, Tool as MCPTool

# No-flag behavior must stay byte-for-byte compatible with the historical
# fixed-port setup. Session configuration is deliberately CLI-only: MCP
# clients spawn this stub with a fixed argv, so per-session isolation has to
# be expressible purely as flags — env vars are not part of the contract.
DEFAULT_PORT = 8392
DEFAULT_HOST = "127.0.0.1"

# Grace period between SIGTERM and SIGKILL escalation when reaping the
# session-mode daemon child on stub exit.
_DAEMON_TERMINATE_TIMEOUT = 5


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="headless-ida-mcp-stub",
        description=(
            "Stdio MCP stub that proxies tool calls to a headless-ida "
            "streamable-http daemon."
        ),
    )
    # --port and --session-mode are mutually exclusive: session mode owns the
    # port via ephemeral allocation, so an explicit --port would be ambiguous.
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--session-mode",
        action="store_true",
        help=(
            "Isolate this agent session: allocate an ephemeral port, spawn a "
            "dedicated daemon for this stub only, and terminate the daemon "
            "when the stub exits. Cannot be combined with --port."
        ),
    )
    mode.add_argument(
        "--port",
        "-p",
        type=int,
        default=None,
        help=f"Explicit daemon port (default: {DEFAULT_PORT}).",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"Daemon bind/connect host (default: {DEFAULT_HOST}).",
    )
    return parser


def _find_free_port(host: str = DEFAULT_HOST) -> int:
    """Discover an available ephemeral TCP port on the target host."""
    # Bind the actual target host rather than "": the daemon re-binds this
    # exact (host, port) moments later, and a port that is free on
    # 127.0.0.1 can already be taken on another interface.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _resolve_endpoint(args: argparse.Namespace) -> tuple[str, int]:
    """Map parsed CLI args to the concrete (host, port) daemon endpoint."""
    # The ephemeral port is fixed at startup (not at first tool call) so the
    # daemon target URL is stable before the MCP client initializes.
    if args.session_mode:
        return args.host, _find_free_port(args.host)
    return args.host, args.port if args.port is not None else DEFAULT_PORT


class HeadlessIdaStub(FastMCP):
    """Lazy-proxy stub: exposes ``start_daemon`` until the real tool surface
    of the upstream headless-ida daemon is reachable."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        session_mode: bool = False,
    ) -> None:
        super().__init__("headless-ida")
        self._host = host
        self._port = port
        self._daemon_url = f"http://{host}:{port}/mcp"
        # session_mode ties the spawned daemon to this stub's lifetime; in
        # default mode the daemon detaches and must outlive the stub.
        self._session_mode = session_mode
        self._real_tools: list[MCPTool] | None = None
        self._daemon_proc: subprocess.Popen | None = None
        self._daemon_ready = False
        self._startup_lock = asyncio.Lock()
        # atexit covers interpreter exits that bypass run_stdio_async's
        # finally (fatal errors inside FastMCP); signal-driven exits are
        # covered separately by _install_signal_handlers.
        atexit.register(self._terminate_tracked_daemon)

    async def run_stdio_async(self) -> None:
        try:
            async with stdio_server() as (read_stream, write_stream):
                await self._mcp_server.run(
                    read_stream,
                    write_stream,
                    self._mcp_server.create_initialization_options(
                        notification_options=NotificationOptions(tools_changed=True),
                    ),
                )
        finally:
            # Reached both on stdin EOF (downstream MCP client disconnect)
            # and on error unwind: a session-mode daemon must never outlive
            # its stub, otherwise it would keep holding the IDB and port.
            self._terminate_tracked_daemon()

    def _spawn_daemon(self) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "headless_ida_mcp_server"],
            env={
                **os.environ,
                "IDA_INSTALL_DIR": os.environ.get("IDA_INSTALL_DIR", "/opt/ida-pro-9.3"),
                "TRANSPORT": "streamable-http",
                "PORT": str(self._port),
                "HOST": self._host,
            },
            # start_new_session=True detaches the daemon so it stays warm
            # across stub sessions (historical behavior); session mode keeps
            # the child in this process group and reaps it via
            # _terminate_tracked_daemon on stub exit.
            start_new_session=not self._session_mode,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if self._session_mode:
            self._daemon_proc = proc
        return proc

    def _terminate_tracked_daemon(self) -> None:
        # Only session-mode children are tracked here; detached daemons must
        # survive the stub by design and are never killed from this path.
        proc = self._daemon_proc
        if proc is None:
            return
        self._daemon_proc = None
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=_DAEMON_TERMINATE_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=_DAEMON_TERMINATE_TIMEOUT)

    def _install_signal_handlers(self) -> None:
        # SIGTERM's default action kills the process without unwinding
        # try/finally or running atexit, which would orphan a session-mode
        # daemon. Clean up synchronously, then restore the default
        # disposition and re-raise so the exit status still reports the
        # original signal (128+SIG convention for signal deaths).
        def _handler(signum: int, _frame: Any) -> None:
            self._terminate_tracked_daemon()
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)

    async def _probe_daemon(self) -> bool:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port), timeout=1.5
            )
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            return False

    async def _fetch_real_tools(self) -> list[MCPTool]:
        async with streamable_http_client(self._daemon_url) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()
                return list(result.tools)

    async def _activate_daemon(self) -> str:
        """Start or probe the daemon; fetch real tools; notify CC to re-list."""
        if self._daemon_ready or await self._probe_daemon():
            self._daemon_ready = True
            if self._real_tools is None:
                self._real_tools = await self._fetch_real_tools()
            await self._notify_tools_changed()
            return "running"

        async with self._startup_lock:
            if self._daemon_ready or await self._probe_daemon():
                self._daemon_ready = True
                if self._real_tools is None:
                    self._real_tools = await self._fetch_real_tools()
                await self._notify_tools_changed()
                return "running"

            self._spawn_daemon()
            for _ in range(120):
                await asyncio.sleep(1)
                if await self._probe_daemon():
                    self._daemon_ready = True
                    self._real_tools = await self._fetch_real_tools()
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
                        f"Start the headless-ida daemon with streamable-http "
                        f"on port {self._port}. Call this once before using "
                        f"any other headless IDA tools. Returns 'started' if "
                        f"newly spawned, 'running' if already up."
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

        async with streamable_http_client(self._daemon_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
                return result.content


def main() -> None:
    args = _build_parser().parse_args()
    host, port = _resolve_endpoint(args)
    stub = HeadlessIdaStub(host=host, port=port, session_mode=args.session_mode)
    stub._install_signal_handlers()
    stub.run(transport="stdio")


if __name__ == "__main__":
    main()
