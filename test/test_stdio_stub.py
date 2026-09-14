# -*- coding: utf-8 -*-
"""Tests for stdio_stub module import and basic functionality."""

import pytest
from headless_ida_mcp_server.stdio_stub import HeadlessIdaStub, streamable_http_client


def test_stdio_stub_import():
    # 验证 streamable_http_client 已正确从 mcp.client.streamable_http 导入且可调用
    assert callable(streamable_http_client)


@pytest.mark.asyncio
async def test_headless_ida_stub_list_tools_initial():
    # 在 daemon 未启动时，list_tools 应当返回初始引导工具 start_daemon
    stub = HeadlessIdaStub()
    tools = await stub.list_tools()
    assert len(tools) == 1
    assert tools[0].name == "start_daemon"
