# -*- coding: utf-8 -*-
"""FastMCP server entry point.

Three layers of tools are registered onto a single FastMCP instance:

1. **Lifecycle tools** (`set_binary_path`, `unset`) — idalib-specific helpers
   that open / close the IDB. Defined here, not in upstream `ida-pro-mcp`.
2. **`py_eval`** — vendored from upstream's `api_python.py` (added by the
   `add-py-eval-tool` change). Registered explicitly because it lives at
   `..api_python` and not under the vendored `ida_mcp/` subpackage.
3. **Vendored upstream tools and resources** — the `ida_mcp/` subpackage is
   imported once; importing each `api_*.py` populates `MCP_TOOLS` /
   `MCP_RESOURCES` registries via the `@tool` / `@resource` decorators in
   `ida_mcp/rpc.py`. We walk those registries and bind each entry onto the
   FastMCP instance with a thin error-handling wrapper.

The error wrapper catches `IDAError` (and any other exception) and returns a
short string starting with `"error: "`, instead of letting the exception
escape into FastMCP's transport layer (which would drop the connection).
This satisfies the spec's "errors do not break MCP" requirement.

v2 baseline tools -> upstream equivalents
-----------------------------------------
The v2 baseline (cnitlrt fork) had 19 hand-rolled MCP tools delegating to
`helper.IDA`. Upstream `mrexodia/ida-pro-mcp` covers every v2 use case with
richer typed batch tools, so we replace v2 wholesale rather than maintain
two implementations. Mapping (also captured in the change spec):

  set_binary_path / unset            -> fork-only (idalib lifecycle)
  get_function / get_function_by_name / get_function_by_address
                                     -> lookup_funcs(queries=[...])
  get_current_address / get_current_function
                                     -> resource ida://cursor
  convert_number                     -> int_convert(inputs=[...])
  list_functions                     -> list_funcs(queries=[...])
  decompile_function                 -> decompile(addr)
  disassemble_function               -> disasm(addr)
  get_xrefs_to                       -> xrefs_to(addrs=[...])
  get_entry_points                   -> resource ida://idb/entrypoints
  set_decompiler_comment / set_disassembly_comment
                                     -> set_comments / append_comments
  refresh_decompiler_widget          -> dropped (GUI-only; meaningless under idalib)
  rename_local_variable / rename_function
                                     -> rename(batch={local: [...], func: [...]})
  set_function_prototype             -> set_type(edits=[{kind:'function', signature: ...}])
  save_idb_file                      -> idb_save(save_path)

Result: v2-named tools (other than `set_binary_path`, `unset`, `py_eval`)
are no longer registered. Agents that hard-coded v2 names need to migrate
to upstream names. The migration is unavoidable to stay in sync with the
upstream tool surface, which is the whole point of this change.
"""
from __future__ import annotations

import inspect
import logging
import os
from functools import wraps
from typing import Annotated, Any, Optional, Sequence

from mcp.server import FastMCP
from mcp.server.fastmcp.prompts import base
from mcp.server.lowlevel.server import NotificationOptions
from mcp.types import Tool as MCPTool

from headless_ida_mcp_server.helper import IDA
from headless_ida_mcp_server.logger import logger
from headless_ida_mcp_server import PORT, TRANSPORT
from headless_ida_mcp_server.api_python import py_eval as _py_eval
from headless_ida_mcp_server.tags import (
    META_TOOL_ALLOWLIST,
    TOOL_TAGS,
    builtin_tag_names,
    is_excluded,
    parse_exclude_patterns,
    register_tags,
    tags_for,
)
from headless_ida_mcp_server.plugins import (
    PluginManifestError,
    PluginRecord,
    PluginToolRecord,
    apply_param_overrides,
    make_plugin_tool_wrapper,
    reflect_signature,
    validate_plugin_block,
    validate_tool_entry,
)
from headless_ida_mcp_server.plugins import discovery as _plugin_discovery
from headless_ida_mcp_server.plugins import session_state as _session_state

# Capability filter, populated once at module load from
# IDA_MCP_EXCLUDE_TAGS (set by `__main__.py` from --exclude-tags). Empty
# pattern list = no filtering. The filter applies uniformly to every
# tool / resource registration site below: vendored upstream loop,
# resource loop, and the four fork-only tools.
_EXCLUDE_PATTERNS: list[str] = parse_exclude_patterns(
    os.environ.get("IDA_MCP_EXCLUDE_TAGS", "")
)

_MCP_INSTRUCTIONS = """\
This server analyzes binaries via IDA Pro headlessly. The MCP layer exposes
up to 85 tools and 11 resources backed by an in-process idalib SDK session
(some may be filtered out by the operator's capability tag config — see
the conventions block below).

Workflow primer:
1. Load a binary. If `IDB_PATH` was not set on server start, call
   `set_binary_path(path="...")` first. Any tool that touches IDA state
   returns `error: Binary path not set ...` until you do.
2. Navigate. `list_funcs` / `lookup_funcs` / `xrefs_to` / `imports` /
   `survey_binary` are the cheapest first-look tools. Use the resources
   `ida://idb/metadata` / `ida://idb/segments` / `ida://idb/entrypoints`
   for whole-IDB context.
3. Read code. `decompile` returns hex-rays pseudocode; `disasm` returns
   assembly; `basic_blocks` / `callees` / `callgraph` give structure.
4. Annotate and persist. `rename` / `set_comments` / `append_comments` /
   `set_type` / `define_func` mutate the IDB; `idb_save` writes them out.
   If you mis-type or rename the wrong thing, call `undo()` (or
   `undo(steps=N)` to walk further back) — every IDB-mutating tool
   auto-creates an `ida_undo` undo point before running, so single-step
   recovery works without reloading the IDB.
5. Discover and use plugin tools. Plugins ship typed MCP tools via
   `mcp_manifest.py` (loaded from pip entry points, `~/.idapro/plugins/*`,
   or `IDA_MCP_PLUGIN_PATHS`). Workflow: call `plugins()` to enumerate
   loaded plugins, `plugin_tools(name="<plugin>")` to peek at a plugin's
   tools without changing your session, then `enable_plugin(name="<plugin>")`
   to opt this session in (the server emits `notifications/tools/list_changed`
   so a refetched `list_tools` shows the plugin's tools). Tools are
   prefixed `<plugin>__<short>`. Call `disable_plugin(name="<plugin>")`
   to drop them again. Plugin enable state is per-session and ephemeral
   across reconnects. For ad-hoc Python (idaapi, idautils, idc, ...) or
   plugins you choose to drive without a manifest, `py_eval(code=...)`
   still works.

Conventions:
- Failures return strings starting with `error: ...`. The MCP transport is
  never broken by exceptions.
- Every tool is tagged `kind:read` / `kind:write` / `kind:unsafe`. Writes
  are auto-undo-wrapped; unsafe ones (`patch` / `patch_asm` / `undefine` /
  `py_eval` / `unset`) are NOT — `undo()` cannot recover them. The
  operator may have dropped a tier at startup via `--exclude-tags`
  (e.g. read-only deployment); check what's actually available with the
  MCP `list_tools` request rather than assuming the full surface.
- `dbg_*` tools require a live debugger session that idalib does not host
  by default. They return `error: Debugger not running` unless `dbg_start`
  has been driven first; rely on static analysis tools instead.
- Tool names and signatures track upstream `mrexodia/ida-pro-mcp`. See
  README.md and docs/agent-quickstart.md (§11 covers capability tags and
  the `undo()` contract in detail) for the full reference.
- If `import <plugin>` fails after `IDA_MCP_PLUGIN_PATHS` was set, call
  `py_eval(code="import sys; sys.path[:5]")` to verify the path was
  injected at the front of `sys.path`.
"""

# ----------------------------------------------------------------------------
# Plugin private dispatch table
#
# Plugin tools live in their own dispatch table (per design D13). FastMCP's
# ``_tool_manager`` keeps fork built-ins + 4 meta tools; plugin tools never
# enter it. The session-aware FastMCP subclass below filters ``list_tools``
# and routes ``call_tool`` against the calling session's enabled plugins.
# ----------------------------------------------------------------------------
_PLUGIN_DISPATCH: dict[str, PluginToolRecord] = {}
_PLUGIN_REGISTRY: dict[str, PluginRecord] = {}


def _plugin_to_mcp_tool(rec: PluginToolRecord) -> MCPTool:
    """Convert a :class:`PluginToolRecord` into an ``mcp.types.Tool``."""
    return MCPTool(
        name=rec.full_name,
        description=rec.description,
        inputSchema=rec.input_schema,
    )


class SessionAwareFastMCP(FastMCP):
    """FastMCP subclass that filters tool surface per session.

    See design D13 / D18 / D19. Plugin tools are stored in
    :data:`_PLUGIN_DISPATCH`; built-in + meta tools live on
    ``self._tool_manager``. ``list_tools`` returns built-ins always plus
    plugin tools whose plugin the calling session has enabled.
    ``call_tool`` routes plugin-tool names through the dispatch table
    after a session-membership / ``mcp:false`` check.
    """

    async def list_tools(self) -> list[MCPTool]:
        builtin = await super().list_tools()
        try:
            session = self._mcp_server.request_context.session
        except (LookupError, AttributeError):  # pragma: no cover - defensive
            session = None

        if session is None:
            return list(builtin)

        enabled = _session_state.get_enabled(session)
        plugin_tools_visible: list[MCPTool] = []
        for rec in _PLUGIN_DISPATCH.values():
            if not rec.mcp_visible:
                continue
            if rec.plugin_name not in enabled:
                continue
            plugin_tools_visible.append(_plugin_to_mcp_tool(rec))
        return list(builtin) + plugin_tools_visible

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> Sequence[Any] | dict[str, Any]:
        from mcp.types import TextContent

        rec = _PLUGIN_DISPATCH.get(name)
        if rec is None:
            return await super().call_tool(name, arguments)
        try:
            session = self._mcp_server.request_context.session
        except (LookupError, AttributeError):  # pragma: no cover - defensive
            session = None

        if rec.mcp_visible and session is not None:
            enabled = _session_state.get_enabled(session)
            if rec.plugin_name not in enabled:
                return [
                    TextContent(
                        type="text",
                        text=(
                            f"error: tool {name!r} not enabled. "
                            f"Call enable_plugin({rec.plugin_name!r}) first."
                        ),
                    )
                ]
        # mcp:false bypasses the membership check (design D10).
        result = await rec.wrapped(arguments or {})
        # Wrap plain string results so the lowlevel handler does not
        # iterate them character-by-character into invalid ContentBlocks.
        if isinstance(result, str):
            return [TextContent(type="text", text=result)]
        return result

    async def run_stdio_async(self) -> None:  # type: ignore[override]
        # Per design D17: advertise tools.listChanged=True so MCP-compliant
        # clients refetch ``list_tools`` on every notification.
        # Per `transport-stdio-isolation`: when fd-redirect ran (i.e.
        # `_install_stdio_isolation_if_needed()` saved a JSON-RPC writer),
        # hand it to `stdio_server(stdout=...)` so JSON-RPC frames go
        # through the saved fd while `sys.stdout` (now aliased to stderr)
        # absorbs auto-loaded plugin print noise. Falls back to the
        # default behaviour when no redirect happened (SSE mode does not
        # call this method, but defensive: also covers tests / harnesses
        # that import `MCPServer` directly).
        import anyio
        from mcp.server.stdio import stdio_server
        from headless_ida_mcp_server import _get_jsonrpc_writer

        writer = _get_jsonrpc_writer()
        stdout_arg = anyio.wrap_file(writer) if writer is not None else None

        async with stdio_server(stdout=stdout_arg) as (read_stream, write_stream):
            await self._mcp_server.run(
                read_stream,
                write_stream,
                self._mcp_server.create_initialization_options(
                    notification_options=NotificationOptions(tools_changed=True),
                ),
            )

    async def run_sse_async(self, mount_path: str | None = None) -> None:  # type: ignore[override]
        # Mirror run_stdio_async so the SSE transport also advertises the
        # capability. We replicate the upstream wiring (uvicorn + Starlette)
        # rather than calling super(), which would use the default
        # NotificationOptions().
        import uvicorn

        starlette_app = self.sse_app(mount_path)
        config = uvicorn.Config(
            starlette_app,
            host=self.settings.host,
            port=self.settings.port,
            log_level=self.settings.log_level.lower(),
        )
        server = uvicorn.Server(config)
        await server.serve()


mcp = SessionAwareFastMCP(
    "IDA MCP Server", port=PORT, instructions=_MCP_INSTRUCTIONS
)
ida = None  # legacy `helper.IDA` handle, retained for v2 lifecycle tools


# Counter for fork-only tools dropped by the exclude filter. The vendored
# loop tracks its own counter; both feed into the final startup log line.
_excluded_fork_tools = 0


def _create_undo_point(action_name: str) -> None:
    """Best-effort ``ida_undo.create_undo_point`` invocation.

    Called by ``_make_tool_wrapper`` immediately before delegating to a
    ``kind:write`` upstream tool. Failures are logged at WARNING level
    but never re-raised: a broken undo subsystem must not block normal
    tool execution (best-effort semantics, per the spec).

    The label argument is set to the action name as well; future work
    may switch the label to a more human-readable value (e.g. the
    inbound MCP call id) once that signal is available at this layer.
    """
    try:
        # Lazy import: ``ida_undo`` is part of the IDA SDK and is only
        # importable after idalib bootstrap. Module-level import would
        # fail when the test suite imports server.py without idalib (and
        # would also break the "import order" contract documented in
        # __init__.py.).
        import ida_undo  # noqa: WPS433  (lazy on purpose)

        ida_undo.create_undo_point(action_name, action_name)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "create_undo_point(%s) failed: %r", action_name, exc
        )


# ----------------------------------------------------------------------------
# Lifecycle tools (idalib-specific, not in upstream)
# ----------------------------------------------------------------------------
def _set_binary_path_impl(path: Annotated[str, "Path to the binary file"]):
    """Set the path to the binary file (opens the IDB via idalib)."""
    global ida
    if ida is not None:
        return "Binary path already set, call unset first"

    ida = IDA(path)
    if ida.open is True:
        # Mark the package-level idb-open flag so vendored upstream tools
        # (which call `idaapi.*` / `idc.*` directly, not via `helper.IDA`)
        # pass the `_binary_required_guard` check.
        import headless_ida_mcp_server as _pkg

        _pkg._idb_open = True
        # The vendored `api_core` keeps a strings cache keyed off the current
        # IDB. Invalidate it after a fresh open so the first call to
        # `list_strings` rebuilds.
        try:
            from headless_ida_mcp_server.ida_mcp import api_core as _api_core

            _api_core.invalidate_strings_cache()
        except Exception:  # pragma: no cover - best effort
            pass
        return "Binary path set"
    else:
        ida = None
        return "Failed to set binary path"


# Preserve the original public name on the function object so MCP tool
# introspection still sees ``set_binary_path``.
_set_binary_path_impl.__name__ = "set_binary_path"


def _unset_impl():
    """Close the IDA database and unset the binary path."""
    global ida
    if ida is None:
        return "error: Binary path not set"
    ida.clean_up()
    ida = None
    import headless_ida_mcp_server as _pkg

    _pkg._idb_open = False
    try:
        from headless_ida_mcp_server.ida_mcp import api_core as _api_core

        _api_core.invalidate_strings_cache()
    except Exception:  # pragma: no cover
        pass
    return "Binary path unset"


_unset_impl.__name__ = "unset"


# Each fork-only tool participates in the same exclude filter as
# vendored tools, so a read-only deployment (--exclude-tags
# 'kind:write,kind:unsafe') can drop set_binary_path / unset / py_eval
# wholesale. The undo tool is registered separately further below
# because it needs to live AFTER the vendored loop (so its name beats
# any future upstream collision in `_RESERVED_BY_FORK`).
if not is_excluded("set_binary_path", _EXCLUDE_PATTERNS):
    mcp.add_tool(_set_binary_path_impl, structured_output=False)
else:
    _excluded_fork_tools += 1

if not is_excluded("unset", _EXCLUDE_PATTERNS):
    mcp.add_tool(_unset_impl, structured_output=False)
else:
    _excluded_fork_tools += 1

# Register the vendored py_eval tool. FastMCP reads the function's type
# annotations and docstring to build the MCP tool schema. We expose it
# under its native name so agents see `py_eval` in `list_tools`. See
# `api_python.py` for the implementation and the "unsafe" disclaimer.
if not is_excluded("py_eval", _EXCLUDE_PATTERNS):
    mcp.tool()(_py_eval)
else:
    _excluded_fork_tools += 1


# ----------------------------------------------------------------------------
# Vendored upstream tools + resources
#
# Importing the `ida_mcp` subpackage triggers each `api_*.py` module load,
# which in turn calls the `@tool` / `@resource` decorators in our `rpc.py`
# stub. That populates `MCP_TOOLS` / `MCP_RESOURCES` (lists of `(name, fn)` /
# `(uri, fn)` tuples). We then wrap each entry with a structured-error layer
# and register it on the FastMCP instance.
# ----------------------------------------------------------------------------
from headless_ida_mcp_server import ida_mcp  # noqa: E402  (must run after py_eval reg)
from headless_ida_mcp_server.ida_mcp import IDAError  # noqa: E402
from headless_ida_mcp_server.ida_mcp.rpc import (  # noqa: E402
    MCP_TOOLS,
    MCP_RESOURCES,
)


def _binary_required_guard(name: str):
    """Tools that touch IDA state require an open IDB. Two paths can open one:

      * `_bootstrap_idalib()` auto-opens the IDB pointed to by `IDB_PATH`
        before the server starts (sets `headless_ida_mcp_server._idb_open`),
      * the `set_binary_path` MCP tool opens it on demand (sets `ida` to the
        legacy `helper.IDA` instance).

    Upstream tools call `idaapi.*` / `idc.*` directly and only need ANY open
    IDB; they don't care which path opened it. So the guard accepts either
    indicator.
    """
    import headless_ida_mcp_server as _pkg

    if ida is None and not getattr(_pkg, "_idb_open", False):
        return f"error: Binary path not set (call set_binary_path first; tool={name!r})"
    return None


def _make_tool_wrapper(name: str, fn):
    """Wrap a vendored upstream tool so:
      - exceptions become structured error strings (no transport drop),
      - `set_binary_path` lifecycle is checked,
      - if the tool is tagged ``kind:write``, an ``ida_undo`` undo point
        is created BEFORE the underlying function runs (so a subsequent
        ``undo()`` call rolls back the side effect),
      - the FastMCP-visible signature mirrors the original (annotations
        and docstring preserved via `functools.wraps` + `__signature__`).
    """
    sig = inspect.signature(fn)
    # Compute once at registration time. ``kind:read`` and
    # ``kind:unsafe`` deliberately skip the auto undo point: read tools
    # do not need it, unsafe tools cannot be recovered by ``ida_undo``
    # so creating one would just be misleading noise.
    needs_undo = "kind:write" in tags_for(name)

    @wraps(fn)
    def wrapper(*args, **kwargs):
        guard = _binary_required_guard(name)
        if guard is not None:
            return guard
        if needs_undo:
            # Best-effort: failures are logged but never block the
            # tool call (the wrapper catches its own exceptions inside
            # _create_undo_point).
            _create_undo_point(name)
        try:
            return fn(*args, **kwargs)
        except IDAError as exc:
            return f"error: {exc.message}"
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("tool %s raised: %s", name, exc)
            return f"error: {type(exc).__name__}: {exc}"

    # Preserve the original signature so FastMCP's schema generator (Pydantic)
    # sees the typed parameters / return annotation. functools.wraps copies
    # __wrapped__ but not __signature__ in all Python versions.
    wrapper.__signature__ = sig  # type: ignore[attr-defined]
    return wrapper


def _make_resource_wrapper(uri: str, fn):
    """Resource wrapper: same error contract as tools. Resources without
    template params (`{name}`) take no args; templated resources receive
    each placeholder as a kwarg from FastMCP's URI matcher.
    """
    sig = inspect.signature(fn)

    @wraps(fn)
    def wrapper(*args, **kwargs):
        guard = _binary_required_guard(uri)
        if guard is not None:
            return {"error": guard}
        try:
            return fn(*args, **kwargs)
        except IDAError as exc:
            return {"error": exc.message}
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("resource %s raised: %s", uri, exc)
            return {"error": f"{type(exc).__name__}: {exc}"}

    wrapper.__signature__ = sig  # type: ignore[attr-defined]
    return wrapper


_REGISTERED_TOOLS: set[str] = set()
_REGISTERED_RESOURCES: set[str] = set()

# Names already bound by the lifecycle / py_eval block above. If upstream
# defines a tool with the same name, prefer the fork (matches spec rule:
# fork-only tools win over upstream collisions for `set_binary_path` /
# `unset` / `py_eval`, and `undo` is reserved against future upstream
# additions of the same name). FastMCP's `add_tool` overwrites the
# previous binding, so we just need to skip our explicit reservations
# during the vendored loop rather than blocking upstream registration.
_RESERVED_BY_FORK: set[str] = {"set_binary_path", "unset", "py_eval", "undo"}

# Counters for the startup summary log. The fork-only counter is
# initialised by the lifecycle / py_eval block above; we still need to
# add the undo decision later when its registration site is reached.
_excluded_tools = 0
_excluded_resources = 0

for _name, _fn in MCP_TOOLS:
    if _name in _RESERVED_BY_FORK:
        # Upstream may also define `py_eval` (it does in api_python.py, which
        # we vendor in `..api_python` not under `ida_mcp/`). Skipping here
        # avoids a name clash with the explicit fork-only registrations.
        # `set_binary_path` / `unset` / `undo` are fork-only entries that
        # stay reserved even if upstream later picks the same name.
        continue
    if is_excluded(_name, _EXCLUDE_PATTERNS):
        # The capability filter dropped this tool. Counted separately
        # so the startup log can report `tools (N excluded)` accurately.
        _excluded_tools += 1
        continue
    if _name in _REGISTERED_TOOLS:
        # Same-name across two upstream files -> last one wins (upstream
        # behaviour). Mirror it explicitly so the log line is clear.
        logger.warning("tool %s already registered; overriding", _name)
    # `structured_output=False`: disable FastMCP's strict-schema generation
    # for the return type. Many upstream tools return TypedDicts whose
    # fields reference other TypedDicts defined later in the same module
    # (legitimate forward references inside upstream's framework). FastMCP's
    # Pydantic schema generator chokes on those forward refs at registration
    # time. Returning unstructured content is the same contract the v2
    # baseline tools used (they returned dicts/lists without schemas), and
    # is more tolerant of upstream churn between syncs. Tools still serialise
    # via `mcp.types.TextContent` so agents see the JSON.
    mcp.add_tool(_make_tool_wrapper(_name, _fn), structured_output=False)
    _REGISTERED_TOOLS.add(_name)

for _uri, _fn in MCP_RESOURCES:
    if is_excluded(_uri, _EXCLUDE_PATTERNS):
        # Resource URIs go through the same filter (default kind:read
        # via tags_for if not enumerated in TOOL_TAGS) so a read-only
        # mode applied symmetrically to tools and resources behaves
        # consistently.
        _excluded_resources += 1
        continue
    if _uri in _REGISTERED_RESOURCES:
        logger.warning("resource %s already registered; overriding", _uri)
    mcp.resource(_uri)(_make_resource_wrapper(_uri, _fn))
    _REGISTERED_RESOURCES.add(_uri)


# ----------------------------------------------------------------------------
# Untagged-tool detection. After the vendored loop, every tool name that
# the server actually saw should be enumerated in TOOL_TAGS; any name in
# `MCP_TOOLS` (plus the four fork-only names) that is missing from
# `TOOL_TAGS.keys()` falls through to the conservative `kind:read`
# default and is reported here so maintainers can update tags.py after
# an upstream sync.
# ----------------------------------------------------------------------------
_FORK_ONLY_NAMES: set[str] = {"set_binary_path", "unset", "py_eval", "undo"}
_observed_tool_names = {name for name, _ in MCP_TOOLS} | _FORK_ONLY_NAMES
_untagged = sorted(_observed_tool_names - set(TOOL_TAGS.keys()))
if _untagged:
    logger.warning(
        "untagged tools default to kind:read: %s "
        "(update src/headless_ida_mcp_server/tags.py after an upstream sync)",
        _untagged,
    )


# Total registered tool count includes the fork-only block (which has
# its own excluded counter). Fork-only contributes set_binary_path /
# unset / py_eval (registered above) and `undo` (registered below).
_undo_excluded = 1 if is_excluded("undo", _EXCLUDE_PATTERNS) else 0


def _emit_registration_log() -> None:
    """Single-source startup summary including the undo + plugin counts.

    Called from the bottom of this module after the ``undo`` tool and
    plugin registration have both completed.
    """
    total_tools = len(_REGISTERED_TOOLS) + (
        # Fork-only contribution: 4 candidate slots minus what the
        # filter dropped (counted in `_excluded_fork_tools` for the
        # three lifecycle entries plus `_undo_excluded` for `undo`).
        4 - _excluded_fork_tools - _undo_excluded
    )
    total_excluded = _excluded_tools + _excluded_fork_tools + _undo_excluded
    plugin_tool_count = len(_PLUGIN_DISPATCH)
    plugin_visible = sum(1 for r in _PLUGIN_DISPATCH.values() if r.mcp_visible)
    plugin_count = len(_PLUGIN_REGISTRY)
    plugin_excluded = _excluded_plugin_tools
    logger.info(
        "registered %d tools (%d excluded by tag filter), %d resources "
        "(%d excluded), %d plugin tools across %d plugins "
        "(%d plugin tools excluded; %d session-visible candidates); "
        "session-default enabled = empty (4 fork-only tools: "
        "set_binary_path, unset, py_eval, undo; 4 meta tools: "
        "plugins, plugin_tools, enable_plugin, disable_plugin)",
        total_tools,
        total_excluded,
        len(_REGISTERED_RESOURCES),
        _excluded_resources,
        plugin_tool_count,
        plugin_count,
        plugin_excluded,
        plugin_visible,
    )


# ----------------------------------------------------------------------------
# Fork-only `undo` tool. Registered AFTER the vendored loop so an
# upstream-introduced collision is shadowed by the fork (the loop already
# skips `_RESERVED_BY_FORK`, but registration order also matters because
# FastMCP's `add_tool` is "last write wins"). Tagged kind:read so the
# wrapper does NOT recursively create undo points around the undo call
# itself, and so it remains usable in read-only deployments (an agent
# can still roll back earlier writes after the writer surface has been
# excluded).
# ----------------------------------------------------------------------------
def _undo_impl(steps: int = 1) -> dict:
    """Roll back the last ``steps`` IDB-modifying actions.

    Each ``kind:write`` tool call is wrapped in its own ``ida_undo``
    undo point, so by default ``undo()`` (steps=1) reverts exactly one
    prior tool call. Use ``undo(steps=N)`` to walk further back. The
    function never raises into the MCP transport: failures are reported
    via the ``error`` field of the returned dict, mirroring the rest of
    the fork's tool surface.

    Returns a dict with:

    * ``label_before`` -- ``ida_undo.get_undo_action_label()`` value at
      entry (the human-readable label of the action about to be undone).
    * ``steps_requested`` -- the integer the caller passed in.
    * ``steps_executed`` -- the number of successful
      ``perform_undo()`` calls (<= ``steps_requested``).
    * ``error`` -- a string describing why the loop stopped early, or
      ``None`` if every step succeeded.
    """
    guard = _binary_required_guard("undo")
    if guard is not None:
        return guard
    # Lazy import: see the docstring on _create_undo_point. Importing
    # ida_undo at module top would break the test suite's ability to
    # import server.py without idalib.
    try:
        import ida_undo  # noqa: WPS433
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "label_before": None,
            "steps_requested": steps,
            "steps_executed": 0,
            "error": f"ida_undo unavailable: {exc!r}",
        }

    try:
        label_before = ida_undo.get_undo_action_label()
    except Exception as exc:  # pragma: no cover - defensive
        # Older idalib builds may not expose get_undo_action_label.
        # Treat as advisory.
        logger.warning("get_undo_action_label() failed: %r", exc)
        label_before = None

    success_count = 0
    last_error: Optional[str] = None
    # ``max(1, steps)`` guards against negative / zero requests — the
    # spec scenario "single-step undo" expects steps_executed == 1 in
    # the normal case, never < 0.
    for _ in range(max(1, steps)):
        try:
            ok = bool(ida_undo.perform_undo())
        except Exception as exc:  # pragma: no cover - defensive
            last_error = f"perform_undo raised {exc!r}"
            break
        if ok:
            success_count += 1
        else:
            last_error = (
                "perform_undo returned False (no more undo points?)"
            )
            break

    return {
        "label_before": label_before,
        "steps_requested": steps,
        "steps_executed": success_count,
        "error": last_error,
    }


_undo_impl.__name__ = "undo"

if not is_excluded("undo", _EXCLUDE_PATTERNS):
    mcp.add_tool(_undo_impl, structured_output=False)
# Else `_undo_excluded` already accounts for the skipped slot in the
# startup log.


# ----------------------------------------------------------------------------
# Plugin registration pipeline
#
# Sequence:
#   1. Discover manifests (Path A entry points + Path B directory scan).
#   2. Validate PLUGIN block + each TOOLS entry against the schema.
#   3. Collision-check PLUGIN.name against fork built-ins and previously
#      registered plugin names.
#   4. For each tool: build prefixed name, inject ``ext::<plugin>::<short>``
#      tag, run is_excluded filter, register tags, build wrapper.
#   5. Insert into ``_PLUGIN_DISPATCH``; track in ``_PLUGIN_REGISTRY``.
# ----------------------------------------------------------------------------

_excluded_plugin_tools = 0
# Snapshot of every discovery-stage plugin (loaded + filtered) so the
# meta-tool error path can distinguish "unknown plugin" from
# "tag-excluded plugin". Populated by :func:`_register_plugins`.
_DISCOVERED_PLUGINS: list[Any] = []


def _register_plugins() -> None:
    """Discover plugins and load them into ``_PLUGIN_DISPATCH``.

    Aborts startup (raises) on schema violations or PLUGIN.name collisions.
    Manifest import failures emit a WARNING and skip (handled inside
    :func:`headless_ida_mcp_server.plugins.discovery.discover_all`).
    """
    global _excluded_plugin_tools, _DISCOVERED_PLUGINS

    discovered = _plugin_discovery.discover_all()
    _DISCOVERED_PLUGINS = list(discovered)
    seen_names: dict[str, str] = {}  # name -> source

    for entry in discovered:
        # ``_read_plugin_name`` already gave us a hint; let validate
        # canonicalise.
        plugin_block_raw = getattr(entry.module, "PLUGIN", None)
        if not isinstance(plugin_block_raw, dict):
            raise PluginManifestError(
                "manifest must export a PLUGIN dict at module top level",
                entry.source,
            )
        try:
            plugin_block = validate_plugin_block(plugin_block_raw, entry.source)
        except PluginManifestError:
            raise

        plugin_name = plugin_block["name"]
        # Collision: built-in
        if plugin_name in builtin_tag_names():
            raise RuntimeError(
                f"PLUGIN.name {plugin_name!r} (from {entry.source}) collides "
                f"with a fork built-in tool name. Rename the plugin."
            )
        # Collision: another plugin
        if plugin_name in seen_names:
            raise RuntimeError(
                f"PLUGIN.name {plugin_name!r} declared twice:\n"
                f"  - {seen_names[plugin_name]}\n"
                f"  - {entry.source}\n"
                f"Rename one of them."
            )
        seen_names[plugin_name] = entry.source

        tools_raw = getattr(entry.module, "TOOLS", None)
        if not isinstance(tools_raw, list):
            raise PluginManifestError(
                "manifest TOOLS must be a list", entry.source
            )

        record = PluginRecord(
            name=plugin_name,
            description=plugin_block["description"],
            version=plugin_block["version"],
            categories=plugin_block["categories"],
            source=entry.source,
        )

        short_seen: set[str] = set()
        for raw_tool in tools_raw:
            tool = validate_tool_entry(raw_tool, plugin_name, entry.source)
            short = tool["short_name"]
            if short in short_seen:
                raise PluginManifestError(
                    f"duplicate tool short name {short!r} in plugin {plugin_name!r}",
                    entry.source,
                )
            short_seen.add(short)

            full_name = tool["full_name"]
            ext_tag = f"ext::{plugin_name}::{short}"
            merged_tags = list(tool["tags"])
            if ext_tag not in merged_tags:
                merged_tags.append(ext_tag)

            # Apply tag filter BEFORE registering tags / dispatch so a
            # filtered tool is fully absent.
            #
            # ``is_excluded`` reads from TOOL_TAGS; we have not registered
            # yet. Use a synthetic check: any pattern that matches any of
            # ``merged_tags`` excludes the tool.
            if _plugin_tool_excluded(merged_tags):
                _excluded_plugin_tools += 1
                continue

            try:
                register_tags(full_name, merged_tags)
            except ValueError as exc:
                raise RuntimeError(
                    f"plugin tool {full_name!r} registration failed: {exc} "
                    f"(source: {entry.source})"
                )

            input_schema = reflect_signature(tool["handler"])
            input_schema = apply_param_overrides(
                input_schema, tool["params"], plugin_name, short
            )

            needs_undo = tool["kind_tag"] == "kind:write"
            wrapped = make_plugin_tool_wrapper(
                full_name=full_name,
                handler=tool["handler"],
                needs_undo=needs_undo,
                timeout=tool["timeout"],
                log=logger,
            )

            tool_record = PluginToolRecord(
                full_name=full_name,
                short_name=short,
                plugin_name=plugin_name,
                description=tool["description"],
                tags=merged_tags,
                timeout=tool["timeout"],
                mcp_visible=tool["mcp_visible"],
                input_schema=input_schema,
                returns=tool["returns"],
                handler=tool["handler"],
                wrapped=wrapped,
                source=entry.source,
            )
            _PLUGIN_DISPATCH[full_name] = tool_record
            record.tools.append(tool_record)

        # If every tool of this plugin was excluded, drop the plugin
        # itself from ``_PLUGIN_REGISTRY`` so ``enable_plugin(name)``
        # returns the "not loaded (excluded by tag filter)" error.
        if not record.tools:
            continue
        _PLUGIN_REGISTRY[plugin_name] = record

    _session_state.set_loaded_plugin_names(set(_PLUGIN_REGISTRY.keys()))


def _plugin_tool_excluded(tags: list[str]) -> bool:
    """Return True if any pattern in ``_EXCLUDE_PATTERNS`` matches any tag."""
    if not _EXCLUDE_PATTERNS:
        return False
    import fnmatch as _fn

    for pattern in _EXCLUDE_PATTERNS:
        for tag in tags:
            if _fn.fnmatchcase(tag, pattern):
                return True
    return False


# Run plugin discovery now. Failures abort startup with a clear message.
try:
    _register_plugins()
except (PluginManifestError, RuntimeError) as exc:
    logger.error("plugin registration failed: %s", exc)
    raise


# ----------------------------------------------------------------------------
# Four meta tools: plugins / plugin_tools / enable_plugin / disable_plugin
#
# Each carries ``kind:read`` plus the sentinel ``core::plugin-meta`` tag
# (per ``mcp-capability-tags`` ADDED requirement). The four names live in
# :data:`META_TOOL_ALLOWLIST` so ``is_excluded`` always returns False for
# them, regardless of the filter pattern in effect.
# ----------------------------------------------------------------------------


def _excluded_plugin_for_session(name: str) -> Optional[str]:
    """Return an error string if ``name`` is not loadable in any session.

    Returns ``None`` when the plugin is loaded and addressable; returns the
    ``error`` string for unknown / tag-excluded plugins.
    """
    if name in _PLUGIN_REGISTRY:
        return None
    # Loaded by discovery but every tool got tag-filtered? We do not keep
    # plugin metadata for fully-filtered plugins; treat as excluded if the
    # name appears anywhere in the original discovered set. Discovery
    # state is not retained, so we infer from the seen-name -> filtered
    # path by scanning ``_EXCLUDE_PATTERNS`` against the conventional
    # ``ext::<name>::*`` prefix pattern. This is a heuristic: if the
    # operator's filter would have matched every tool of plugin ``name``,
    # we surface the "excluded by tag filter" message; otherwise the
    # plugin truly does not exist.
    for plugin_name, _src in [(d.plugin_name, d.source) for d in _DISCOVERED_PLUGINS]:
        if plugin_name == name:
            return (
                f"plugin {name!r} not loaded (excluded by tag filter)"
            )
    return f"plugin {name!r} not found. Use plugins() to list."


def _plugins_impl() -> list[dict]:
    """Return one dict per loaded plugin with the calling session's enabled flag."""
    try:
        session = mcp._mcp_server.request_context.session
    except (LookupError, AttributeError):  # pragma: no cover - defensive
        session = None

    enabled = (
        _session_state.get_enabled(session) if session is not None else set()
    )
    out: list[dict] = []
    for plugin_name, rec in _PLUGIN_REGISTRY.items():
        visible_tool_count = sum(1 for t in rec.tools if t.mcp_visible)
        entry: dict[str, Any] = {
            "name": rec.name,
            "description": rec.description,
            "version": rec.version,
            "tool_count": visible_tool_count,
            "enabled": rec.name in enabled,
        }
        if rec.categories:
            entry["categories"] = list(rec.categories)
        out.append(entry)
    return out


_plugins_impl.__name__ = "plugins"


def _plugin_tools_impl(name: Annotated[str, "Plugin name (from plugins())"]) -> Any:
    """Return the per-tool list for ``name`` or the documented error string.

    Peek-only: never mutates session state, never emits notifications.
    """
    rec = _PLUGIN_REGISTRY.get(name)
    if rec is None:
        return f"error: plugin {name!r} not found. Use plugins() to list."

    out: list[dict] = []
    for tool in rec.tools:
        if not tool.mcp_visible:
            continue
        out.append(
            {
                "name": tool.full_name,
                "description": tool.description,
                "signature": tool.input_schema,
                "tags": list(tool.tags),
                "timeout": tool.timeout,
                "returns": tool.returns,
            }
        )
    return out


_plugin_tools_impl.__name__ = "plugin_tools"


async def _enable_plugin_impl(
    name: Annotated[str, "Plugin name to enable for the calling session"]
) -> dict:
    """Add ``name`` to the calling session's enabled set.

    Idempotent: returns ``already_enabled: True`` without re-emitting the
    notification when the plugin is already enabled.
    """
    try:
        session = mcp._mcp_server.request_context.session
    except (LookupError, AttributeError):  # pragma: no cover - defensive
        session = None

    rec = _PLUGIN_REGISTRY.get(name)
    if rec is None:
        err = _excluded_plugin_for_session(name)
        return {"ok": False, "plugin": name, "error": err}

    changed, _bag = _session_state.enable(session, name)
    if not changed:
        return {
            "ok": True,
            "plugin": name,
            "already_enabled": True,
            "added": [],
        }

    if session is not None:
        await _session_state.emit_tools_list_changed(session)

    added = [t.full_name for t in rec.tools if t.mcp_visible]
    return {"ok": True, "plugin": name, "added": added}


_enable_plugin_impl.__name__ = "enable_plugin"


async def _disable_plugin_impl(
    name: Annotated[str, "Plugin name to disable for the calling session"]
) -> dict:
    """Remove ``name`` from the calling session's enabled set.

    Idempotent: returns ``already_disabled: True`` without re-emitting the
    notification when the plugin was not enabled.
    """
    try:
        session = mcp._mcp_server.request_context.session
    except (LookupError, AttributeError):  # pragma: no cover - defensive
        session = None

    rec = _PLUGIN_REGISTRY.get(name)
    if rec is None:
        err = _excluded_plugin_for_session(name)
        return {"ok": False, "plugin": name, "error": err}

    changed, _bag = _session_state.disable(session, name)
    if not changed:
        return {
            "ok": True,
            "plugin": name,
            "already_disabled": True,
            "removed": [],
        }

    if session is not None:
        await _session_state.emit_tools_list_changed(session)

    removed = [t.full_name for t in rec.tools if t.mcp_visible]
    return {"ok": True, "plugin": name, "removed": removed}


_disable_plugin_impl.__name__ = "disable_plugin"


# Register the meta tools. They are immune to ``--exclude-tags`` per
# :data:`META_TOOL_ALLOWLIST`. Tag them ``kind:read`` plus the sentinel
# ``core::plugin-meta`` so operators can still filter them out by removing
# the entry from the allowlist if they want a customised surface.
TOOL_TAGS["plugins"] = ["kind:read", "core::plugin-meta"]
TOOL_TAGS["plugin_tools"] = ["kind:read", "core::plugin-meta"]
TOOL_TAGS["enable_plugin"] = ["kind:read", "core::plugin-meta"]
TOOL_TAGS["disable_plugin"] = ["kind:read", "core::plugin-meta"]

mcp.add_tool(_plugins_impl, structured_output=False)
mcp.add_tool(_plugin_tools_impl, structured_output=False)
mcp.add_tool(_enable_plugin_impl, structured_output=False)
mcp.add_tool(_disable_plugin_impl, structured_output=False)


# Emit the final summary line now that every tool registration site has
# committed. The function reads `_REGISTERED_TOOLS`, the filter counters,
# and the fork-only contribution (4 slots minus excludes) to produce a
# single authoritative number.
_emit_registration_log()


# ----------------------------------------------------------------------------
# Prompts (kept from v2)
# ----------------------------------------------------------------------------
@mcp.prompt()
def exploit_prompt():
    """Exploit prompt"""

    messages = [
        base.UserMessage("You are a helpful assistant that can help me with my exploit."),
        base.UserMessage("""
        You need to follow these steps to complete the exploit:
        1. Reverse analyze the binary file to locate vulnerabilities and analyze vulnerability types
            - Locate vulnerabilities: Use IDA Pro's analysis features to find vulnerability locations
            - Need to first gather binary file information, such as using checksec tool to check binary protection mechanisms
                - If you find NX protection is disabled, you can use ret2shellcode method to get a shell
        2. Choose appropriate exploitation method based on vulnerability type
            - For stack overflow vulnerabilities, analyze the overflow pattern and check if there are backdoor functions in the binary. If there are backdoor functions, modify the return address to point to the backdoor function address
            - If there are no backdoor functions, need to use ret2libc method. Don't assume the binary contains the gadgets you want - you need to use ROPgadget to find gadgets and combine them to construct the pop chain.
        """),
    ]
    return messages


def main():
    mcp.run(transport=TRANSPORT)


if __name__ == "__main__":
    main()
