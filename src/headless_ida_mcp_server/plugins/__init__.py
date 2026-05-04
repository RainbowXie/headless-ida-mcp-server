# -*- coding: utf-8 -*-
"""Plugin registration contract.

This package implements the manifest-driven plugin contract described by
the OpenSpec change ``add-plugin-tool-registration-contract``. It exposes:

* :class:`ToolError` and :class:`PluginManifestError` exception types.
* :func:`validate_plugin_block` / :func:`validate_tool_entry` schema
  validators.
* :func:`reflect_signature` and :func:`apply_param_overrides` for the
  ``inspect.signature`` -> JSON Schema mapping.
* :class:`PluginRecord` and :class:`PluginToolRecord` dataclasses describing
  registered plugins / tools.
* The :func:`make_plugin_tool_wrapper` factory that owns timeout, undo, and
  error handling for plugin handlers.
* :class:`SessionAwareFastMCP` -- the FastMCP subclass that filters
  ``list_tools`` and routes ``call_tool`` based on per-session enabled
  plugins.

Discovery (Path A entry points / Path B directory scan) and the per-session
state map live in :mod:`headless_ida_mcp_server.plugins.discovery` and
:mod:`headless_ida_mcp_server.plugins.session_state`.

The package is lazy-import-friendly: importing it MUST NOT touch IDA, the
mcp transport, or any plugin handler. Plugin handlers themselves SHALL keep
``import idaapi`` / ``ida_*`` calls inside the handler body so plugin
discovery / registration does not bring up idalib.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
import re
import traceback
import typing
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from headless_ida_mcp_server.logger import logger as _server_logger

__all__ = [
    "ToolError",
    "PluginManifestError",
    "PLUGIN_NAME_RE",
    "MAX_NAME_LEN",
    "MAX_PREFIXED_LEN",
    "REQUIRED_KIND_TAGS",
    "PluginRecord",
    "PluginToolRecord",
    "validate_plugin_block",
    "validate_tool_entry",
    "reflect_signature",
    "apply_param_overrides",
    "make_plugin_tool_wrapper",
]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ToolError(Exception):
    """Plugin handlers raise this for expected, recoverable failures.

    The wrapper converts ``ToolError(code, message)`` into the string
    ``"error: <code>: <message>"`` per spec ``Requirement: ToolError
    converts to structured error string``.
    """

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = int(code)
        self.message = str(message)

    def __str__(self) -> str:  # pragma: no cover - exercised via wrapper
        return f"{self.code}: {self.message}"


class PluginManifestError(Exception):
    """Raised when a manifest violates the schema. Aborts startup.

    Carries the manifest source path so server.py can surface it in the
    abort message together with the offending field.
    """

    def __init__(self, message: str, source: str) -> None:
        super().__init__(f"{message} (source: {source})")
        self.message = message
        self.source = source


# ---------------------------------------------------------------------------
# Naming / schema constants
# ---------------------------------------------------------------------------

PLUGIN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
MAX_NAME_LEN = 32
MAX_PREFIXED_LEN = 64
REQUIRED_KIND_TAGS = frozenset({"kind:read", "kind:write", "kind:unsafe"})


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class PluginToolRecord:
    """One registered plugin tool.

    ``wrapped`` is the awaitable handler. ``mcp_visible`` mirrors the
    ``mcp`` flag from the manifest entry: ``True`` (default) means the
    tool participates in ``list_tools`` / ``plugin_tools`` /
    ``enable_plugin``; ``False`` means it is dispatch-only and bypasses
    the session-membership check.
    """

    full_name: str
    short_name: str
    plugin_name: str
    description: str
    tags: list[str]
    timeout: int
    mcp_visible: bool
    input_schema: dict
    returns: str
    handler: Callable[..., Any]
    wrapped: Callable[[dict], Awaitable[Any]]
    source: str  # manifest source path / entry-point string


@dataclass
class PluginRecord:
    """One registered plugin (one ``PLUGIN`` block from a manifest)."""

    name: str
    description: str
    version: str
    categories: list[str] = field(default_factory=list)
    source: str = ""  # filesystem path or entry-point string
    tools: list[PluginToolRecord] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Schema validators
# ---------------------------------------------------------------------------


def _require_str(d: dict, key: str, source: str, *, allow_empty: bool = False) -> str:
    if key not in d:
        raise PluginManifestError(f"missing required key '{key}'", source)
    value = d[key]
    if not isinstance(value, str):
        raise PluginManifestError(
            f"key '{key}' must be a string (got {type(value).__name__})",
            source,
        )
    if not allow_empty and not value.strip():
        raise PluginManifestError(f"key '{key}' must be a non-empty string", source)
    return value


def _check_name(name: str, what: str, source: str, *, max_len: int = MAX_NAME_LEN) -> None:
    if not isinstance(name, str) or not name:
        raise PluginManifestError(
            f"{what} must be a non-empty string", source
        )
    if not PLUGIN_NAME_RE.match(name):
        raise PluginManifestError(
            f"{what} {name!r} fails regex {PLUGIN_NAME_RE.pattern!r} "
            f"(must start with [a-z], use only [a-z0-9_])",
            source,
        )
    if len(name) > max_len:
        raise PluginManifestError(
            f"{what} {name!r} exceeds max length {max_len}",
            source,
        )


def validate_plugin_block(plugin: dict, source: str) -> dict:
    """Enforce the ``PLUGIN`` block schema.

    Returns a normalised dict suitable for storage in :class:`PluginRecord`
    (with defaults filled in).
    """
    if not isinstance(plugin, dict):
        raise PluginManifestError(
            f"PLUGIN must be a dict (got {type(plugin).__name__})",
            source,
        )

    name = _require_str(plugin, "name", source)
    _check_name(name, "PLUGIN.name", source)

    description = _require_str(plugin, "description", source)
    version = _require_str(plugin, "version", source, allow_empty=False)

    categories = plugin.get("categories", []) or []
    if not isinstance(categories, list) or not all(
        isinstance(c, str) for c in categories
    ):
        raise PluginManifestError(
            "PLUGIN.categories must be a list of strings", source
        )

    return {
        "name": name,
        "description": description,
        "version": version,
        "categories": list(categories),
    }


def validate_tool_entry(tool: dict, plugin_name: str, source: str) -> dict:
    """Enforce the per-tool entry schema.

    Returns a normalised dict; the caller is responsible for combining
    this with the prefixed full name and tag injection.
    """
    if not isinstance(tool, dict):
        raise PluginManifestError(
            f"TOOLS entry must be a dict (got {type(tool).__name__})",
            source,
        )

    short_name = _require_str(tool, "name", source)
    _check_name(short_name, f"TOOLS[{short_name!r}].name", source)
    prefixed = f"{plugin_name}__{short_name}"
    if len(prefixed) > MAX_PREFIXED_LEN:
        raise PluginManifestError(
            f"prefixed name {prefixed!r} exceeds {MAX_PREFIXED_LEN} chars",
            source,
        )

    description = _require_str(tool, "description", source)

    handler = tool.get("handler")
    if handler is None:
        raise PluginManifestError(
            f"TOOLS[{short_name!r}] missing required key 'handler'",
            source,
        )
    if not callable(handler):
        raise PluginManifestError(
            f"TOOLS[{short_name!r}].handler must be callable "
            f"(got {type(handler).__name__})",
            source,
        )

    tags = tool.get("tags")
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise PluginManifestError(
            f"TOOLS[{short_name!r}].tags must be a list of strings",
            source,
        )
    kind_tags = [t for t in tags if t in REQUIRED_KIND_TAGS]
    if len(kind_tags) != 1:
        raise PluginManifestError(
            f"TOOLS[{short_name!r}].tags must contain exactly one of "
            f"{sorted(REQUIRED_KIND_TAGS)} (got {kind_tags!r})",
            source,
        )

    timeout = tool.get("timeout", 30)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise PluginManifestError(
            f"TOOLS[{short_name!r}].timeout must be a positive int "
            f"(got {timeout!r})",
            source,
        )

    mcp_flag = tool.get("mcp", True)
    if not isinstance(mcp_flag, bool):
        raise PluginManifestError(
            f"TOOLS[{short_name!r}].mcp must be a bool "
            f"(got {type(mcp_flag).__name__})",
            source,
        )

    params = tool.get("params")
    if params is not None and not isinstance(params, dict):
        raise PluginManifestError(
            f"TOOLS[{short_name!r}].params must be a dict",
            source,
        )

    returns = tool.get("returns", "")
    if not isinstance(returns, str):
        raise PluginManifestError(
            f"TOOLS[{short_name!r}].returns must be a string",
            source,
        )

    return {
        "short_name": short_name,
        "full_name": prefixed,
        "description": description,
        "handler": handler,
        "tags": list(tags),
        "kind_tag": kind_tags[0],
        "timeout": timeout,
        "mcp_visible": mcp_flag,
        "params": params or {},
        "returns": returns,
    }


# ---------------------------------------------------------------------------
# Reflection: inspect.signature -> MCP JSON Schema
# ---------------------------------------------------------------------------

_PRIMITIVE_MAP: dict[Any, dict] = {
    int: {"type": "integer"},
    float: {"type": "number"},
    str: {"type": "string"},
    bool: {"type": "boolean"},
}


def _is_optional(tp: Any) -> tuple[bool, Any]:
    """Return ``(is_optional, inner_type)`` for ``Optional[T]`` / ``T | None``."""
    origin = typing.get_origin(tp)
    if origin is typing.Union or (
        # Py 3.10+ ``X | Y`` reports its origin as ``types.UnionType``.
        getattr(__import__("types"), "UnionType", None) is not None
        and origin is __import__("types").UnionType
    ):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        if len(typing.get_args(tp)) == 2 and len(args) == 1:
            return True, args[0]
    return False, tp


def _annotation_to_schema(annotation: Any) -> tuple[dict, Optional[str], list[Any]]:
    """Map a (possibly Annotated) type annotation to a JSON Schema fragment.

    Returns ``(schema, description, extras)`` where ``extras`` is a list of
    metadata items beyond the first description string (currently used for
    enum dicts).
    """
    description: Optional[str] = None
    extras: list[Any] = []

    # Unwrap Annotated[...]: typing.get_origin returns the ``typing.Annotated``
    # marker; typing.get_args returns ``(T, *metadata)``.
    if typing.get_origin(annotation) is typing.Annotated:
        annot_args = typing.get_args(annotation)
        annotation = annot_args[0]
        for meta in annot_args[1:]:
            if isinstance(meta, str) and description is None:
                description = meta
            else:
                extras.append(meta)

    # Optional[T] / T | None
    is_opt, inner = _is_optional(annotation)
    if is_opt:
        annotation = inner

    schema: dict
    origin = typing.get_origin(annotation)

    if annotation in _PRIMITIVE_MAP:
        schema = dict(_PRIMITIVE_MAP[annotation])
    elif annotation is list or origin is list:
        item_args = typing.get_args(annotation)
        if item_args:
            inner_schema, _, _ = _annotation_to_schema(item_args[0])
            schema = {"type": "array", "items": inner_schema}
        else:
            schema = {"type": "array"}
    elif annotation is dict or origin is dict:
        schema = {"type": "object"}
    elif annotation is inspect.Parameter.empty:
        schema = {"type": "string"}
    else:
        # Anything we don't know about (custom classes, NewType, etc.)
        # falls back to string per the conservative default in spec D2.
        schema = {"type": "string"}

    # Apply extras: an enum dict like {"enum": [...]} merges in.
    for meta in extras:
        if isinstance(meta, dict):
            for k, v in meta.items():
                schema[k] = v

    return schema, description, extras


def reflect_signature(handler: Callable[..., Any]) -> dict:
    """Build a JSON Schema (object) reflecting ``handler``'s signature.

    Returns a dict of the form::

        {
            "type": "object",
            "properties": {<param>: {<schema>}},
            "required": [<param>, ...],
        }

    Maps Python types per ``design.md`` D2.
    """
    try:
        sig = inspect.signature(handler, eval_str=True)
    except (NameError, TypeError):  # pragma: no cover - fallback for forward refs
        sig = inspect.signature(handler)
    properties: dict[str, dict] = {}
    required: list[str] = []

    for pname, param in sig.parameters.items():
        # Skip *args / **kwargs / self / cls.
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        if pname in ("self", "cls"):
            continue

        annotation = param.annotation
        is_opt, _inner = _is_optional(annotation)
        schema, description, _extras = _annotation_to_schema(annotation)

        if description is None and annotation is inspect.Parameter.empty:
            description = ""

        if description is not None:
            schema = {**schema, "description": description}

        # Optional[T] (no default) -> not required, default null.
        # Default present (param.default != empty) -> not required.
        has_default = param.default is not inspect.Parameter.empty
        if has_default:
            try:
                # Only attach default when it is JSON-serialisable scalar.
                if isinstance(param.default, (int, float, str, bool, type(None))):
                    schema = {**schema, "default": param.default}
            except Exception:  # pragma: no cover - defensive
                pass

        if is_opt and not has_default:
            schema = {**schema, "default": None}

        if not is_opt and not has_default:
            required.append(pname)

        properties[pname] = schema

    schema_doc: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema_doc["required"] = required
    return schema_doc


def apply_param_overrides(
    reflected: dict,
    params: dict,
    plugin: str,
    tool: str,
) -> dict:
    """Merge manifest ``params`` over the reflected schema field-by-field.

    Raises :class:`PluginManifestError` if ``params`` declares a key not
    present in the reflected ``properties``.
    """
    if not params:
        return reflected

    out = {**reflected, "properties": {**reflected.get("properties", {})}}
    required = list(out.get("required", []))

    for pname, override in params.items():
        if pname not in out["properties"]:
            raise PluginManifestError(
                f"params declares unknown parameter {pname!r} for "
                f"tool {plugin}__{tool}",
                source=f"{plugin}.{tool}",
            )
        if not isinstance(override, dict):
            raise PluginManifestError(
                f"params[{pname!r}] must be a dict (got "
                f"{type(override).__name__})",
                source=f"{plugin}.{tool}",
            )

        merged = {**out["properties"][pname]}
        for k, v in override.items():
            if k == "required":
                if v and pname not in required:
                    required.append(pname)
                elif not v and pname in required:
                    required.remove(pname)
                continue
            if k == "type":
                merged["type"] = v
            else:
                merged[k] = v
        out["properties"][pname] = merged

    if required:
        out["required"] = required
    elif "required" in out:
        del out["required"]
    return out


# ---------------------------------------------------------------------------
# Wrapper factory: timeout + undo + error contract
# ---------------------------------------------------------------------------


_TIMEOUT_POOL: Optional[concurrent.futures.ThreadPoolExecutor] = None


def _get_pool() -> concurrent.futures.ThreadPoolExecutor:
    global _TIMEOUT_POOL
    if _TIMEOUT_POOL is None:
        _TIMEOUT_POOL = concurrent.futures.ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="plugin-handler"
        )
    return _TIMEOUT_POOL


def _create_undo_point_safe(label: str, log: logging.Logger) -> None:
    """Best-effort ``ida_undo.create_undo_point``; never raises."""
    try:
        import ida_undo  # noqa: WPS433 (lazy on purpose)

        ida_undo.create_undo_point(label, label)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("create_undo_point(%s) failed: %r", label, exc)


def make_plugin_tool_wrapper(
    full_name: str,
    handler: Callable[..., Any],
    *,
    needs_undo: bool,
    timeout: int,
    log: Optional[logging.Logger] = None,
) -> Callable[[dict], Awaitable[str | Any]]:
    """Wrap a handler with timeout, optional undo, and the error contract.

    Returns an async coroutine accepting a single ``arguments`` dict (the
    MCP shape). Sync handlers run on a shared thread pool; async handlers
    run with ``asyncio.wait_for``.
    """
    log = log or _server_logger
    is_async = inspect.iscoroutinefunction(handler)

    async def _wrapped(arguments: dict) -> Any:
        if needs_undo:
            _create_undo_point_safe(full_name, log)

        try:
            if is_async:
                return await asyncio.wait_for(
                    handler(**(arguments or {})),
                    timeout=timeout,
                )
            else:
                loop = asyncio.get_event_loop()
                pool = _get_pool()
                fut = loop.run_in_executor(
                    pool,
                    lambda: handler(**(arguments or {})),
                )
                return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            log.warning(
                "plugin tool %s timed out after %ds", full_name, timeout
            )
            return "error: timeout"
        except ToolError as exc:
            return f"error: {exc.code}: {exc.message}"
        except Exception as exc:
            log.error(
                "plugin tool %s raised %s: %s\n%s",
                full_name,
                type(exc).__name__,
                exc,
                traceback.format_exc(),
            )
            return f"error: {type(exc).__name__}: {exc}"

    _wrapped.__name__ = f"plugin_wrapped_{full_name}"
    return _wrapped
