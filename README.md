# Headless IDA MCP Server

Run IDA Pro headlessly. Drive it from any MCP agent.

> Built upon [`mrexodia/ida-pro-mcp`](https://github.com/mrexodia/ida-pro-mcp) (tools), [A1Lin/headless-ida-mcp-server](https://github.com/A1Lin/headless-ida-mcp-server) (idalib rewrite), [cnitlrt/headless-ida-mcp-server](https://github.com/cnitlrt/headless-ida-mcp-server) (lineage origin).
>
> 中文版本见 [README_CN.md](./README_CN.md)。

## What

In-process `idalib` SDK runs the IDA backend; FastMCP exposes it as **85 MCP tools + 11 resources**, with hot-pluggable third-party plugins via [`mcp_manifest.py`](./docs/plugin-adaptation-guide.md), capability tags with auto-undo, and an `instructions` primer pushed to clients on connect. Built for unattended batch agent workflows, not interactive use.

## Features

- **Full tool surface** — analysis (`decompile` / `disasm` / `xrefs_to` / `callgraph` / ...), memory, types, structures, modify, stack, search, sigmaker, debugger. 81 vendored from upstream + 4 fork-only (`set_binary_path` / `unset` / `py_eval` / `undo`). Re-synced ad-hoc.
- **In-process idalib** — no `idat` subprocess, no per-call spawn. One MCP connection drives a long agent session against one IDB. Mutations persist via `idb_save`.
- **Typed plugins via `mcp_manifest.py`** — drop a manifest next to your plugin source; the server reflects each handler into a typed MCP tool with capability tags, auto-undo, and per-tool timeouts. Three discovery paths: pip entry points, `~/.idapro/plugins/`, and `IDA_MCP_PLUGIN_PATHS`. Adapt an existing IDA plugin → see [`docs/plugin-adaptation-guide.md`](./docs/plugin-adaptation-guide.md). **Worked example**: see [`headless-ida-mcp-comment-helper`](https://github.com/RainbowXie/headless-ida-mcp-comment-helper) — a complete reference plugin you can `pip install` and drive from any MCP agent. Reads / lists / writes / bulk-clears `[mcp]`-marked comments on functions and instructions. Demonstrates all three capability tiers (read / write / unsafe) and the typed-facade pattern.
- **Capability gating + auto-undo** — every tool tagged `kind:read` / `kind:write` / `kind:unsafe`. Writes auto-wrap an `ida_undo` undo point so a single `undo()` call rolls back any agent mistake. Operators drop tiers via `--exclude-tags 'kind:write,kind:unsafe'` for read-only batch runs.
- **Built for autonomous agents** — no MCP elicitation, no confirmation dialogs. Failures return as `error: ...` strings instead of dropping the connection. Connect once, hand the agent a goal, walk away.

## Quick start

```bash
IDA_INSTALL_DIR=/opt/ida-pro-9.3 \
IDB_PATH=/path/to/sample.i64 \
uvx --python 3.12 \
    --with /opt/ida-pro-9.3/idalib/python/idapro-*.whl \
    --from git+https://github.com/RainbowXie/headless-ida-mcp-server \
    headless_ida_mcp_server
```

Server up, IDB loaded, tools exposed. Connect any MCP client.

## Documentation

| Doc | What it covers |
|---|---|
| [`docs/agent-quickstart.md`](./docs/agent-quickstart.md) | Full reference: every env / CLI flag, MCP client config, tool catalog, capability gating §11, plugin contract §12, troubleshooting |
| [`docs/plugin-adaptation-guide.md`](./docs/plugin-adaptation-guide.md) | Adapt an existing IDA plugin to the contract: 7 steps, code templates, known limitations |

## Prerequisites

- Python 3.12+
- IDA Pro ≥ 9.3 with the `idapro` Python wheel ([idalib docs](https://docs.hex-rays.com/user-guide/idalib))
- [`uv`](https://github.com/astral-sh/uv)

## Contributing

Clone, `uv sync`, follow the contributor flow in [`docs/agent-quickstart.md`](./docs/agent-quickstart.md). PRs land on `v2`; `main` is the stable promotion target.

![](./images/pic.png)
![](./images/pic2.png)
