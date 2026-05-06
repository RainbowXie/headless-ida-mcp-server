# Acknowledgments

This project builds upon the work of:
- Tools code adapted from [ida-pro-mcp](https://github.com/mrexodia/ida-pro-mcp) by mrexodia
- idalib rewrite based on [headless-ida-mcp-server](https://github.com/A1Lin/headless-ida-mcp-server) by A1Lin
- Lineage starts from [headless-ida-mcp-server](https://github.com/cnitlrt/headless-ida-mcp-server) by cnitlrt

# Headless IDA MCP Server

Run an IDA Pro analysis backend headlessly and expose it as an MCP server.
Useful when you want to drive IDA from a CLI / agent / CI rather than as an
interactive plugin.

> 中文版本见 [README_CN.md](./README_CN.md)。

## Features

- **Full upstream tool surface.** 85 MCP tools + 11 MCP resources (81
  vendored verbatim from
  [`mrexodia/ida-pro-mcp`](https://github.com/mrexodia/ida-pro-mcp) plus 4
  fork-only — `set_binary_path` / `unset` / `py_eval` / `undo`), covering
  analysis (`decompile` / `disasm` / `xrefs_to` / `callgraph` / ...),
  memory, types, structures, modify, stack, search, sigmaker, and
  debugger surfaces. Re-synced ad-hoc as upstream evolves.
- **Headless IDA fully driven by an agent.** Runs the IDA backend through
  the in-process `idalib` SDK — no `idat` subprocess, no per-call spawn
  overhead. One MCP connection drives a long agent session against one IDB.
  Renames / comments / type changes persist to the IDB via `idb_save`.
  When a client connects the server hands it an `instructions` field with
  a 5-step workflow primer, so an agent gets to its first useful tool call
  without reading any out-of-band docs.
- **Plugins expose typed MCP tools via `mcp_manifest.py`.** Drop a
  `mcp_manifest.py` next to your plugin source and the server reflects
  every handler into a typed MCP tool with `inspect.signature` schemas,
  capability tags, optional auto-undo, and per-tool timeouts. Three
  discovery paths: pip entry points (`[project.entry-points."headless_ida_mcp.plugins"]`),
  `~/.idapro/plugins/<name>/mcp_manifest.py`, and every path in
  `IDA_MCP_PLUGIN_PATHS`. Minimal example:

  ```python
  # mcp_manifest.py
  def ping() -> dict:
      return {"ok": True}

  PLUGIN = {"name": "demo", "description": "Demo", "version": "0.1"}
  TOOLS  = [{"name": "ping", "handler": ping, "description": "ping",
             "tags": ["kind:read"]}]
  ```

  Agents enable per-session via `enable_plugin(name)` (the server emits
  `notifications/tools/list_changed` so the client refetches `list_tools`).
  For ad-hoc Python or plugins without a manifest, `IDA_MCP_PLUGIN_PATHS`
  still injects `sys.path[0]` and `py_eval` still works.

  **Adapting an existing IDA plugin?** See
  [`docs/plugin-adaptation-guide.md`](./docs/plugin-adaptation-guide.md) —
  detailed walk-through of restructuring a reactive GUI plugin into a
  manifest-conformant agent-callable form, with 7 concrete steps, code
  templates, common pitfalls, and known limitations (Qt headless, debugpy,
  IDA main-thread). Contract reference is in
  [`docs/agent-quickstart.md`](./docs/agent-quickstart.md) §12.

  **Worked example**: see
  [`headless-ida-mcp-comment-helper`](https://github.com/RainbowXie/headless-ida-mcp-comment-helper)
  — a complete reference plugin you can `pip install` and drive from any
  MCP agent. Reads / lists / writes / bulk-clears `[mcp]`-marked comments
  on functions and instructions. Demonstrates all three capability tiers
  (read / write / unsafe) and the typed-facade pattern.
- **Built for autonomous agent workflows.** Optimized for unattended,
  long-running, batch analysis: no MCP elicitation (server never
  interrupts the agent to ask a human), no foreground / background
  prompts, no confirmation dialogs. Failures are returned as
  `error: ...` strings instead of being raised into the transport, so a
  bad tool call never drops the connection or stops a multi-step
  workflow. Connect once, hand the agent a goal, walk away.
- **Capability gating with auto-undo.** Every tool is tagged `kind:read`
  / `kind:write` / `kind:unsafe`. Writes are auto-wrapped in an
  `ida_undo` undo point so a single `undo()` call rolls back any
  agent mistake without reloading the IDB. Unsafe tools (`patch` /
  `patch_asm` / `undefine` / `py_eval` / `unset`) opt out — `ida_undo`
  cannot recover them. The operator picks the surface at startup with
  `--exclude-tags` (or `IDA_MCP_EXCLUDE_TAGS` env): drop
  `kind:write,kind:unsafe` for a strict read-only batch run, drop
  `kind:unsafe` to keep normal writes but block destructive tools, or
  drop `core::debug::*` to hide the entire `dbg_*` surface in
  deployments without a debugger. See
  [`docs/agent-quickstart.md`](./docs/agent-quickstart.md) §11 for
  details.

## Quick start (5 lines)

```bash
# Run the server straight from git, no clone needed.
# `--with <wheel>` injects IDA Pro's idapro wheel into the uvx-managed venv.
IDA_INSTALL_DIR=/opt/ida-pro-9.3 \
IDB_PATH=/path/to/sample.i64 \
uvx --python 3.12 \
    --with /opt/ida-pro-9.3/idalib/python/idapro-*.whl \
    --from git+https://github.com/RainbowXie/headless-ida-mcp-server \
    headless_ida_mcp_server
```

That's it. Server is up, IDB is loaded, up to 85 MCP tools and 11
resources are exposed (subject to any `--exclude-tags` capability filter).
Connect any MCP client and start analyzing.

When an MCP client connects, the server hands it an `instructions` field
containing the 5-step workflow primer + error conventions, so an agent can
get to its first useful tool call without reading any other docs.

## Full reference

The detailed reference — every env / CLI flag, MCP client config snippets,
all 85 tools and 11 resources, capability tags + `undo()`, plugin loading,
debugger caveats, troubleshooting — lives in
**[docs/agent-quickstart.md](./docs/agent-quickstart.md)**. Read that for
anything beyond the 5-line quickstart above.

## Architecture

In-process `idalib` SDK runs the IDA backend; FastMCP exposes the analysis
surface as 85 MCP tools and 11 MCP resources. Tool layer is vendored from
[`mrexodia/ida-pro-mcp`](https://github.com/mrexodia/ida-pro-mcp) and
re-synced ad-hoc as upstream evolves. No `idat` subprocess, no per-call
spawn overhead — connect once, drive a long agent session against one IDB.

## Prerequisites

- Python 3.12 or higher
- IDA Pro >= 9.3 with the `idapro` Python wheel
  ([idalib docs](https://docs.hex-rays.com/user-guide/idalib))
- [`uv`](https://github.com/astral-sh/uv) (for `uvx`)

## Contributing

End users follow the 5-line `uvx` quickstart above. **This Contributing
section is only for people patching the server itself.** Clone the repo,
`uv sync`, and follow the contributor flow in
[docs/agent-quickstart.md](./docs/agent-quickstart.md). PRs land on the
`v2` branch; `main` is the stable promotion target.

![](./images/pic.png)

![](./images/pic2.png)
