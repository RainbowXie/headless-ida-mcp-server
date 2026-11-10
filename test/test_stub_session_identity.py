# -*- coding: utf-8 -*-
"""Session identity tests for the stdio stub.

Covers the contract in `specs/stub-session-identity`: a session-mode stub only
adopts a daemon carrying its own token, a foreign port triggers a bounded
reallocation instead of a silent adoption, and a spawn that dies is noticed
immediately rather than after the full startup budget.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.types import Tool as MCPTool

from headless_ida_mcp_server import stdio_stub
from headless_ida_mcp_server.stdio_stub import HeadlessIdaStub

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_BINARY = REPO_ROOT / "test" / "heap" / "main"


def _tool(name: str) -> MCPTool:
    return MCPTool(name=name, inputSchema={"type": "object", "properties": {}})


def _health(payload: dict):
    return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(payload))])


class _FakeProc:
    """Stand-in for `subprocess.Popen` that only needs poll/terminate/wait."""

    def __init__(self, exit_code: int | None = None) -> None:
        self._exit = exit_code

    def poll(self):
        return self._exit

    def terminate(self) -> None:
        self._exit = 0

    def kill(self) -> None:
        self._exit = 0

    def wait(self, timeout=None) -> int:
        return 0


class _FakeTransport:
    async def __aenter__(self):
        return None, None, None

    async def __aexit__(self, *exc_info):
        return False


class _FakeSession:
    def __init__(self, *, token: str | None, fail: bool = False) -> None:
        self._token = token
        self._fail = fail

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def initialize(self) -> None:
        if self._fail:
            raise RuntimeError("not an MCP endpoint")

    async def list_tools(self):
        return SimpleNamespace(tools=[_tool("server_health")])

    async def call_tool(self, _name, _arguments):
        return _health({"session_token": self._token})


def _install_transport(monkeypatch, *, token: str | None, fail: bool = False) -> None:
    monkeypatch.setattr(
        stdio_stub, "streamable_http_client", lambda _url: _FakeTransport()
    )
    monkeypatch.setattr(
        stdio_stub, "ClientSession", lambda _r, _w: _FakeSession(token=token, fail=fail)
    )


# ---------------------------------------------------------------------------
# Token propagation
# ---------------------------------------------------------------------------


def test_session_mode_passes_its_token_to_the_daemon(monkeypatch):
    stub = HeadlessIdaStub(port=40010, session_mode=True)
    captured: dict = {}

    def _popen(*_args, **kwargs):
        captured.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(stdio_stub.subprocess, "Popen", _popen)
    stub._spawn_daemon()

    assert stub._session_token is not None
    assert len(stub._session_token) == 32
    assert captured["env"]["IDA_MCP_SESSION_TOKEN"] == stub._session_token


def test_default_mode_sends_no_token(monkeypatch):
    stub = HeadlessIdaStub()
    captured: dict = {}

    def _popen(*_args, **kwargs):
        captured.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(stdio_stub.subprocess, "Popen", _popen)
    stub._spawn_daemon()

    assert stub._session_token is None
    assert "IDA_MCP_SESSION_TOKEN" not in captured["env"]


def test_session_token_is_parsed_from_the_health_payload():
    assert stdio_stub._session_token_of(_health({"session_token": "abc"})) == "abc"
    assert stdio_stub._session_token_of(_health({"session_token": None})) is None
    assert (
        stdio_stub._session_token_of(
            SimpleNamespace(content=[SimpleNamespace(text="not json")])
        )
        is None
    )


# ---------------------------------------------------------------------------
# Handshake classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handshake_reports_absent_when_nothing_listens(monkeypatch):
    stub = HeadlessIdaStub(port=40011, session_mode=True)

    async def _probe() -> bool:
        return False

    monkeypatch.setattr(stub, "_probe_daemon", _probe)

    assert await stub._handshake() == ("absent", None)


@pytest.mark.asyncio
async def test_handshake_reports_foreign_when_mcp_fails(monkeypatch):
    stub = HeadlessIdaStub(port=40012, session_mode=True)

    async def _probe() -> bool:
        return True

    monkeypatch.setattr(stub, "_probe_daemon", _probe)
    _install_transport(monkeypatch, token=None, fail=True)

    assert await stub._handshake() == ("foreign", None)


@pytest.mark.asyncio
async def test_handshake_reports_foreign_on_token_mismatch(monkeypatch):
    stub = HeadlessIdaStub(port=40013, session_mode=True)

    async def _probe() -> bool:
        return True

    monkeypatch.setattr(stub, "_probe_daemon", _probe)
    _install_transport(monkeypatch, token="another-session")

    assert await stub._handshake() == ("foreign", None)


@pytest.mark.asyncio
async def test_handshake_accepts_matching_token(monkeypatch):
    stub = HeadlessIdaStub(port=40014, session_mode=True)

    async def _probe() -> bool:
        return True

    monkeypatch.setattr(stub, "_probe_daemon", _probe)
    _install_transport(monkeypatch, token=stub._session_token)

    identity, tools = await stub._handshake()

    assert identity == "ours"
    assert [t.name for t in tools] == ["server_health"]


@pytest.mark.asyncio
async def test_handshake_accepts_any_daemon_in_default_mode(monkeypatch):
    # Default mode expects no token, so a daemon that reports one is still ours.
    stub = HeadlessIdaStub(port=40015)

    async def _probe() -> bool:
        return True

    monkeypatch.setattr(stub, "_probe_daemon", _probe)
    _install_transport(monkeypatch, token="whatever")

    identity, _tools = await stub._handshake()

    assert identity == "ours"


@pytest.mark.asyncio
async def test_foreign_daemon_is_never_adopted(monkeypatch):
    stub = HeadlessIdaStub(port=40016, session_mode=True)

    async def _handshake():
        return "foreign", [_tool("server_health")]

    monkeypatch.setattr(stub, "_handshake", _handshake)

    assert [t.name for t in await stub.list_tools()] == ["start_daemon"]
    assert stub._real_tools is None


@pytest.mark.asyncio
async def test_session_mode_refuses_to_forward_before_adoption(monkeypatch):
    # The advertised surface already hides a foreign daemon's tools, but a
    # client can still name one directly; the forwarding gate must hold.
    stub = HeadlessIdaStub(port=40017, session_mode=True)

    def _boom(_url):
        raise AssertionError("must not open a transport before adoption")

    monkeypatch.setattr(stdio_stub, "streamable_http_client", _boom)

    result = await stub.call_tool("server_health", {})

    assert result[0].text.startswith("error: no daemon adopted for this session")


@pytest.mark.asyncio
async def test_default_mode_forwards_without_adoption(monkeypatch):
    # Default mode deliberately attaches to whatever answers the endpoint, so
    # the session-mode gate must not apply there.
    stub = HeadlessIdaStub(port=40018)

    class _ForwardSession(_FakeSession):
        async def call_tool(self, _name, _arguments):
            from mcp.types import TextContent

            return SimpleNamespace(content=[TextContent(type="text", text="forwarded")])

    monkeypatch.setattr(
        stdio_stub, "streamable_http_client", lambda _url: _FakeTransport()
    )
    monkeypatch.setattr(stdio_stub, "ClientSession", lambda _r, _w: _ForwardSession(token=None))

    result = await stub.call_tool("server_health", {})

    assert result[0].text == "forwarded"


# ---------------------------------------------------------------------------
# Collision recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_foreign_port_is_reallocated_before_spawning(monkeypatch):
    stub = HeadlessIdaStub(port=40020, session_mode=True)
    handshakes = iter([("foreign", None), ("absent", None)])
    spawns: list[int] = []

    async def _handshake():
        return next(handshakes)

    def _spawn() -> None:
        spawns.append(stub._port)

    async def _await_start() -> str:
        stub._real_tools = [_tool("server_health")]
        return "ready"

    async def _notify() -> None:
        return None

    monkeypatch.setattr(stub, "_handshake", _handshake)
    monkeypatch.setattr(stub, "_spawn_daemon", _spawn)
    monkeypatch.setattr(stub, "_await_daemon_start", _await_start)
    monkeypatch.setattr(stub, "_notify_tools_changed", _notify)
    monkeypatch.setattr(stdio_stub, "_find_free_port", lambda _host=None: 40021)

    assert await stub._activate_daemon() == "started"
    assert stub._port == 40021
    assert stub._daemon_url == "http://127.0.0.1:40021/mcp"
    assert spawns == [40021]


@pytest.mark.asyncio
async def test_repeated_collisions_end_in_a_named_error(monkeypatch):
    stub = HeadlessIdaStub(port=40022, session_mode=True)
    ports = iter([40023, 40024, 40025])

    async def _handshake():
        return "foreign", None

    async def _await_start() -> str:
        return "foreign"

    monkeypatch.setattr(stub, "_handshake", _handshake)
    monkeypatch.setattr(stub, "_spawn_daemon", lambda: None)
    monkeypatch.setattr(stub, "_await_daemon_start", _await_start)
    monkeypatch.setattr(stdio_stub, "_find_free_port", lambda _host=None: next(ports))

    result = await stub._activate_daemon()

    assert result.startswith("error: session port collision after 3 attempts")
    assert "40025" in result
    assert stub._port == 40025


@pytest.mark.asyncio
async def test_exited_child_ends_the_wait_immediately(monkeypatch):
    stub = HeadlessIdaStub(port=40026, session_mode=True)

    async def _handshake():
        return "absent", None

    def _spawn() -> None:
        stub._daemon_proc = _FakeProc(exit_code=7)

    monkeypatch.setattr(stub, "_handshake", _handshake)
    monkeypatch.setattr(stub, "_spawn_daemon", _spawn)
    monkeypatch.setattr(stdio_stub, "_find_free_port", lambda _host=None: 40027)

    t0 = time.monotonic()
    result = await stub._activate_daemon()
    elapsed = time.monotonic() - t0

    assert "exited during startup (code 7)" in result
    # Three attempts, one poll interval each: nowhere near the 120 s budget.
    assert elapsed < 10


@pytest.mark.asyncio
async def test_repeated_child_exits_do_not_report_a_collision(monkeypatch):
    stub = HeadlessIdaStub(port=40030, session_mode=True)

    async def _handshake():
        return "absent", None

    def _spawn() -> None:
        stub._daemon_proc = _FakeProc(exit_code=9)

    async def _await_start() -> str:
        return "exited"

    monkeypatch.setattr(stub, "_handshake", _handshake)
    monkeypatch.setattr(stub, "_spawn_daemon", _spawn)
    monkeypatch.setattr(stub, "_await_daemon_start", _await_start)

    result = await stub._activate_daemon()

    assert result.startswith("error: daemon failed to start after 3 attempts")
    assert "port collision" not in result
    assert "code 9" in result


@pytest.mark.asyncio
async def test_reactivation_waits_on_a_live_child(monkeypatch):
    # Design D4: a failed handshake while our own child is alive means "not
    # serving yet". Re-activating must not kill it and jump to another port.
    stub = HeadlessIdaStub(port=40031, session_mode=True)
    stub._daemon_proc = _FakeProc(exit_code=None)
    reallocations: list[bool] = []

    async def _handshake():
        return "foreign", None

    async def _await_start() -> str:
        stub._real_tools = [_tool("server_health")]
        return "ready"

    async def _notify() -> None:
        return None

    monkeypatch.setattr(stub, "_handshake", _handshake)
    monkeypatch.setattr(stub, "_await_daemon_start", _await_start)
    monkeypatch.setattr(stub, "_notify_tools_changed", _notify)
    monkeypatch.setattr(stub, "_reallocate_port", lambda: reallocations.append(True))

    assert await stub._activate_daemon() == "started"
    assert reallocations == []
    assert stub._port == 40031


@pytest.mark.asyncio
async def test_a_live_child_is_waited_on_not_replaced(monkeypatch):
    # uvicorn binds before the app is ready, so a failed handshake while our
    # own child is alive must not be read as a collision.
    stub = HeadlessIdaStub(port=40028, session_mode=True)
    stub._daemon_proc = _FakeProc(exit_code=None)

    async def _handshake():
        return "foreign", None

    monkeypatch.setattr(stub, "_handshake", _handshake)
    monkeypatch.setattr(stdio_stub, "_DAEMON_START_TIMEOUT_SEC", 2)

    assert await stub._await_daemon_start() == "timeout"
    assert stub._port == 40028


@pytest.mark.asyncio
async def test_default_mode_adopts_a_tokenless_warm_daemon(monkeypatch):
    stub = HeadlessIdaStub(port=40029)

    async def _handshake():
        return "ours", [_tool("server_health")]

    monkeypatch.setattr(stub, "_handshake", _handshake)

    assert stub._session_token is None
    assert [t.name for t in await stub.list_tools()] == [
        "start_daemon",
        "server_health",
    ]


# ---------------------------------------------------------------------------
# Concurrent isolation (real daemons)
# ---------------------------------------------------------------------------


def _idalib_available() -> bool:
    install_dir = os.environ.get("IDA_INSTALL_DIR", "")
    if not install_dir:
        for candidate in ("/opt/ida-pro-9.3", "/opt/ida-pro-9.0"):
            if os.path.isdir(candidate):
                os.environ["IDA_INSTALL_DIR"] = candidate
                install_dir = candidate
                break
    if not install_dir or not os.path.isdir(install_dir):
        return False
    return any(
        os.path.isfile(os.path.join(install_dir, libname))
        for libname in ("libidalib.so", "libidalib.dylib", "idalib.dll")
    )


@pytest.mark.asyncio
@pytest.mark.skipif(not _idalib_available(), reason="idalib not available")
@pytest.mark.skipif(not SAMPLE_BINARY.exists(), reason="sample binary missing")
async def test_concurrent_session_stubs_stay_isolated(tmp_path):
    import shutil

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def _run(name: str) -> dict:
        binary = tmp_path / name
        shutil.copy(SAMPLE_BINARY, binary)
        params = StdioServerParameters(
            command=str(REPO_ROOT / ".venv" / "bin" / "headless-ida-mcp-stub"),
            args=["--session-mode"],
            env={**os.environ, "IDA_INSTALL_DIR": os.environ["IDA_INSTALL_DIR"]},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                # The bootstrap tool description carries this stub's port, which
                # is the only endpoint the client can observe.
                listing = await session.list_tools()
                bootstrap = next(t for t in listing.tools if t.name == "start_daemon")
                port = int(re.search(r"port (\d+)", bootstrap.description or "").group(1))
                assert (await session.call_tool("start_daemon", {})).content[0].text == (
                    "started"
                )
                await session.call_tool("set_binary_path", {"path": str(binary)})
                health = json.loads(
                    (await session.call_tool("server_health", {})).content[0].text
                )
                return {
                    "port": port,
                    "token": health["session_token"],
                    "idb_path": health["idb_path"],
                }

    first, second = await asyncio.gather(_run("iso_a"), _run("iso_b"))

    assert first["port"] != second["port"]
    assert first["token"] and second["token"]
    assert first["token"] != second["token"]
    assert first["idb_path"] != second["idb_path"]
