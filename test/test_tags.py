# -*- coding: utf-8 -*-
"""Unit tests for the capability tag table.

These tests deliberately avoid any IDA / idalib dependency: they only
exercise the pure-Python ``tags.py`` module so they can run on CI hosts
that do not ship IDA. The end-to-end tag enforcement (registration loop
filtering, undo wrapping) is exercised by smoke tests in the
agent-quickstart guide and the ``/opsx:verify`` step of this change.
"""
from __future__ import annotations

from headless_ida_mcp_server.tags import (
    TOOL_TAGS,
    is_excluded,
    parse_exclude_patterns,
    tags_for,
)

KIND_NAMESPACE = "kind:"
KIND_VALUES = {"kind:read", "kind:write", "kind:unsafe"}


# ---------------------------------------------------------------------------
# Tag table self-consistency
# ---------------------------------------------------------------------------


def test_every_enumerated_tool_has_exactly_one_kind_tag():
    """Every entry in TOOL_TAGS MUST carry exactly one kind:* tag."""
    failures = []
    for name, tags in TOOL_TAGS.items():
        kind_tags = [t for t in tags if t.startswith(KIND_NAMESPACE)]
        if len(kind_tags) != 1:
            failures.append((name, kind_tags))
        elif kind_tags[0] not in KIND_VALUES:
            failures.append((name, kind_tags))
    assert not failures, (
        "tools without exactly one kind:read|write|unsafe tag: "
        f"{failures}"
    )


def test_kind_tags_are_mutually_exclusive():
    """No tool may carry two of {read, write, unsafe} simultaneously."""
    overlap = []
    for name, tags in TOOL_TAGS.items():
        present = KIND_VALUES.intersection(tags)
        if len(present) > 1:
            overlap.append((name, sorted(present)))
    assert not overlap, f"mutually exclusive kind:* violations: {overlap}"


# ---------------------------------------------------------------------------
# Helper contracts
# ---------------------------------------------------------------------------


def test_unknown_name_falls_back_to_kind_read():
    """tags_for() returns the conservative kind:read default for unknowns."""
    assert tags_for("definitely_not_a_real_tool_xyz") == ["kind:read"]


def test_known_name_returns_a_copy_not_the_internal_list():
    """tags_for() must not let a caller mutate TOOL_TAGS in place."""
    tags = tags_for("decompile")
    tags.append("kind:write")  # caller mutation
    # Re-read should not reflect the mutation.
    assert "kind:write" not in tags_for("decompile")


def test_is_excluded_empty_patterns_returns_false():
    """An empty pattern list short-circuits to False."""
    assert is_excluded("rename", []) is False
    assert is_excluded("rename", None) is False  # type: ignore[arg-type]


def test_is_excluded_matches_kind_write():
    """`--exclude-tags kind:write` drops every kind:write tool."""
    assert is_excluded("rename", ["kind:write"]) is True
    assert is_excluded("set_type", ["kind:write"]) is True


def test_is_excluded_does_not_match_unrelated_tag():
    """A read-only tool must NOT match a kind:write filter."""
    assert is_excluded("decompile", ["kind:write"]) is False
    assert is_excluded("xrefs_to", ["kind:write"]) is False


def test_is_excluded_glob_matches_group_path():
    """Glob in core::debug::* drops every dbg_* tool."""
    assert is_excluded("dbg_start", ["core::debug::*"]) is True
    assert is_excluded("dbg_write", ["core::debug::*"]) is True
    # An unrelated read tool must not match a debug glob.
    assert is_excluded("decompile", ["core::debug::*"]) is False


def test_is_excluded_unknown_name_treated_as_kind_read():
    """An unknown name still walks the default kind:read list when filtered."""
    assert is_excluded("unknown_tool_xyz", ["kind:read"]) is True
    assert is_excluded("unknown_tool_xyz", ["kind:write"]) is False


def test_is_excluded_self_destruct_pattern():
    """`kind:*` matches every tool in the table (the spec calls this an
    accepted self-destructive configuration -- server still starts; tools
    just register at zero).
    """
    for name in TOOL_TAGS:
        assert is_excluded(name, ["kind:*"]) is True


# ---------------------------------------------------------------------------
# parse_exclude_patterns
# ---------------------------------------------------------------------------


def test_parse_empty_string():
    assert parse_exclude_patterns("") == []


def test_parse_strips_whitespace_and_drops_empty_tokens():
    assert parse_exclude_patterns(
        " kind:write , , kind:unsafe ,, "
    ) == ["kind:write", "kind:unsafe"]


def test_parse_single_token():
    assert parse_exclude_patterns("kind:unsafe") == ["kind:unsafe"]


# ---------------------------------------------------------------------------
# Fork-only tool tags lock-in (spec scenario)
# ---------------------------------------------------------------------------


def test_fork_only_tool_tags():
    """The four fork-only tools have the exact tier the spec mandates."""
    assert "kind:write" in tags_for("set_binary_path")
    assert "kind:unsafe" in tags_for("unset")
    assert "kind:unsafe" in tags_for("py_eval")
    assert "kind:read" in tags_for("undo")
