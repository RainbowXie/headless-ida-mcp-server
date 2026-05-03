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
from functools import wraps
from typing import Annotated, Optional

from mcp.server import FastMCP
from mcp.server.fastmcp.prompts import base

from headless_ida_mcp_server.helper import IDA
from headless_ida_mcp_server.logger import logger
from headless_ida_mcp_server import PORT, TRANSPORT
from headless_ida_mcp_server.api_python import py_eval as _py_eval

_MCP_INSTRUCTIONS = """\
This server analyzes binaries via IDA Pro headlessly. The MCP layer exposes
84 tools and 11 resources backed by an in-process idalib SDK session.

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
5. Use third-party tools. If `IDA_MCP_PLUGIN_PATHS` was set on server
   start, the listed plugin checkout roots are on `sys.path`. Call
   `py_eval(code="from <plugin> import api; ...")` to drive them, or to
   run any other Python (idaapi, idautils, idc, ...) inside the server.

Conventions:
- Failures return strings starting with `error: ...`. The MCP transport is
  never broken by exceptions.
- `dbg_*` tools require a live debugger session that idalib does not host
  by default. They return `error: Debugger not running` unless `dbg_start`
  has been driven first; rely on static analysis tools instead.
- Tool names and signatures track upstream `mrexodia/ida-pro-mcp`. See
  README.md and docs/agent-quickstart.md for the full reference.
"""

mcp = FastMCP("IDA MCP Server", port=PORT, instructions=_MCP_INSTRUCTIONS)
ida = None  # legacy `helper.IDA` handle, retained for v2 lifecycle tools

# Register the vendored py_eval tool. FastMCP reads the function's type
# annotations and docstring to build the MCP tool schema. We expose it under
# its native name so agents see `py_eval` in `list_tools`. See
# `api_python.py` for the implementation and the "unsafe" disclaimer.
mcp.tool()(_py_eval)


# ----------------------------------------------------------------------------
# Lifecycle tools (idalib-specific, not in upstream)
# ----------------------------------------------------------------------------
@mcp.tool()
def set_binary_path(path: Annotated[str, "Path to the binary file"]):
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


@mcp.tool()
def unset():
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
      - the FastMCP-visible signature mirrors the original (annotations and
        docstring preserved via `functools.wraps` + `__signature__`).
    """
    sig = inspect.signature(fn)

    @wraps(fn)
    def wrapper(*args, **kwargs):
        guard = _binary_required_guard(name)
        if guard is not None:
            return guard
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
# defines a tool with the same name, prefer upstream (matches spec rule
# "v2 baseline overridden by upstream when names collide"). FastMCP's
# `add_tool` overwrites the previous binding, so we just need to skip our
# explicit reservations rather than blocking upstream registration.
_RESERVED_BY_FORK: set[str] = {"set_binary_path", "unset", "py_eval"}

for _name, _fn in MCP_TOOLS:
    if _name in _RESERVED_BY_FORK:
        # Upstream may also define `py_eval` (it does in api_python.py, which
        # we vendor in `..api_python` not under `ida_mcp/`). Skipping here
        # avoids a name clash with the explicit `mcp.tool()(_py_eval)` above.
        # `set_binary_path` / `unset` are fork-only.
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
    if _uri in _REGISTERED_RESOURCES:
        logger.warning("resource %s already registered; overriding", _uri)
    mcp.resource(_uri)(_make_resource_wrapper(_uri, _fn))
    _REGISTERED_RESOURCES.add(_uri)

logger.info(
    "registered %d upstream tools and %d resources (plus 3 fork-only tools: "
    "set_binary_path, unset, py_eval)",
    len(_REGISTERED_TOOLS),
    len(_REGISTERED_RESOURCES),
)


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
