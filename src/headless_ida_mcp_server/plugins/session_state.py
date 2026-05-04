# -*- coding: utf-8 -*-
"""Per-session state for plugin enable/disable.

Per design D11/D16, each MCP session gets its own ``enabled_plugins`` set
keyed on ``id(server_session)``. Cleanup uses ``weakref.finalize`` so the
entry is dropped before ``id()`` can be reused.

The module is intentionally tiny: just a dict + helpers + the
``send_tool_list_changed`` notifier. The FastMCP override and the meta
tools live elsewhere; they call into this module to read / mutate state.
"""
from __future__ import annotations

import weakref
from typing import Any, Optional

from headless_ida_mcp_server.logger import logger

# id(session) -> set of enabled plugin names. Empty by default.
_SESSION_ENABLED: dict[int, set[str]] = {}
# id(session) -> finalize so the weakref keeps the cleanup hook live.
_SESSION_FINALIZERS: dict[int, "weakref.finalize"] = {}
# Plugin names that survived discovery + tag filter; readable by helpers.
# Populated by ``server.py`` during ``_register_plugins``.
_LOADED_PLUGIN_NAMES: set[str] = set()


def _drop_session(key: int) -> None:
    """``weakref.finalize`` callback: remove the per-session entry."""
    _SESSION_ENABLED.pop(key, None)
    _SESSION_FINALIZERS.pop(key, None)


def get_enabled(session: Any) -> set[str]:
    """Return the calling session's enabled-plugin set, lazily creating one.

    The returned set is the live storage; mutations are visible across
    callers in the same session.
    """
    key = id(session)
    bag = _SESSION_ENABLED.get(key)
    if bag is None:
        bag = set()
        _SESSION_ENABLED[key] = bag
        try:
            _SESSION_FINALIZERS[key] = weakref.finalize(
                session, _drop_session, key
            )
        except TypeError:  # pragma: no cover - object disallows weakref
            # If the session is not weakreferable on this platform, skip
            # the finalizer; we leak one set per dropped connection but
            # the leak is bounded and harmless.
            logger.debug(
                "plugins.session_state: session of type %s is not "
                "weakref-able; skipping finalizer",
                type(session).__name__,
            )
    return bag


def is_enabled(session: Any, plugin_name: str) -> bool:
    """``True`` iff the calling session has enabled ``plugin_name``."""
    return plugin_name in get_enabled(session)


def enable(
    session: Any, plugin_name: str
) -> tuple[bool, set[str]]:
    """Add ``plugin_name`` to the session's set.

    Returns ``(changed, current_set)``. ``changed`` is ``False`` when the
    plugin was already enabled (idempotent no-op).
    """
    bag = get_enabled(session)
    if plugin_name in bag:
        return False, bag
    bag.add(plugin_name)
    return True, bag


def disable(
    session: Any, plugin_name: str
) -> tuple[bool, set[str]]:
    """Remove ``plugin_name`` from the session's set.

    Returns ``(changed, current_set)``. ``changed`` is ``False`` when the
    plugin was not in the set.
    """
    bag = get_enabled(session)
    if plugin_name not in bag:
        return False, bag
    bag.discard(plugin_name)
    return True, bag


def loaded_plugin_names() -> set[str]:
    """Return the set of plugin names that survived discovery + tag filter."""
    return _LOADED_PLUGIN_NAMES


def set_loaded_plugin_names(names: set[str]) -> None:
    """Server.py calls this once during ``_register_plugins``."""
    _LOADED_PLUGIN_NAMES.clear()
    _LOADED_PLUGIN_NAMES.update(names)


def reset_for_tests() -> None:
    """Test helper: blow away all session state."""
    for key, fin in list(_SESSION_FINALIZERS.items()):
        try:
            fin.detach()
        except Exception:  # pragma: no cover
            pass
    _SESSION_ENABLED.clear()
    _SESSION_FINALIZERS.clear()


async def emit_tools_list_changed(session: Any) -> None:
    """Best-effort ``ServerSession.send_tool_list_changed()``.

    Failures are logged at WARNING level and never re-raised; a broken
    notifier must not break the calling tool path.
    """
    if session is None:
        return
    try:
        await session.send_tool_list_changed()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "send_tool_list_changed failed: %s: %s",
            type(exc).__name__,
            exc,
        )
