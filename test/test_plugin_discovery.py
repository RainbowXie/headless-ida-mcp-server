# -*- coding: utf-8 -*-
"""Discovery tests covering Path A (entry points) and Path B (directory).

Covers tasks.md 3.6 plus several scenarios from
``specs/mcp-plugin-contract/spec.md``.
"""
from __future__ import annotations

import logging
import os
import textwrap
from pathlib import Path

import pytest

from headless_ida_mcp_server.plugins import discovery


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "plugins"


# ---------------------------------------------------------------------------
# Path B directory scan
# ---------------------------------------------------------------------------


def test_path_b_finds_demo_in_fixture_root():
    found = list(
        discovery.iter_directory_manifests([FIXTURE_ROOT])
    )
    names = {n for n, _, _ in found}
    assert "demo" in names
    assert "bar" in names


def test_path_b_skips_root_without_manifest(tmp_path):
    plugin_dir = tmp_path / "plugins" / "noop"
    plugin_dir.mkdir(parents=True)
    found = list(discovery.iter_directory_manifests([tmp_path / "plugins"]))
    assert found == []


def test_path_b_warns_on_broken_manifest(caplog):
    # The fixture ``broken/mcp_manifest.py`` has a SyntaxError on purpose.
    with caplog.at_level(logging.WARNING):
        found = list(discovery.iter_directory_manifests([FIXTURE_ROOT]))
    names = {n for n, _, _ in found}
    assert "broken" not in names
    assert any("broken" in r.getMessage() for r in caplog.records)


def test_path_b_handles_missing_root(tmp_path):
    fake = tmp_path / "no_such_dir"
    found = list(discovery.iter_directory_manifests([fake]))
    assert found == []


def test_merge_default_roots_uses_env(monkeypatch, tmp_path):
    monkeypatch.setenv("IDA_MCP_PLUGIN_PATHS", f"{tmp_path}:/another")
    roots = discovery.merge_default_roots()
    assert tmp_path in roots
    assert Path("/another") in roots
    # ~/.idapro/plugins is always first.
    assert roots[0] == Path.home() / ".idapro" / "plugins"


def test_merge_default_roots_drops_empty_tokens(monkeypatch):
    monkeypatch.setenv("IDA_MCP_PLUGIN_PATHS", ":a::b:")
    roots = discovery.merge_default_roots()
    assert Path("a") in roots
    assert Path("b") in roots
    assert Path("") not in roots


# ---------------------------------------------------------------------------
# discover_all driver
# ---------------------------------------------------------------------------


def test_discover_all_with_only_path_b(monkeypatch, tmp_path):
    # Isolate from the developer's real ~/.idapro/plugins by pointing at
    # tmp_path with a copy of the demo fixture.
    plugin_root = tmp_path / "plugins"
    demo_dir = plugin_root / "demo"
    demo_dir.mkdir(parents=True)
    (demo_dir / "mcp_manifest.py").write_text(
        textwrap.dedent(
            """
            def hello() -> str:
                return "hi"

            PLUGIN = {"name": "demo", "description": "x", "version": "0.1"}
            TOOLS = [
                {
                    "name": "hello",
                    "handler": hello,
                    "description": "h",
                    "tags": ["kind:read"],
                }
            ]
            """
        )
    )
    monkeypatch.setenv("IDA_MCP_PLUGIN_PATHS", str(plugin_root))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    out = discovery.discover_all()
    names = {p.plugin_name for p in out}
    assert "demo" in names


def test_discover_all_collision_two_path_b_aborts(monkeypatch, tmp_path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    for root in (root_a, root_b):
        plug = root / "dup"
        plug.mkdir(parents=True)
        (plug / "mcp_manifest.py").write_text(
            textwrap.dedent(
                """
                def h(): return None

                PLUGIN = {"name": "dup", "description": "x", "version": "0.1"}
                TOOLS = [
                    {"name": "h", "handler": h, "description": "h", "tags": ["kind:read"]}
                ]
                """
            )
        )
    monkeypatch.setenv("IDA_MCP_PLUGIN_PATHS", f"{root_a}:{root_b}")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    with pytest.raises(RuntimeError) as ei:
        discovery.discover_all()
    assert "dup" in str(ei.value)


def test_discover_all_path_a_wins_over_path_b(monkeypatch, tmp_path, caplog):
    """Same PLUGIN.name discovered via Path A and Path B -> Path A wins.

    We synthesise a Path A entry by monkeypatching
    ``iter_entry_point_manifests`` to yield a fake module.
    """
    import types

    fake_mod = types.ModuleType("fake_demo_manifest")

    def fake_handler():
        return "from path A"

    fake_mod.PLUGIN = {"name": "shared", "description": "from A", "version": "0.1"}
    fake_mod.TOOLS = [
        {
            "name": "h",
            "handler": fake_handler,
            "description": "h",
            "tags": ["kind:read"],
        }
    ]

    def fake_eps():
        yield "shared", fake_mod, "entry_point[fake]:shared"

    monkeypatch.setattr(discovery, "iter_entry_point_manifests", fake_eps)

    root_b = tmp_path / "b"
    plug = root_b / "shared"
    plug.mkdir(parents=True)
    (plug / "mcp_manifest.py").write_text(
        textwrap.dedent(
            """
            def h_b(): return "from path B"

            PLUGIN = {"name": "shared", "description": "from B", "version": "0.2"}
            TOOLS = [
                {"name": "h", "handler": h_b, "description": "h", "tags": ["kind:read"]}
            ]
            """
        )
    )
    monkeypatch.setenv("IDA_MCP_PLUGIN_PATHS", str(root_b))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

    with caplog.at_level(logging.INFO):
        out = discovery.discover_all()

    names = [p.plugin_name for p in out]
    assert names.count("shared") == 1
    winner = next(p for p in out if p.plugin_name == "shared")
    assert winner.discovery_path == "A"
    # Both source locations mentioned in the INFO message.
    info_msgs = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert any("entry_point[fake]" in m and "shared" in m for m in info_msgs)
