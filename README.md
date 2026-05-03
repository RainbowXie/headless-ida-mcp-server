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

- **Full upstream tool surface.** 84 MCP tools + 11 MCP resources vendored
  verbatim from
  [`mrexodia/ida-pro-mcp`](https://github.com/mrexodia/ida-pro-mcp), covering
  analysis (`decompile` / `disasm` / `xrefs_to` / `callgraph` / ...), memory,
  types, structures, modify, stack, search, sigmaker, and debugger surfaces.
  Re-synced ad-hoc as upstream evolves.
- **Headless IDA fully driven by an agent.** Runs the IDA backend through
  the in-process `idalib` SDK — no `idat` subprocess, no per-call spawn
  overhead. One MCP connection drives a long agent session against one IDB.
  Renames / comments / type changes persist to the IDB via `idb_save`.
  When a client connects the server hands it an `instructions` field with
  a 5-step workflow primer, so an agent gets to its first useful tool call
  without reading any out-of-band docs.
- **Any IDA plugin is importable.** Generic `IDA_MCP_PLUGIN_PATHS` env
  (colon-separated, PYTHONPATH-style) gets each plugin checkout root
  injected at `sys.path[0]` after idalib bootstrap. Agent calls
  `py_eval(code="from <plugin> import api; ...")` to drive any plugin's
  Python API — no per-plugin special-casing in the server, no packaging
  required from the plugin author. The classic "drop into IDA's plugins/
  folder" plugin layout works as-is.

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

That's it. Server is up, IDB is loaded, 84 MCP tools and 11 resources are
exposed. Connect any MCP client and start analyzing.

When an MCP client connects, the server hands it an `instructions` field
containing the 5-step workflow primer + error conventions, so an agent can
get to its first useful tool call without reading any other docs.

## Full reference

The detailed reference — every env / CLI flag, MCP client config snippets,
all 84 tools and 11 resources, plugin loading, debugger caveats,
troubleshooting — lives in
**[docs/agent-quickstart.md](./docs/agent-quickstart.md)**. Read that for
anything beyond the 5-line quickstart above.

## Architecture

In-process `idalib` SDK runs the IDA backend; FastMCP exposes the analysis
surface as 84 MCP tools and 11 MCP resources. Tool layer is vendored from
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
