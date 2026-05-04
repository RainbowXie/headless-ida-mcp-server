# -*- coding: utf-8 -*-
"""Plugin discovery (Path A entry-points + Path B directory scan).

This module owns:

* :func:`iter_entry_point_manifests` -- enumerates pip distributions that
  declare ``[project.entry-points."headless_ida_mcp.plugins"]``.
* :func:`iter_directory_manifests` -- scans configured directories one
  level deep for ``mcp_manifest.py``.
* :func:`merge_default_roots` -- the default Path B roots
  (``~/.idapro/plugins`` plus ``IDA_MCP_PLUGIN_PATHS``).
* :func:`discover_all` -- the top-level driver that combines both paths,
  applies the Path-A-wins-on-collision rule (per design D5), and surfaces
  manifest import failures as WARNING + skip (per D6).

Discovery only **imports** manifests; it never invokes any handler. The
caller (``server.py``) is responsible for validation (via
:mod:`headless_ida_mcp_server.plugins`) and for actually registering tools.
"""
from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Iterable, Iterator

from headless_ida_mcp_server.logger import logger

ENTRY_POINT_GROUP = "headless_ida_mcp.plugins"
MANIFEST_FILENAME = "mcp_manifest.py"


@dataclass
class DiscoveredPlugin:
    """A manifest module successfully imported during discovery.

    The caller validates ``module.PLUGIN`` / ``module.TOOLS`` against the
    schema in :mod:`headless_ida_mcp_server.plugins`.
    """

    plugin_name: str  # the entry-point name (Path A) or directory name (Path B)
    module: ModuleType
    source: str  # filesystem path or entry-point string
    discovery_path: str  # "A" or "B"


def _split_paths(env_value: str) -> list[str]:
    """Mirror the colon parser used by ``runtime-plugin-paths``."""
    if not env_value:
        return []
    return [p for p in env_value.split(":") if p]


def merge_default_roots() -> list[Path]:
    """Return the default Path B roots.

    Order: ``~/.idapro/plugins`` first, then each non-empty path in
    ``IDA_MCP_PLUGIN_PATHS``.
    """
    roots: list[Path] = []
    home_root = Path.home() / ".idapro" / "plugins"
    roots.append(home_root)
    env = os.environ.get("IDA_MCP_PLUGIN_PATHS", "")
    for raw in _split_paths(env):
        roots.append(Path(raw))
    return roots


def iter_entry_point_manifests() -> Iterator[tuple[str, ModuleType, str]]:
    """Path A: yield ``(plugin_name, module, source_str)`` for each entry point.

    Import failures are caught and surfaced via WARNING; failed manifests
    are skipped (consistent with design D6).
    """
    try:
        eps = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    except TypeError:  # pragma: no cover - Python < 3.10 compat path
        eps = importlib.metadata.entry_points().get(ENTRY_POINT_GROUP, [])  # type: ignore[attr-defined]

    for ep in eps:
        source = f"entry_point[{ENTRY_POINT_GROUP}]:{ep.name}={ep.value}"
        try:
            module = ep.load()
        except Exception as exc:
            logger.warning(
                "plugin manifest import failed: %s -> %s: %s",
                source,
                type(exc).__name__,
                exc,
            )
            continue
        yield ep.name, module, source


def _import_manifest_from_path(manifest_path: Path) -> ModuleType:
    """Import ``mcp_manifest.py`` at ``manifest_path`` as a fresh module.

    The parent directory is inserted at ``sys.path[0]`` if absent so a
    manifest's relative imports resolve.
    """
    plugin_dir = manifest_path.parent
    parent = str(plugin_dir.parent)
    if parent and parent not in sys.path:
        sys.path.insert(0, parent)

    # Use importlib.util.spec_from_file_location for a robust per-path
    # import that does not collide across plugins sharing module names.
    plugin_dir_name = plugin_dir.name
    module_name = f"_headless_ida_mcp_plugin_{plugin_dir_name}"
    spec = importlib.util.spec_from_file_location(module_name, manifest_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not build spec for {manifest_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def iter_directory_manifests(
    roots: Iterable[Path],
) -> Iterator[tuple[str, ModuleType, str]]:
    """Path B: scan each ``root`` one level deep for ``mcp_manifest.py``.

    Yields ``(plugin_name, module, source_str)`` for each successfully
    imported manifest. ``plugin_name`` is the directory basename. Import
    failures emit a WARNING and skip (design D6); broken / missing roots
    are silently skipped (the existing ``_inject_plugin_paths`` warning
    already covers missing-path operator visibility).
    """
    seen: set[str] = set()
    for root in roots:
        try:
            if not root.exists() or not root.is_dir():
                continue
        except OSError:  # pragma: no cover - defensive
            continue

        try:
            entries = sorted(root.iterdir())
        except OSError as exc:  # pragma: no cover - permission etc.
            logger.warning(
                "plugin discovery: cannot list %s: %r", root, exc
            )
            continue

        for child in entries:
            if not child.is_dir():
                continue
            manifest = child / MANIFEST_FILENAME
            if not manifest.is_file():
                continue
            plugin_dir_name = child.name
            # Same-directory under multiple roots: only keep the first.
            key = str(manifest.resolve())
            if key in seen:
                continue
            seen.add(key)
            source = str(manifest)
            try:
                module = _import_manifest_from_path(manifest)
            except Exception as exc:
                logger.warning(
                    "plugin manifest import failed: %s -> %s: %s",
                    source,
                    type(exc).__name__,
                    exc,
                )
                continue
            yield plugin_dir_name, module, source


def _read_plugin_name(module: ModuleType) -> str | None:
    """Best-effort PLUGIN.name read used for collision detection.

    Returns ``None`` if the manifest does not expose ``PLUGIN`` as a dict
    or the dict is missing ``name``. The full validator runs later.
    """
    plugin = getattr(module, "PLUGIN", None)
    if not isinstance(plugin, dict):
        return None
    name = plugin.get("name")
    return name if isinstance(name, str) else None


def discover_all(
    roots: Iterable[Path] | None = None,
) -> list[DiscoveredPlugin]:
    """Top-level driver: Path A first, then Path B with collision rule.

    When the same ``PLUGIN.name`` appears via both paths, the Path A entry
    wins and the Path B duplicate is skipped with an INFO log mentioning
    both source locations (design D5).
    """
    if roots is None:
        roots = merge_default_roots()

    out: list[DiscoveredPlugin] = []
    by_name: dict[str, DiscoveredPlugin] = {}

    # ---- Path A first.
    for ep_name, module, source in iter_entry_point_manifests():
        plugin_name = _read_plugin_name(module) or ep_name
        record = DiscoveredPlugin(
            plugin_name=plugin_name,
            module=module,
            source=source,
            discovery_path="A",
        )
        # Two Path-A entries with the same PLUGIN.name -> collision (D4)
        if plugin_name in by_name:
            existing = by_name[plugin_name]
            raise RuntimeError(
                f"PLUGIN.name {plugin_name!r} declared by two manifests:\n"
                f"  - {existing.source}\n"
                f"  - {source}\n"
                f"Rename one of them so plugin names stay globally unique."
            )
        by_name[plugin_name] = record
        out.append(record)

    # ---- Path B second; apply Path-A-wins rule.
    for dir_name, module, source in iter_directory_manifests(roots):
        plugin_name = _read_plugin_name(module) or dir_name
        if plugin_name in by_name:
            existing = by_name[plugin_name]
            if existing.discovery_path == "A":
                logger.info(
                    "plugin %r discovered via both Path A entry-point "
                    "(%s) and Path B directory (%s); Path A wins.",
                    plugin_name,
                    existing.source,
                    source,
                )
                continue
            # Two Path-B entries with the same PLUGIN.name -> collision (D4)
            raise RuntimeError(
                f"PLUGIN.name {plugin_name!r} declared by two manifests:\n"
                f"  - {existing.source}\n"
                f"  - {source}\n"
                f"Rename one of them so plugin names stay globally unique."
            )
        record = DiscoveredPlugin(
            plugin_name=plugin_name,
            module=module,
            source=source,
            discovery_path="B",
        )
        by_name[plugin_name] = record
        out.append(record)

    return out
