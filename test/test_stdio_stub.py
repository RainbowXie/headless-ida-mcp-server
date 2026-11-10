# -*- coding: utf-8 -*-
"""Tests for stdio_stub module import and basic functionality."""

import pytest
from mcp.types import Tool as MCPTool

from headless_ida_mcp_server import stdio_stub
from headless_ida_mcp_server.stdio_stub import HeadlessIdaStub, streamable_http_client


def _tool(name: str) -> MCPTool:
    return MCPTool(name=name, inputSchema={"type": "object", "properties": {}})


def test_stdio_stub_import():
    # 验证 streamable_http_client 已正确从 mcp.client.streamable_http 导入且可调用
    assert callable(streamable_http_client)


@pytest.mark.asyncio
async def test_headless_ida_stub_list_tools_initial(monkeypatch):
    # 没有 daemon 应答时，list_tools 应当只返回初始引导工具 start_daemon
    stub = HeadlessIdaStub()

    async def _no_daemon() -> bool:
        return False

    monkeypatch.setattr(stub, "_probe_daemon", _no_daemon)
    tools = await stub.list_tools()
    assert len(tools) == 1
    assert tools[0].name == "start_daemon"


@pytest.mark.asyncio
async def test_list_tools_adopts_warm_daemon(monkeypatch):
    # 热 daemon 已就绪时，第一次列表就要给出完整工具面，不需要先调 start_daemon，
    # 也不依赖 client 收到 list_changed 通知。
    stub = HeadlessIdaStub(port=40004)

    async def _handshake():
        return "ours", [_tool("server_health"), _tool("lookup_funcs")]

    monkeypatch.setattr(stub, "_handshake", _handshake)

    assert [t.name for t in await stub.list_tools()] == [
        "start_daemon",
        "server_health",
        "lookup_funcs",
    ]


@pytest.mark.asyncio
async def test_list_tools_falls_back_when_handshake_fails(monkeypatch):
    # 端口有人监听但不是我们的 daemon（握手失败/超时）时，只能给 bootstrap 面。
    stub = HeadlessIdaStub(port=40005)

    async def _handshake():
        return "foreign", None

    monkeypatch.setattr(stub, "_handshake", _handshake)

    assert [t.name for t in await stub.list_tools()] == ["start_daemon"]


@pytest.mark.asyncio
async def test_start_daemon_stays_visible_after_activation():
    # call_tool 在 daemon 挂掉时让 agent 去调 start_daemon；client 只在
    # list_changed 通知时重取工具列表，所以 start_daemon 必须一直可见，
    # 否则这条恢复路径在 client 的旧快照里根本不存在。
    stub = HeadlessIdaStub()
    stub._real_tools = [_tool("server_health")]

    assert [t.name for t in await stub.list_tools()] == [
        "start_daemon",
        "server_health",
    ]


@pytest.mark.asyncio
async def test_activate_respawns_dead_daemon(monkeypatch):
    # 握手说 absent 就意味着 daemon 已经死了，必须重新拉起并重新拉取工具面，
    # 而不是把一个死进程报成 "running"。
    stub = HeadlessIdaStub(port=40001, session_mode=True)
    stub._real_tools = [_tool("stale_tool")]
    spawn_calls = []
    notify_calls = []

    async def _handshake():
        return "absent", None

    def _spawn() -> None:
        spawn_calls.append(True)

    async def _await_start() -> str:
        stub._real_tools = [_tool("server_health")]
        return "ready"

    async def _notify() -> None:
        notify_calls.append(True)

    monkeypatch.setattr(stub, "_handshake", _handshake)
    monkeypatch.setattr(stub, "_spawn_daemon", _spawn)
    monkeypatch.setattr(stub, "_await_daemon_start", _await_start)
    monkeypatch.setattr(stub, "_notify_tools_changed", _notify)

    assert await stub._activate_daemon() == "started"
    assert spawn_calls == [True]
    # 旧 daemon 的工具面不能残留：新 daemon 的工具面要重新拉取，并通知 client 重取。
    assert [t.name for t in stub._real_tools] == ["server_health"]
    assert notify_calls == [True]


@pytest.mark.asyncio
async def test_activate_reports_startup_failure_as_error_text(monkeypatch):
    # spawn / MCP 握手失败必须以 error: 文本返回，不能把 ExceptionGroup
    # 抛进 MCP 传输层（client 会据此判定整个 server 已死）。
    stub = HeadlessIdaStub(port=40002, session_mode=True)

    async def _probe() -> bool:
        return False

    def _boom() -> None:
        raise OSError("address already in use")

    monkeypatch.setattr(stub, "_probe_daemon", _probe)
    monkeypatch.setattr(stub, "_spawn_daemon", _boom)

    result = await stub._activate_daemon()

    assert result.startswith("error: daemon startup failed")
    assert stub._real_tools is None


@pytest.mark.asyncio
async def test_call_tool_reports_dead_daemon_as_error_text(monkeypatch):
    # daemon 死掉时底层抛的是 anyio 的 ExceptionGroup
    # ("unhandled errors in a TaskGroup")。必须转成 error: 文本，并丢弃
    # 已失效的工具面，让后续 start_daemon 真的重启 daemon。
    stub = HeadlessIdaStub(port=40003, session_mode=True)
    stub._real_tools = [_tool("server_health")]

    class _BrokenTransport:
        async def __aenter__(self):
            raise ExceptionGroup(
                "unhandled errors in a TaskGroup", [ConnectionRefusedError("refused")]
            )

        async def __aexit__(self, *exc_info):
            return False

    monkeypatch.setattr(
        stdio_stub, "streamable_http_client", lambda _url: _BrokenTransport()
    )

    result = await stub.call_tool("server_health", {})

    assert len(result) == 1
    assert result[0].text.startswith("error: daemon unreachable")
    assert "start_daemon" in result[0].text
    # 工具面描述的是"有哪些工具"，一次转发失败不代表 daemon 换了工具面。
    assert [t.name for t in stub._real_tools] == ["server_health"]
