# -*- coding: utf-8 -*-
"""Meta tool tests: plugins / plugin_tools / enable_plugin / disable_plugin.

These tests exercise the meta-tool implementations from server.py against
a self-contained pipeline that does not need idalib. We rebuild the
SessionAwareFastMCP harness manually using the public APIs from
:mod:`headless_ida_mcp_server.plugins`.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "plugins"


class _FakeSession:
    def __init__(self) -> None:
        self.notifications: list[str] = []

    async def send_tool_list_changed(self) -> None:
        self.notifications.append("tools/list_changed")


class _MetaToolHarness:
    """In-process SessionAwareFastMCP harness without the vendored loop."""

    def __init__(self, plugin_paths: list[Path], exclude: str = "") -> None:
        os.environ["IDA_MCP_PLUGIN_PATHS"] = ":".join(str(p) for p in plugin_paths)
        os.environ["IDA_MCP_EXCLUDE_TAGS"] = exclude

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

        # Clean slate for module-level state.
        for k in list(tags_mod.TOOL_TAGS):
            if k not in tags_mod.builtin_tag_names():
                tags_mod.TOOL_TAGS.pop(k, None)
        session_state.reset_for_tests()

        import fnmatch

        patterns = tags_mod.parse_exclude_patterns(exclude)
        self._patterns = patterns
        self._tags = tags_mod
        self._session_state = session_state

        self.dispatch: dict[str, PluginToolRecord] = {}
        self.registry: dict[str, PluginRecord] = {}
        self.discovered = list(discovery.iter_directory_manifests(plugin_paths))

        for dir_name, module, source in self.discovered:
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
                if any(
                    any(fnmatch.fnmatchcase(t, pat) for t in merged)
                    for pat in patterns
                ):
                    continue
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
                self.dispatch[full] = rec
                record.tools.append(rec)
            if record.tools:
                self.registry[plugin_name] = record
        session_state.set_loaded_plugin_names(set(self.registry.keys()))

    # Replicate the body of the four meta tools so tests exercise the
    # exact same logic. Server.py's implementations call these via the
    # ``mcp._mcp_server.request_context.session`` indirection; here we
    # accept the session directly.

    def plugins(self, session) -> list[dict]:
        enabled = self._session_state.get_enabled(session)
        out: list[dict] = []
        for name, rec in self.registry.items():
            visible = sum(1 for t in rec.tools if t.mcp_visible)
            entry = {
                "name": rec.name,
                "description": rec.description,
                "version": rec.version,
                "tool_count": visible,
                "enabled": rec.name in enabled,
            }
            if rec.categories:
                entry["categories"] = list(rec.categories)
            out.append(entry)
        return out

    def plugin_tools(self, name: str):
        rec = self.registry.get(name)
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

    async def enable_plugin(self, session, name: str) -> dict:
        rec = self.registry.get(name)
        if rec is None:
            err_msg = self._not_loaded_message(name)
            return {"ok": False, "plugin": name, "error": err_msg}
        changed, _bag = self._session_state.enable(session, name)
        if not changed:
            return {
                "ok": True,
                "plugin": name,
                "already_enabled": True,
                "added": [],
            }
        await self._session_state.emit_tools_list_changed(session)
        return {
            "ok": True,
            "plugin": name,
            "added": [t.full_name for t in rec.tools if t.mcp_visible],
        }

    async def disable_plugin(self, session, name: str) -> dict:
        rec = self.registry.get(name)
        if rec is None:
            err_msg = self._not_loaded_message(name)
            return {"ok": False, "plugin": name, "error": err_msg}
        changed, _bag = self._session_state.disable(session, name)
        if not changed:
            return {
                "ok": True,
                "plugin": name,
                "already_disabled": True,
                "removed": [],
            }
        await self._session_state.emit_tools_list_changed(session)
        return {
            "ok": True,
            "plugin": name,
            "removed": [t.full_name for t in rec.tools if t.mcp_visible],
        }

    def _not_loaded_message(self, name: str) -> str:
        for dir_name, module, _src in self.discovered:
            block = getattr(module, "PLUGIN", {})
            if isinstance(block, dict) and block.get("name") == name:
                return f"plugin {name!r} not loaded (excluded by tag filter)"
        return f"plugin {name!r} not found. Use plugins() to list."


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_plugins_lists_loaded_plugins_with_enabled_flag():
    h = _MetaToolHarness([FIXTURE_ROOT])
    a = _FakeSession()
    asyncio.run(h.enable_plugin(a, "demo"))
    rows = h.plugins(a)
    by_name = {r["name"]: r for r in rows}
    assert by_name["demo"]["enabled"] is True
    assert by_name["bar"]["enabled"] is False
    assert "version" in by_name["demo"]
    # tool_count excludes mcp:false ``internal``: demo has 7 visible.
    assert by_name["demo"]["tool_count"] == 7


def test_plugin_tools_lists_visible_only_does_not_emit():
    h = _MetaToolHarness([FIXTURE_ROOT])
    s = _FakeSession()
    out = h.plugin_tools("demo")
    assert isinstance(out, list)
    names = {t["name"] for t in out}
    assert "demo__ping" in names
    assert "demo__internal" not in names  # mcp:false hidden
    # plugin_tools must not have mutated session state or emitted.
    assert s.notifications == []
    assert "demo" not in h._session_state.get_enabled(s)


def test_plugin_tools_unknown_returns_error_string():
    h = _MetaToolHarness([FIXTURE_ROOT])
    out = h.plugin_tools("nonexistent")
    assert out == "error: plugin 'nonexistent' not found. Use plugins() to list."


def test_enable_plugin_first_time_emits_notification():
    h = _MetaToolHarness([FIXTURE_ROOT])
    s = _FakeSession()
    out = asyncio.run(h.enable_plugin(s, "demo"))
    assert out["ok"] is True
    assert "demo__ping" in out["added"]
    assert s.notifications == ["tools/list_changed"]


def test_enable_plugin_idempotent_does_not_emit_twice():
    h = _MetaToolHarness([FIXTURE_ROOT])
    s = _FakeSession()
    asyncio.run(h.enable_plugin(s, "demo"))
    out = asyncio.run(h.enable_plugin(s, "demo"))
    assert out == {"ok": True, "plugin": "demo", "already_enabled": True, "added": []}
    assert s.notifications == ["tools/list_changed"]


def test_enable_plugin_unknown_returns_error_no_emit():
    h = _MetaToolHarness([FIXTURE_ROOT])
    s = _FakeSession()
    out = asyncio.run(h.enable_plugin(s, "nope"))
    assert out["ok"] is False
    assert "not found" in out["error"]
    assert s.notifications == []


def test_enable_plugin_excluded_returns_not_loaded_error():
    h = _MetaToolHarness([FIXTURE_ROOT], exclude="ext::demo::*")
    s = _FakeSession()
    out = asyncio.run(h.enable_plugin(s, "demo"))
    assert out["ok"] is False
    assert "excluded by tag filter" in out["error"]
    assert s.notifications == []


def test_disable_plugin_happy_path_emits_notification():
    h = _MetaToolHarness([FIXTURE_ROOT])
    s = _FakeSession()
    asyncio.run(h.enable_plugin(s, "demo"))
    out = asyncio.run(h.disable_plugin(s, "demo"))
    assert out["ok"] is True
    assert "demo__ping" in out["removed"]
    assert s.notifications == ["tools/list_changed", "tools/list_changed"]


def test_disable_plugin_no_op_does_not_emit():
    h = _MetaToolHarness([FIXTURE_ROOT])
    s = _FakeSession()
    out = asyncio.run(h.disable_plugin(s, "demo"))
    assert out["already_disabled"] is True
    assert s.notifications == []


def test_two_sessions_isolation():
    h = _MetaToolHarness([FIXTURE_ROOT])
    a = _FakeSession()
    b = _FakeSession()
    asyncio.run(h.enable_plugin(a, "demo"))
    assert "demo" in h._session_state.get_enabled(a)
    assert "demo" not in h._session_state.get_enabled(b)


def test_plugins_tool_count_reflects_post_exclude_filter():
    h = _MetaToolHarness([FIXTURE_ROOT], exclude="ext::demo::ping")
    s = _FakeSession()
    rows = h.plugins(s)
    by_name = {r["name"]: r for r in rows}
    # demo had 7 visible; remove ping -> 6.
    assert by_name["demo"]["tool_count"] == 6
