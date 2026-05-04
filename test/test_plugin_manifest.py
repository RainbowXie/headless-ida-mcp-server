# -*- coding: utf-8 -*-
"""Schema validator + reflection unit tests.

Covers tasks.md 2.5 and most of the schema-related scenarios in
``specs/mcp-plugin-contract/spec.md``.
"""
from __future__ import annotations

from typing import Annotated, Optional

import pytest

from headless_ida_mcp_server.plugins import (
    PLUGIN_NAME_RE,
    PluginManifestError,
    ToolError,
    apply_param_overrides,
    reflect_signature,
    validate_plugin_block,
    validate_tool_entry,
)


# ---------------------------------------------------------------------------
# ToolError
# ---------------------------------------------------------------------------


def test_tool_error_str_format():
    err = ToolError(-12, "bad input")
    assert err.code == -12
    assert err.message == "bad input"
    assert str(err) == "-12: bad input"


# ---------------------------------------------------------------------------
# PLUGIN block validation
# ---------------------------------------------------------------------------


def test_plugin_block_minimal_valid():
    out = validate_plugin_block(
        {"name": "demo", "description": "desc", "version": "0.1"},
        source="memory://t",
    )
    assert out["name"] == "demo"
    assert out["categories"] == []


def test_plugin_block_uppercase_name_rejected():
    with pytest.raises(PluginManifestError) as ei:
        validate_plugin_block(
            {"name": "Demo", "description": "x", "version": "0.1"},
            source="path/to/plugin",
        )
    assert "PLUGIN.name" in str(ei.value)
    assert "path/to/plugin" in str(ei.value)


def test_plugin_block_missing_required_key():
    with pytest.raises(PluginManifestError) as ei:
        validate_plugin_block(
            {"name": "demo", "description": "x"},
            source="src",
        )
    assert "version" in str(ei.value)


def test_plugin_block_empty_description_rejected():
    with pytest.raises(PluginManifestError):
        validate_plugin_block(
            {"name": "demo", "description": "  ", "version": "0.1"},
            source="src",
        )


def test_plugin_block_name_too_long():
    with pytest.raises(PluginManifestError):
        validate_plugin_block(
            {"name": "a" * 33, "description": "x", "version": "0.1"},
            source="src",
        )


def test_plugin_block_categories_must_be_list_of_str():
    with pytest.raises(PluginManifestError):
        validate_plugin_block(
            {
                "name": "demo",
                "description": "x",
                "version": "0.1",
                "categories": [1, 2, 3],
            },
            source="src",
        )


def test_plugin_name_regex_seed_rules():
    assert PLUGIN_NAME_RE.match("demo")
    assert PLUGIN_NAME_RE.match("a")
    assert PLUGIN_NAME_RE.match("a1_b")
    assert not PLUGIN_NAME_RE.match("Demo")
    assert not PLUGIN_NAME_RE.match("1demo")
    assert not PLUGIN_NAME_RE.match("demo-bar")


# ---------------------------------------------------------------------------
# Tool entry validation
# ---------------------------------------------------------------------------


def _ok_tool(handler):
    return {
        "name": "ping",
        "handler": handler,
        "description": "ping",
        "tags": ["kind:read"],
    }


def test_tool_entry_minimal_valid():
    def h(): ...

    out = validate_tool_entry(_ok_tool(h), "demo", "src")
    assert out["full_name"] == "demo__ping"
    assert out["timeout"] == 30
    assert out["mcp_visible"] is True
    assert out["kind_tag"] == "kind:read"


def test_tool_entry_handler_must_be_callable():
    with pytest.raises(PluginManifestError) as ei:
        validate_tool_entry(
            {
                "name": "ping",
                "handler": "module.func",
                "description": "x",
                "tags": ["kind:read"],
            },
            "demo",
            "src",
        )
    assert "callable" in str(ei.value)


def test_tool_entry_missing_kind_tag_rejected():
    def h(): ...

    with pytest.raises(PluginManifestError) as ei:
        validate_tool_entry(
            {
                "name": "ping",
                "handler": h,
                "description": "x",
                "tags": ["custom:foo"],
            },
            "demo",
            "src",
        )
    assert "kind" in str(ei.value)


def test_tool_entry_two_kind_tags_rejected():
    def h(): ...

    with pytest.raises(PluginManifestError):
        validate_tool_entry(
            {
                "name": "ping",
                "handler": h,
                "description": "x",
                "tags": ["kind:read", "kind:write"],
            },
            "demo",
            "src",
        )


def test_tool_entry_timeout_must_be_positive_int():
    def h(): ...

    for bad in (0, -1, "30", 1.5, True):
        with pytest.raises(PluginManifestError):
            validate_tool_entry(
                {
                    "name": "ping",
                    "handler": h,
                    "description": "x",
                    "tags": ["kind:read"],
                    "timeout": bad,
                },
                "demo",
                "src",
            )


def test_tool_entry_mcp_flag_must_be_bool():
    def h(): ...

    with pytest.raises(PluginManifestError):
        validate_tool_entry(
            {
                "name": "ping",
                "handler": h,
                "description": "x",
                "tags": ["kind:read"],
                "mcp": "false",
            },
            "demo",
            "src",
        )


def test_tool_entry_prefixed_name_too_long():
    def h(): ...

    long_short = "a" * 32
    long_plugin = "b" * 32  # 32 + 2 + 32 = 66 > 64
    with pytest.raises(PluginManifestError) as ei:
        validate_tool_entry(
            {
                "name": long_short,
                "handler": h,
                "description": "x",
                "tags": ["kind:read"],
            },
            long_plugin,
            "src",
        )
    assert "64" in str(ei.value)


# ---------------------------------------------------------------------------
# Signature reflection: D2 mapping table
# ---------------------------------------------------------------------------


def test_reflect_int():
    def h(x: int) -> int: ...
    s = reflect_signature(h)
    assert s["properties"]["x"]["type"] == "integer"


def test_reflect_float():
    def h(x: float) -> float: ...
    s = reflect_signature(h)
    assert s["properties"]["x"]["type"] == "number"


def test_reflect_str():
    def h(x: str) -> str: ...
    s = reflect_signature(h)
    assert s["properties"]["x"]["type"] == "string"


def test_reflect_bool():
    def h(x: bool) -> bool: ...
    s = reflect_signature(h)
    assert s["properties"]["x"]["type"] == "boolean"


def test_reflect_list_of_t():
    def h(x: list[int]) -> int: ...
    s = reflect_signature(h)
    assert s["properties"]["x"]["type"] == "array"
    assert s["properties"]["x"]["items"]["type"] == "integer"


def test_reflect_dict():
    def h(x: dict[str, int]) -> int: ...
    s = reflect_signature(h)
    assert s["properties"]["x"]["type"] == "object"


def test_reflect_optional():
    def h(x: Optional[int] = None) -> int: ...
    s = reflect_signature(h)
    # Optional is not in required
    assert "x" not in s.get("required", [])
    assert s["properties"]["x"]["type"] == "integer"


def test_reflect_annotated_description():
    def h(ea: Annotated[int, "Effective address"]) -> dict: ...
    s = reflect_signature(h)
    assert s["properties"]["ea"]["type"] == "integer"
    assert s["properties"]["ea"]["description"] == "Effective address"


def test_reflect_no_annotation_defaults_to_string():
    def h(x): ...
    s = reflect_signature(h)
    assert s["properties"]["x"]["type"] == "string"


def test_reflect_required_vs_default():
    def h(a: int, b: int = 7): ...
    s = reflect_signature(h)
    assert s.get("required") == ["a"]
    assert s["properties"]["b"]["default"] == 7


# ---------------------------------------------------------------------------
# apply_param_overrides
# ---------------------------------------------------------------------------


def test_param_overrides_merge_description():
    def h(name: str): ...
    reflected = reflect_signature(h)
    out = apply_param_overrides(
        reflected,
        {"name": {"description": "override"}},
        plugin="demo",
        tool="t",
    )
    assert out["properties"]["name"]["description"] == "override"


def test_param_overrides_unknown_parameter_aborts():
    def h(name: str): ...
    reflected = reflect_signature(h)
    with pytest.raises(PluginManifestError) as ei:
        apply_param_overrides(
            reflected,
            {"missing": {"description": "..."}},
            plugin="demo",
            tool="t",
        )
    assert "missing" in str(ei.value)


def test_param_overrides_required_flag_added():
    def h(name: str = "x"): ...
    reflected = reflect_signature(h)
    out = apply_param_overrides(
        reflected,
        {"name": {"required": True}},
        plugin="demo",
        tool="t",
    )
    assert "name" in out.get("required", [])


def test_param_overrides_returns_input_when_empty():
    def h(name: str): ...
    reflected = reflect_signature(h)
    assert apply_param_overrides(reflected, {}, "p", "t") == reflected
    assert apply_param_overrides(reflected, None, "p", "t") == reflected
