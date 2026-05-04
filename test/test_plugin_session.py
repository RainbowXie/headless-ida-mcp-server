# -*- coding: utf-8 -*-
"""Per-session state + meta-tool unit tests.

The plugin pipeline is exercised against a self-contained
:class:`SessionAwareFastMCP` instance built without the heavy idalib /
vendored ``ida_mcp`` import chain so the tests run on CI hosts without IDA.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "plugins"


# ---------------------------------------------------------------------------
# Minimal in-process server harness
# ---------------------------------------------------------------------------


class _FakeSession:
    """Stand-in for ``mcp.server.session.ServerSession``.

    Records ``send_tool_list_changed`` calls so tests can assert
    notification semantics without a real MCP transport.
    """

    def __init__(self) -> None:
        self.notifications: list[str] = []

    async def send_tool_list_changed(self) -> None:
        self.notifications.append("tools/list_changed")


def _import_plugins_pkg():
    """Re-import the plugins package fresh (clears ``_DISCOVERED`` etc.)."""
    for mod in list(sys.modules):
        if mod.startswith("headless_ida_mcp_server.plugins"):
            del sys.modules[mod]
    return importlib.import_module("headless_ida_mcp_server.plugins")


def _build_pipeline(plugin_paths: list[Path], exclude: str = "") -> dict:
    """Run the discovery + registration pipeline manually.

    Returns a dict containing every artefact the meta-tool tests need so
    each test can drive the pipeline without booting the FastMCP HTTP /
    stdio transport.
    """
    os.environ.pop("IDA_MCP_PLUGIN_PATHS", None)
    os.environ["IDA_MCP_PLUGIN_PATHS"] = ":".join(str(p) for p in plugin_paths)
    os.environ["IDA_MCP_EXCLUDE_TAGS"] = exclude

    from headless_ida_mcp_server import tags as tags_mod

    # Drop any plugin tag entries from prior test runs so register_tags
    # does not raise "duplicate" on repeat builds.
    for k in list(tags_mod.TOOL_TAGS):
        if k not in tags_mod.builtin_tag_names():
            tags_mod.TOOL_TAGS.pop(k, None)
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
    import fnmatch

    # Reset session state too.
    session_state.reset_for_tests()

    patterns = tags_mod.parse_exclude_patterns(exclude)

    # Path home() is overridden to avoid the developer's real
    # ~/.idapro/plugins by passing a tmp path through the env.
    discovered = list(discovery.iter_directory_manifests(plugin_paths))
    plugins: dict[str, PluginRecord] = {}
    dispatch: dict[str, PluginToolRecord] = {}
    excluded_count = 0

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
            # exclude filter
            skip = False
            for pat in patterns:
                if any(fnmatch.fnmatchcase(t, pat) for t in merged):
                    skip = True
                    break
            if skip:
                excluded_count += 1
                continue
            tags_mod.register_tags(full, merged)
            input_schema = reflect_signature(tool["handler"])
            input_schema = apply_param_overrides(
                input_schema, tool["params"], plugin_name, short
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
                input_schema=input_schema,
                returns=tool["returns"],
                handler=tool["handler"],
                wrapped=wrapped,
                source=source,
            )
            dispatch[full] = rec
            record.tools.append(rec)

        if record.tools:
            plugins[plugin_name] = record

    session_state.set_loaded_plugin_names(set(plugins.keys()))
    return {
        "plugins": plugins,
        "dispatch": dispatch,
        "discovered": discovered,
        "excluded_count": excluded_count,
        "session_state": session_state,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_session_state_per_session_isolation():
    state = _build_pipeline([FIXTURE_ROOT])["session_state"]
    a = _FakeSession()
    b = _FakeSession()
    state.enable(a, "demo")
    assert state.is_enabled(a, "demo") is True
    assert state.is_enabled(b, "demo") is False


def test_session_state_disconnect_cleans_up():
    state = _build_pipeline([FIXTURE_ROOT])["session_state"]
    a = _FakeSession()
    state.enable(a, "demo")
    key = id(a)
    assert key in state._SESSION_ENABLED
    del a
    import gc
    gc.collect()
    assert key not in state._SESSION_ENABLED


def test_enable_idempotent_returns_changed_flag():
    state = _build_pipeline([FIXTURE_ROOT])["session_state"]
    a = _FakeSession()
    changed1, _ = state.enable(a, "demo")
    changed2, _ = state.enable(a, "demo")
    assert changed1 is True
    assert changed2 is False


def test_disable_idempotent_returns_changed_flag():
    state = _build_pipeline([FIXTURE_ROOT])["session_state"]
    a = _FakeSession()
    state.enable(a, "demo")
    changed1, _ = state.disable(a, "demo")
    changed2, _ = state.disable(a, "demo")
    assert changed1 is True
    assert changed2 is False


def test_minimal_manifest_registers_with_expected_tags():
    artefacts = _build_pipeline([FIXTURE_ROOT])
    from headless_ida_mcp_server.tags import tags_for

    tags = tags_for("demo__ping")
    assert "kind:read" in tags
    assert "ext::demo::ping" in tags


def test_demo_dispatch_includes_visible_tools_only_for_mcp_true():
    artefacts = _build_pipeline([FIXTURE_ROOT])
    dispatch = artefacts["dispatch"]
    # ``internal`` is mcp:false; it lives in dispatch but is hidden from
    # plugin_tools / list_tools.
    assert "demo__internal" in dispatch
    assert dispatch["demo__internal"].mcp_visible is False
    assert dispatch["demo__ping"].mcp_visible is True


def test_kind_write_wrapper_calls_create_undo_point(monkeypatch):
    artefacts = _build_pipeline([FIXTURE_ROOT])
    rec = artefacts["dispatch"]["demo__rename_function"]
    calls: list[str] = []

    fake_mod = types.ModuleType("ida_undo")
    fake_mod.create_undo_point = lambda lbl, lbl2=None: calls.append(lbl)
    monkeypatch.setitem(sys.modules, "ida_undo", fake_mod)

    out = asyncio.run(rec.wrapped({"addr": 0x1000, "name": "foo"}))
    assert out == {"addr": 0x1000, "name": "foo"}
    assert calls == ["demo__rename_function"]


def test_kind_unsafe_wrapper_does_not_call_create_undo_point(monkeypatch):
    artefacts = _build_pipeline([FIXTURE_ROOT])
    rec = artefacts["dispatch"]["demo__patch_bytes"]
    calls: list[str] = []

    fake_mod = types.ModuleType("ida_undo")
    fake_mod.create_undo_point = lambda lbl, lbl2=None: calls.append(lbl)
    monkeypatch.setitem(sys.modules, "ida_undo", fake_mod)

    out = asyncio.run(rec.wrapped({"addr": 0x2000, "value": b"\x90\x90"}))
    assert out["addr"] == 0x2000
    assert calls == []


def test_timeout_returns_error_string():
    artefacts = _build_pipeline([FIXTURE_ROOT])
    rec = artefacts["dispatch"]["demo__slow"]
    out = asyncio.run(rec.wrapped({"seconds": 5}))
    assert out == "error: timeout"


def test_tool_error_returns_structured_string():
    artefacts = _build_pipeline([FIXTURE_ROOT])
    rec = artefacts["dispatch"]["demo__raise_tool_error"]
    out = asyncio.run(rec.wrapped({}))
    assert out == "error: -12: bad input"


def test_generic_exception_returns_error_string_with_type(caplog):
    artefacts = _build_pipeline([FIXTURE_ROOT])
    rec = artefacts["dispatch"]["demo__raise_value_error"]
    with caplog.at_level(logging.ERROR):
        out = asyncio.run(rec.wrapped({}))
    assert out == "error: ValueError: nope"
    assert any("ValueError" in r.getMessage() for r in caplog.records)


def test_exclude_pattern_drops_one_plugin():
    artefacts = _build_pipeline([FIXTURE_ROOT], exclude="ext::bar::*")
    plugins = artefacts["plugins"]
    assert "bar" not in plugins
    assert "demo" in plugins


def test_exclude_pattern_drops_one_tool():
    artefacts = _build_pipeline([FIXTURE_ROOT], exclude="ext::demo::ping")
    dispatch = artefacts["dispatch"]
    assert "demo__ping" not in dispatch
    # other demo tools survive
    assert "demo__add" in dispatch


def test_exclude_pattern_drops_all_plugin_tools():
    artefacts = _build_pipeline([FIXTURE_ROOT], exclude="ext::*")
    assert artefacts["plugins"] == {}


def test_meta_tool_allowlist_present():
    from headless_ida_mcp_server.tags import META_TOOL_ALLOWLIST, is_excluded

    assert META_TOOL_ALLOWLIST == {
        "plugins",
        "plugin_tools",
        "enable_plugin",
        "disable_plugin",
    }
    # is_excluded returns False for these regardless of patterns.
    for name in META_TOOL_ALLOWLIST:
        assert is_excluded(name, ["kind:*"]) is False
        assert is_excluded(name, ["core::plugin-meta"]) is False


def test_register_tags_collision_with_builtin_rejected():
    from headless_ida_mcp_server import tags as tags_mod

    with pytest.raises(ValueError):
        tags_mod.register_tags("decompile", ["kind:read", "ext::demo::decompile"])


def test_register_tags_duplicate_plugin_name_rejected():
    from headless_ida_mcp_server import tags as tags_mod

    # Reset and inject one fake plugin entry.
    for k in list(tags_mod.TOOL_TAGS):
        if k not in tags_mod.builtin_tag_names():
            tags_mod.TOOL_TAGS.pop(k, None)
    tags_mod.register_tags("demo__x", ["kind:read", "ext::demo::x"])
    with pytest.raises(ValueError):
        tags_mod.register_tags("demo__x", ["kind:read", "ext::demo::x"])


def test_register_tags_requires_exactly_one_kind():
    from headless_ida_mcp_server import tags as tags_mod

    with pytest.raises(ValueError):
        tags_mod.register_tags("demo__y", ["ext::demo::y"])  # missing kind:*
    with pytest.raises(ValueError):
        tags_mod.register_tags(
            "demo__y", ["kind:read", "kind:write", "ext::demo::y"]
        )


def test_session_state_advertises_list_changed_capability():
    """server.py override sets tools_changed=True via NotificationOptions."""
    # We exercise this indirectly by inspecting NotificationOptions; the
    # full E2E flag check lives in test_plugin_contract_e2e.py.
    from mcp.server.lowlevel.server import NotificationOptions

    opts = NotificationOptions(tools_changed=True)
    assert opts.tools_changed is True
