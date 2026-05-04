# -*- coding: utf-8 -*-
"""End-to-end MCP tests over the in-memory transport.

Exercises the SessionAwareFastMCP subclass + plugin dispatch through a
real ``mcp.client.session.ClientSession`` so the contract holds at the
protocol level (notification emission, list_tools refetch, call_tool
routing).
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import timedelta
from pathlib import Path

import anyio
import pytest

from mcp.client.session import ClientSession
from mcp.server.lowlevel.server import NotificationOptions
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import Tool as MCPTool

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "plugins"


def _build_session_aware_server():
    """Return a SessionAwareFastMCP with the demo fixture loaded.

    Replicates the shape of server.py without booting idalib / the
    vendored ``ida_mcp`` loop.
    """
    from headless_ida_mcp_server import tags as tags_mod
    from headless_ida_mcp_server.plugins import (
        PluginRecord,
        PluginToolRecord,
        apply_param_overrides,
        make_plugin_tool_wrapper,
        reflect_signature,
        validate_plugin_block,
        validate_tool_entry,
    )
    from headless_ida_mcp_server.plugins import discovery, session_state

    # Reset module-level state.
    for k in list(tags_mod.TOOL_TAGS):
        if k not in tags_mod.builtin_tag_names():
            tags_mod.TOOL_TAGS.pop(k, None)
    session_state.reset_for_tests()

    # Build a minimal SessionAwareFastMCP. We re-implement the subclass
    # locally because importing server.py would trigger the vendored
    # tool loop, which depends on idalib.
    from mcp.server import FastMCP
    from typing import Any, Sequence

    dispatch: dict[str, PluginToolRecord] = {}
    registry: dict[str, PluginRecord] = {}

    discovered = list(discovery.iter_directory_manifests([FIXTURE_ROOT]))
    for dir_name, module, source in discovered:
        block = validate_plugin_block(getattr(module, "PLUGIN"), source)
        plugin_name = block["name"]
        record = PluginRecord(
            name=plugin_name,
            description=block["description"],
            version=block["version"],
            categories=block["categories"],
            source=source,
        )
        for raw_tool in module.TOOLS:
            tool = validate_tool_entry(raw_tool, plugin_name, source)
            full = tool["full_name"]
            short = tool["short_name"]
            ext_tag = f"ext::{plugin_name}::{short}"
            merged = list(tool["tags"]) + [ext_tag]
            tags_mod.register_tags(full, merged)
            schema = reflect_signature(tool["handler"])
            schema = apply_param_overrides(
                schema, tool["params"], plugin_name, short
            )
            wrapped = make_plugin_tool_wrapper(
                full,
                tool["handler"],
                needs_undo=(tool["kind_tag"] == "kind:write"),
                timeout=tool["timeout"],
            )
            rec = PluginToolRecord(
                full_name=full,
                short_name=short,
                plugin_name=plugin_name,
                description=tool["description"],
                tags=merged,
                timeout=tool["timeout"],
                mcp_visible=tool["mcp_visible"],
                input_schema=schema,
                returns=tool["returns"],
                handler=tool["handler"],
                wrapped=wrapped,
                source=source,
            )
            dispatch[full] = rec
            record.tools.append(rec)
        registry[plugin_name] = record
    session_state.set_loaded_plugin_names(set(registry.keys()))

    class _Srv(FastMCP):
        async def list_tools(self):
            builtin = await super().list_tools()
            try:
                session = self._mcp_server.request_context.session
            except Exception:
                session = None
            if session is None:
                return list(builtin)
            enabled = session_state.get_enabled(session)
            extra = [
                MCPTool(
                    name=rec.full_name,
                    description=rec.description,
                    inputSchema=rec.input_schema,
                )
                for rec in dispatch.values()
                if rec.mcp_visible and rec.plugin_name in enabled
            ]
            return list(builtin) + extra

        async def call_tool(self, name, arguments):
            from mcp.types import TextContent

            rec = dispatch.get(name)
            if rec is None:
                return await super().call_tool(name, arguments)
            try:
                session = self._mcp_server.request_context.session
            except Exception:
                session = None
            if rec.mcp_visible and session is not None:
                if rec.plugin_name not in session_state.get_enabled(session):
                    return [
                        TextContent(
                            type="text",
                            text=(
                                f"error: tool {name!r} not enabled. "
                                f"Call enable_plugin({rec.plugin_name!r}) first."
                            ),
                        )
                    ]
            result = await rec.wrapped(arguments or {})
            if isinstance(result, str):
                return [TextContent(type="text", text=result)]
            return result

    srv = _Srv("Test")

    # Register four meta tools.
    def plugins_tool() -> list[dict]:
        try:
            session = srv._mcp_server.request_context.session
        except Exception:
            session = None
        enabled = session_state.get_enabled(session) if session else set()
        out = []
        for n, r in registry.items():
            visible = sum(1 for t in r.tools if t.mcp_visible)
            out.append(
                {
                    "name": r.name,
                    "description": r.description,
                    "version": r.version,
                    "tool_count": visible,
                    "enabled": r.name in enabled,
                }
            )
        return out

    plugins_tool.__name__ = "plugins"

    def plugin_tools_tool(name: str):
        rec = registry.get(name)
        if rec is None:
            return f"error: plugin {name!r} not found. Use plugins() to list."
        return [
            {
                "name": t.full_name,
                "description": t.description,
                "signature": t.input_schema,
                "tags": list(t.tags),
                "timeout": t.timeout,
                "returns": t.returns,
            }
            for t in rec.tools
            if t.mcp_visible
        ]

    plugin_tools_tool.__name__ = "plugin_tools"

    async def enable_plugin_tool(name: str) -> dict:
        try:
            session = srv._mcp_server.request_context.session
        except Exception:
            session = None
        rec = registry.get(name)
        if rec is None:
            return {"ok": False, "plugin": name, "error": f"plugin {name!r} not found. Use plugins() to list."}
        changed, _ = session_state.enable(session, name)
        if not changed:
            return {"ok": True, "plugin": name, "already_enabled": True, "added": []}
        if session is not None:
            await session_state.emit_tools_list_changed(session)
        return {
            "ok": True,
            "plugin": name,
            "added": [t.full_name for t in rec.tools if t.mcp_visible],
        }

    enable_plugin_tool.__name__ = "enable_plugin"

    async def disable_plugin_tool(name: str) -> dict:
        try:
            session = srv._mcp_server.request_context.session
        except Exception:
            session = None
        rec = registry.get(name)
        if rec is None:
            return {"ok": False, "plugin": name, "error": f"plugin {name!r} not found."}
        changed, _ = session_state.disable(session, name)
        if not changed:
            return {"ok": True, "plugin": name, "already_disabled": True, "removed": []}
        if session is not None:
            await session_state.emit_tools_list_changed(session)
        return {
            "ok": True,
            "plugin": name,
            "removed": [t.full_name for t in rec.tools if t.mcp_visible],
        }

    disable_plugin_tool.__name__ = "disable_plugin"

    srv.add_tool(plugins_tool, structured_output=False)
    srv.add_tool(plugin_tools_tool, structured_output=False)
    srv.add_tool(enable_plugin_tool, structured_output=False)
    srv.add_tool(disable_plugin_tool, structured_output=False)
    return srv, dispatch, registry


# ---------------------------------------------------------------------------
# anyio-based runner (mcp memory transport requires anyio)
# ---------------------------------------------------------------------------


async def _run_e2e_round_trip():
    srv, dispatch, registry = _build_session_aware_server()
    server = srv._mcp_server
    list_changed_count = {"n": 0}

    async def _message_handler(msg):
        # Look at notifications.
        from mcp import types as mtypes
        from mcp.shared.session import RequestResponder

        if isinstance(msg, RequestResponder):
            return
        if isinstance(msg, Exception):
            return
        # Notification.
        try:
            method = msg.root.method  # ServerNotification.root
        except AttributeError:
            method = None
        if method == "notifications/tools/list_changed":
            list_changed_count["n"] += 1

    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams

        async with anyio.create_task_group() as tg:
            init_opts = server.create_initialization_options(
                notification_options=NotificationOptions(tools_changed=True),
            )
            tg.start_soon(
                lambda: server.run(server_read, server_write, init_opts, raise_exceptions=True)
            )

            async with ClientSession(
                client_read,
                client_write,
                read_timeout_seconds=timedelta(seconds=5),
                message_handler=_message_handler,
            ) as client:
                init_result = await client.initialize()
                # tools.listChanged should be advertised.
                caps = init_result.capabilities
                assert caps.tools is not None
                assert caps.tools.listChanged is True

                # 1. Initial list_tools should NOT include any plugin tool.
                tools = await client.list_tools()
                names = {t.name for t in tools.tools}
                assert "plugins" in names
                assert "plugin_tools" in names
                assert "enable_plugin" in names
                assert "disable_plugin" in names
                assert "demo__ping" not in names

                # 2. plugins()
                plugins_result = await client.call_tool("plugins", {})
                # Result content -> first item is text JSON.
                assert plugins_result.isError is False

                # 3. plugin_tools(demo) is peek-only.
                pt = await client.call_tool("plugin_tools", {"name": "demo"})
                assert pt.isError is False
                tools_after_peek = await client.list_tools()
                assert "demo__ping" not in {t.name for t in tools_after_peek.tools}

                # 4. enable_plugin -> expect notification + list refetch.
                before = list_changed_count["n"]
                en = await client.call_tool("enable_plugin", {"name": "demo"})
                assert en.isError is False
                # Give the runtime a tick to deliver notification.
                await anyio.sleep(0.05)
                assert list_changed_count["n"] == before + 1

                tools_after_enable = await client.list_tools()
                names_after = {t.name for t in tools_after_enable.tools}
                assert "demo__ping" in names_after
                assert "demo__internal" not in names_after  # mcp:false

                # 5. call demo__ping
                call = await client.call_tool("demo__ping", {"message": "x"})
                assert call.isError is False

                # 6. disable_plugin -> notification, list shrinks.
                before2 = list_changed_count["n"]
                dis = await client.call_tool("disable_plugin", {"name": "demo"})
                assert dis.isError is False
                await anyio.sleep(0.05)
                assert list_changed_count["n"] == before2 + 1
                tools_after_disable = await client.list_tools()
                assert "demo__ping" not in {t.name for t in tools_after_disable.tools}

            tg.cancel_scope.cancel()


def test_plugin_contract_e2e_round_trip():
    """Full enable -> list -> call -> disable round trip via in-memory MCP."""
    anyio.run(_run_e2e_round_trip)


async def _run_call_before_enable():
    srv, dispatch, registry = _build_session_aware_server()
    server = srv._mcp_server
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        async with anyio.create_task_group() as tg:
            init_opts = server.create_initialization_options(
                notification_options=NotificationOptions(tools_changed=True),
            )
            tg.start_soon(
                lambda: server.run(server_read, server_write, init_opts, raise_exceptions=True)
            )
            async with ClientSession(
                client_read,
                client_write,
                read_timeout_seconds=timedelta(seconds=5),
            ) as client:
                await client.initialize()
                # Call demo__ping without enabling demo first.
                resp = await client.call_tool("demo__ping", {})
                # Returns the not-enabled hint as a TextContent / string.
                assert resp.isError is False
                # Aggregate all text content to check the hint.
                msg = "".join(
                    getattr(c, "text", "") for c in resp.content
                )
                assert "not enabled" in msg
                assert "enable_plugin" in msg
                assert "demo" in msg
            tg.cancel_scope.cancel()


def test_call_before_enable_returns_hint():
    anyio.run(_run_call_before_enable)


# ---------------------------------------------------------------------------
# Path A entry-points discovery (Scenario 9.5)
# ---------------------------------------------------------------------------


def test_path_a_entry_points_discovery(tmp_path, monkeypatch):
    """Synthetic Path A: register a fake entry point and verify discovery.

    A real ``pyproject.toml`` install is heavy in CI; this test patches
    :func:`importlib.metadata.entry_points` to return a synthetic
    ``EntryPoint`` whose ``load()`` returns a fixture module.
    """
    import types
    from headless_ida_mcp_server.plugins import discovery

    fake_module = types.ModuleType("fake_demo")

    def _h() -> str:
        return "from path A"

    fake_module.PLUGIN = {
        "name": "from_pip",
        "description": "fake pip plugin",
        "version": "0.1",
    }
    fake_module.TOOLS = [
        {
            "name": "h",
            "handler": _h,
            "description": "h",
            "tags": ["kind:read"],
        }
    ]

    class _FakeEP:
        def __init__(self, name, value, mod):
            self.name = name
            self.value = value
            self._mod = mod

        def load(self):
            return self._mod

    def fake_eps(group):
        if group == "headless_ida_mcp.plugins":
            return [_FakeEP("from_pip", "fake_demo:mcp_manifest", fake_module)]
        return []

    monkeypatch.setattr("importlib.metadata.entry_points", fake_eps)

    out = list(discovery.iter_entry_point_manifests())
    assert any(name == "from_pip" for name, _, _ in out)
