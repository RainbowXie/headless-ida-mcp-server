# Agent quickstart

Recipe for an autonomous agent (Claude Code, Claude Desktop, Cursor, or any
MCP-capable client) to drive `headless-ida-mcp-server` end-to-end:

1. Install the server once
2. Wire it into the agent's MCP client config
3. Have the agent call MCP tools to load a binary, analyze it, and (optionally)
   invoke an IDA plugin via `py_eval`

This document is opinionated on **what an agent needs to know to be productive
on day one**. For full reference (every config knob, every transport mode,
every CLI flag) see the main [README](../README.md).

## 1. Prerequisites

- IDA Pro >= 9.3 with idalib license (idalib is bundled — license is the gate)
- Python 3.12+
- [`uv`](https://github.com/astral-sh/uv) package manager
- Two repos cloned:
  - `/path/to/headless-ida-mcp-server` (this repo)
  - `/path/to/your-plugin` if you want the agent to load an IDA plugin

## 2. One-time install

```bash
cd /path/to/headless-ida-mcp-server

# 1. Install the idapro wheel that ships with IDA Pro
uv pip install /opt/ida-pro-9.3/idapro-*.whl

# 2. Activate idalib so it knows where to find IDA
py-activate-idalib

# 3. Install server dependencies
uv sync
```

## 3. Wire into an MCP client

### Claude Code / Claude Desktop (stdio, recommended)

Add to `~/.claude.json` (or your project's `.mcp.json`):

```json
{
  "mcpServers": {
    "ida": {
      "command": "/usr/bin/uv",
      "args": [
        "--directory", "/path/to/headless-ida-mcp-server",
        "run", "headless_ida_mcp_server",
        "--transport", "stdio"
      ],
      "env": {
        "IDA_INSTALL_DIR": "/opt/ida-pro-9.3",
        "IDB_PATH": "/path/to/sample.i64",
        "IDA_MCP_PLUGIN_PATHS": "/path/to/your-plugin"
      }
    }
  }
}
```

| Field | Required | Notes |
|---|---|---|
| `IDA_INSTALL_DIR` | Yes | Path to your IDA Pro install root |
| `IDB_PATH` | No | If set, the IDB auto-loads at startup. If empty, the agent must call the `set_binary_path` tool before any IDA-touching tool |
| `IDA_MCP_PLUGIN_PATHS` | No | Colon-separated plugin checkout roots (PYTHONPATH-style). Each path is inserted at `sys.path[0]` so the agent can `from <plugin> import ...` via `py_eval` |

The server is `stdio`-mode here, meaning it lives as a child process of the
agent's MCP client. When the client exits, the server exits too — fresh state
each session.

### SSE / HTTP

Run the server externally:

```bash
uv run headless_ida_mcp_server --transport sse --port 8888 --host 127.0.0.1
```

Point the MCP client at the URL:

```json
{
  "mcpServers": {
    "ida": {
      "url": "http://127.0.0.1:8888/sse"
    }
  }
}
```

Useful when multiple clients share one server, or when you want the server to
outlive any single agent session.

## 4. Tools the agent now has

The server exposes **84 MCP tools + 11 MCP resources**:

| Group | Examples |
|---|---|
| Lifecycle | `set_binary_path`, `unset`, `py_eval` |
| Functions | `list_funcs`, `lookup_funcs`, `decompile`, `disasm`, `xrefs_to`, `callees`, `callgraph` |
| Memory | `get_bytes`, `get_int`, `get_string`, `get_global_value`, `patch`, `put_int` |
| Types | `declare_type`, `enum_upsert`, `read_struct`, `search_structs`, `set_type`, `infer_types` |
| Modify | `rename`, `set_comments`, `append_comments`, `set_type`, `define_func`, `define_code`, `idb_save` |
| Stack | `stack_frame`, `declare_stack`, `delete_stack` |
| Search | `find_bytes`, `find_regex`, `search_text`, `find_xref_signatures` |
| Survey/Composite | `survey_binary`, `analyze_function`, `analyze_component`, `diff_before_after`, `trace_data_flow` |
| Sigmaker | `make_signature`, `make_signature_for_function` |
| Resources | `ida://idb/metadata`, `ida://idb/segments`, `ida://idb/entrypoints`, `ida://structs`, `ida://struct/{name}` |

`dbg_*` tools (~20 of them) are vendored from upstream but require a live
debugger session that idalib does not host by default; they return
`error: Debugger not running` instead of crashing. Use `dbg_start` first if
you genuinely need them, otherwise rely on static analysis tools.

## 5. Typical agent workflow

```text
# 1. agent starts → MCP client launches the server → server boots idalib
#    → IDB_PATH env was set, so the IDB is already open

# 2. agent: list_funcs(queries=[{"count": 10}])
#    → first 10 functions: addr/name/size

# 3. agent: lookup_funcs(["main"])
#    → finds main entry: addr=0x1329, size=0x10b

# 4. agent: decompile(name="main")
#    → 1725 chars of pseudocode (native hex-rays output)

# 5. agent: py_eval(code="from your_plugin import api; api.start(project='ollvm')")
#    → plugin hooks installed (if your plugin works that way)

# 6. agent: decompile(name="main")
#    → pseudocode now runs through your plugin's hex-rays hooks

# 7. agent: py_eval(code="from your_plugin import api; api.scan_indirect(scope=0x1329)")
#    → plugin-specific analysis result

# 8. agent: idb_save()
#    → persist annotations / renames / comments back to the IDB
```

## 6. Plugin loading via `py_eval`

`py_eval` runs arbitrary Python in the server process. It captures
`stdout`/`stderr`, returns `repr(value)` of the last expression, and never
raises into the MCP transport (exceptions become `stderr` text):

```python
mcp.call_tool("py_eval", {"code": "import sys; sys.version"})
# → {"result": "'3.12.x ...'", "stdout": "", "stderr": ""}

mcp.call_tool("py_eval", {"code": "from your_plugin import api; api.__file__"})
# → {"result": "'/path/to/your-plugin/your_plugin/__init__.py'",
#    "stdout": "", "stderr": ""}

mcp.call_tool("py_eval", {"code": "1/0"})
# → {"result": "", "stdout": "",
#    "stderr": "Traceback ... ZeroDivisionError: division by zero"}
```

Anything you can write in Python is fair game: import IDA SDK modules
(`idaapi`, `idautils`, `idc`, `ida_funcs`, `ida_hexrays`, ...), drive
plugin APIs, walk the database, mutate state.

## 7. Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| `IDA_INSTALL_DIR is not set` | Env not threaded into the MCP client config. Check the `env` field in `.mcp.json` / `~/.claude.json`. |
| `Binary path not set` | `IDB_PATH` not set and `set_binary_path` never called. Either set the env, or have the agent call `set_binary_path(path="/path/to/idb")` first. |
| `from <plugin> import ...` raises `ImportError` | `IDA_MCP_PLUGIN_PATHS` does not include the plugin checkout root. Verify with `py_eval(code="import sys; sys.path[:3]")`. |
| `error: Debugger not running` | Debug-only tool called without a live session. Either drive `dbg_start` first or use static analysis instead. |
| Tool call hangs | Almost always a `dbg_*` call expecting a debugger. Cancel and switch to static tools. |
| `error: Decompilation failed` | The function isn't decompilable (no hex-rays decompiler for the arch, or the function is malformed). Check with `disasm` first. |

## 8. See also

- [README.md](../README.md) — full reference, every config knob
- [README_CN.md](../README_CN.md) — Chinese version of the main README
- [`mrexodia/ida-pro-mcp`](https://github.com/mrexodia/ida-pro-mcp) — upstream
  source of the 81 vendored tools and 11 resources
