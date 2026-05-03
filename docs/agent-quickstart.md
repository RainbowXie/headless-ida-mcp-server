# Agent quickstart — full reference

Recipe for an autonomous agent (Claude Code, Claude Desktop, Cursor, or any
MCP-capable client) to drive `headless-ida-mcp-server` end-to-end:

1. Install the server once
2. Wire it into the agent's MCP client config
3. Have the agent call MCP tools to load a binary, analyze it, and (optionally)
   invoke an IDA plugin via `py_eval`

This is the **single source of truth** for everything beyond the 5-line
quickstart in [README.md](../README.md). Env vars, CLI flags, MCP config
snippets, all 84 tools and 11 resources, plugin loading mechanics,
debugger caveats, and troubleshooting all live here.

## 1. Prerequisites

- Python 3.12 or higher
- IDA Pro >= 9.3 with idalib license (idalib is bundled — license is the gate)
- [`uv`](https://github.com/astral-sh/uv) package manager (for `uvx`)
- The `idapro` Python wheel that ships with IDA Pro. Real path on a stock
  IDA 9.3 install is:
  ```
  /opt/ida-pro-9.3/idalib/python/idapro-0.0.7-py3-none-any.whl
  ```
  (filename varies by version; use `find /opt/ida-pro-9.3 -name 'idapro-*.whl'`
  if unsure)
- Optional: clone of any IDA plugin you want to load via `IDA_MCP_PLUGIN_PATHS`

## 2. One-time install

There is no separate install step for **end users**. The `uvx` form below
pulls and builds the server from git on first run; the `--with <wheel>`
flag injects IDA's `idapro` package into the uvx-managed venv.

For **contributors** who want to edit source locally:

```bash
git clone https://github.com/RainbowXie/headless-ida-mcp-server.git
cd headless-ida-mcp-server
uv pip install /opt/ida-pro-9.3/idalib/python/idapro-*.whl
py-activate-idalib
uv sync
```

## 3. Run the server

### Path A: `uvx` straight from git (recommended)

```bash
IDA_INSTALL_DIR=/opt/ida-pro-9.3 \
IDB_PATH=/path/to/sample.i64 \
IDA_MCP_PLUGIN_PATHS=/path/to/plugin-a:/path/to/plugin-b \
uvx --python 3.12 \
    --with /opt/ida-pro-9.3/idalib/python/idapro-*.whl \
    --from git+https://github.com/RainbowXie/headless-ida-mcp-server \
    headless_ida_mcp_server
```

`uvx` caches the git checkout under `~/.cache/uv/`, builds an isolated
venv, injects the `idapro` wheel, and runs the entry point. Pin a version
by appending `@<tag>` or `@<sha>` to the git URL.

### Path B: source-clone form (contributors)

After cloning + `uv sync` (Section 2):

```bash
IDA_INSTALL_DIR=/opt/ida-pro-9.3 \
IDB_PATH=/path/to/sample.i64 \
uv run headless_ida_mcp_server
```

### CLI flags

`uv run headless_ida_mcp_server --help` prints every flag with its env var
name and default. Common forms:

```bash
# Override port via CLI (env still supplies the rest)
uv run headless_ida_mcp_server --port 13337

# Switch to stdio transport for an MCP client that prefers it
uv run headless_ida_mcp_server --transport stdio
```

## 4. Configuration reference

Server startup reads its configuration from environment variables (typically
loaded via `.env`) and CLI flags. **CLI flags override env; env overrides
defaults.** Unset CLI flags do not clobber env values.

| Env | CLI flag | Required | Default | Purpose |
|---|---|---|---|---|
| `IDA_INSTALL_DIR` | `--ida-install-dir` | Yes (or fallback `IDA_PATH`) | — | IDA Pro install directory, e.g. `/opt/ida-pro-9.3`. |
| `IDA_PATH` | — | Deprecated | — | v1 field pointing at the `idat` binary. If set without `IDA_INSTALL_DIR`, the server infers `IDA_INSTALL_DIR = dirname(IDA_PATH)` and emits a deprecation warning. |
| `IDB_PATH` | `--idb-path` | No | (empty) | IDB file to auto-load at startup. When empty, agent must call `set_binary_path` first. |
| `IDA_MCP_PLUGIN_PATHS` | `--plugin-paths` | No | (empty) | Colon-separated paths (PYTHONPATH-style), e.g. `/path/to/plugin-a:/path/to/plugin-b`. Each is inserted at `sys.path[0]` after idalib bootstrap so agents can `import <plugin>` via `py_eval`. Left = highest priority on `sys.path`. Empty / unset = no injection. See §6. |
| `PORT` | `--port` | No | `8888` | MCP server listen port. |
| `HOST` | `--host` | No | `0.0.0.0` | MCP server listen host. |
| `TRANSPORT` | `--transport` | No | `sse` | MCP transport mode: `sse` or `stdio`. |

Missing `IDA_INSTALL_DIR` (and no `IDA_PATH` fallback) is a fatal startup
error: the server raises `ValueError("IDA_INSTALL_DIR is not set; ...")` at
import time so the failure surfaces immediately.

## 5. Wire into an MCP client

### stdio (recommended for desktop MCP clients)

`uvx` form — no clone needed, the agent runtime starts the server straight
from git. Substitute the actual `idapro-*.whl` filename for your IDA
version:

```json
{
  "mcpServers": {
    "ida": {
      "command": "uvx",
      "args": [
        "--python", "3.12",
        "--with", "/opt/ida-pro-9.3/idalib/python/idapro-0.0.7-py3-none-any.whl",
        "--from", "git+https://github.com/RainbowXie/headless-ida-mcp-server",
        "headless_ida_mcp_server",
        "--transport", "stdio"
      ],
      "env": {
        "IDA_INSTALL_DIR": "/opt/ida-pro-9.3",
        "IDB_PATH": "/path/to/sample.i64",
        "IDA_MCP_PLUGIN_PATHS": "/path/to/plugin-a:/path/to/plugin-b"
      }
    }
  }
}
```

The server is `stdio`-mode here, meaning it lives as a child process of the
agent's MCP client. When the client exits, the server exits too — fresh state
each session.

If you cloned the repo for local edits, the source-clone form works too:

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
        "IDA_MCP_PLUGIN_PATHS": "/path/to/plugin-a:/path/to/plugin-b"
      }
    }
  }
}
```

### sse (HTTP)

Run the server externally:

```bash
IDA_INSTALL_DIR=/opt/ida-pro-9.3 \
IDB_PATH=/path/to/sample.i64 \
uvx --python 3.12 \
    --with /opt/ida-pro-9.3/idalib/python/idapro-*.whl \
    --from git+https://github.com/RainbowXie/headless-ida-mcp-server \
    headless_ida_mcp_server --transport sse --port 8888 --host 127.0.0.1
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

### Debugging

Use the MCP Inspector to poke the running server interactively:

```bash
npx -y @modelcontextprotocol/inspector
```

## 6. Loading IDA plugins

Most IDA plugins ship as a directory you "drop into IDA's plugins/ folder"
rather than as a `pip install`-able package. To let an MCP-connected agent
`import <plugin>` via the `py_eval` tool, the server provides a generic
sys.path injection mechanism — set `IDA_MCP_PLUGIN_PATHS` (or pass
`--plugin-paths`) to a colon-separated list of plugin checkout roots.

What the server does at startup:

1. After idalib is initialized (`init_library` succeeded, `import idapro`
   resolved), the bootstrap calls `_inject_plugin_paths()`.
2. `IDA_MCP_PLUGIN_PATHS` is read from env / CLI flag. Empty / unset is a
   strict no-op (no log, no warning, no `sys.path` change).
3. The value is split on `:` (`PYTHONPATH`-style); empty tokens are
   dropped. Each non-empty path is inserted at the **front** of
   `sys.path`, in left-to-right order: writing
   `IDA_MCP_PLUGIN_PATHS=/a:/b:/c` results in `sys.path[0..2] = [/a, /b, /c]`.
4. Stdout shows one line per path:
   `[plugin-paths] sys.path injected: <path> (exists: <bool>)`. If a path
   does not exist, an additional `[plugin-paths] warning: <path> does not
   exist` lands on stderr but the server still starts — general-purpose
   IDA tools remain usable.

**Invariant:** server startup never executes `import <plugin>`. IDA plugins
typically have global side effects (register_action, hook installation,
etc.); the first import is the agent's choice via `py_eval`, not the
server's.

### Why front-insert?

`sys.path.insert(0, path)` ensures the user's plugin checkout shadows any
stale pip-installed wheel of the same name — handy when developing a plugin
locally while a wheel happens to be in your venv.

### Examples

Single plugin:

```bash
IDA_MCP_PLUGIN_PATHS=/path/to/your-plugin \
  uvx --python 3.12 \
      --with /opt/ida-pro-9.3/idalib/python/idapro-*.whl \
      --from git+https://github.com/RainbowXie/headless-ida-mcp-server \
      headless_ida_mcp_server
```

```python
# Agent-side pseudo-code:
mcp.call_tool("py_eval", {"code": "from your_plugin import api; api.__file__"})
```

Multiple plugins (colon-separated; left = highest priority on `sys.path`):

```bash
IDA_MCP_PLUGIN_PATHS=/path/to/plugin-a:/path/to/HexRaysCodeXplorer \
  uvx --python 3.12 \
      --with /opt/ida-pro-9.3/idalib/python/idapro-*.whl \
      --from git+https://github.com/RainbowXie/headless-ida-mcp-server \
      headless_ida_mcp_server
```

```python
mcp.call_tool("py_eval", {"code": "import plugin_a; plugin_a.__file__"})
mcp.call_tool("py_eval", {"code": "import HexRaysCodeXplorer"})
```

If the plugin is `pip install`-able, you don't need this mechanism at all —
`site-packages` is already on `sys.path`, and `IDA_MCP_PLUGIN_PATHS` can stay
unset.

## 7. Tools and resources

The server exposes **84 MCP tools + 11 MCP resources** (3 fork-only
lifecycle tools + 81 vendored upstream tools + 11 vendored upstream
resources):

| Group | Source | Examples |
|---|---|---|
| Lifecycle | fork-only | `set_binary_path`, `unset`, `py_eval` |
| Core / metadata | `ida_mcp/api_core.py` | `server_health`, `lookup_funcs`, `list_funcs`, `imports`, `idb_save`, `find_regex`, `search_text` |
| Analysis | `ida_mcp/api_analysis.py` | `decompile`, `disasm`, `xrefs_to`, `callees`, `callgraph`, `find_bytes`, `basic_blocks` |
| Memory | `ida_mcp/api_memory.py` | `get_bytes`, `get_int`, `get_string`, `get_global_value`, `patch`, `put_int` |
| Types | `ida_mcp/api_types.py` | `declare_type`, `enum_upsert`, `read_struct`, `search_structs`, `set_type`, `infer_types` |
| Modify | `ida_mcp/api_modify.py` | `set_comments`, `append_comments`, `patch_asm`, `rename`, `define_func`, `define_code` |
| Stack | `ida_mcp/api_stack.py` | `stack_frame`, `declare_stack`, `delete_stack` |
| Debug (best-effort under idalib) | `ida_mcp/api_debug.py` | `dbg_start`, `dbg_continue`, `dbg_regs`, `dbg_bps`, `dbg_read`, `dbg_write` |
| Survey / Composite | `ida_mcp/api_survey.py` + `api_composite.py` | `survey_binary`, `analyze_function`, `analyze_component`, `diff_before_after`, `trace_data_flow` |
| Sigmaker | `ida_mcp/api_sigmaker.py` | `make_signature`, `make_signature_for_function`, `find_xref_signatures` |
| Resources (static) | `ida_mcp/api_resources.py` | `ida://idb/metadata`, `ida://idb/segments`, `ida://idb/entrypoints`, `ida://cursor`, `ida://selection`, `ida://types`, `ida://structs` |
| Resources (templated) | `ida_mcp/api_resources.py` | `ida://struct/{name}`, `ida://import/{name}`, `ida://export/{name}`, `ida://xrefs/from/{addr}` |

The `ida_mcp/` subpackage is vendored from upstream
[`mrexodia/ida-pro-mcp`](https://github.com/mrexodia/ida-pro-mcp) and
resynced ad-hoc as upstream evolves. `api_discovery.py` (process discovery /
multi-instance plumbing) is intentionally NOT vendored — irrelevant under
idalib.

### Debugger tools are best-effort

The `dbg_*` tools (e.g. `dbg_regs`, `dbg_step_into`, `dbg_bps`) are vendored
verbatim from upstream but **idalib does not host a live debugger session**.
Calling them returns `error: Debugger not running` (or a similar structured
error) instead of crashing the server. Use them only when you have driven
idalib through `dbg_start` first; in most workflows you will rely on static
analysis (`decompile`, `disasm`, `xrefs_to`, ...) instead.

### Errors do not break MCP

Any failing tool returns a string starting with `error: ...` rather than
raising into the MCP transport (which would drop the client connection).
Resources return `{"error": "..."}` for the same reason. `set_binary_path`
must be called (or `IDB_PATH` set) before any tool that touches IDA state
— the missing-binary case returns
`error: Binary path not set (call set_binary_path first; tool=...)`.

## 8. Typical agent workflow

```text
# 1. agent starts → MCP client launches the server → server boots idalib
#    → IDB_PATH env was set, so the IDB is already open
#    → instructions field arrives in the agent's system context

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

## 9. `py_eval` recipe

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

## 10. Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| `IDA_INSTALL_DIR is not set` | Env not threaded into the MCP client config. Check the `env` field in `.mcp.json` / `~/.claude.json`. |
| `Binary path not set` | `IDB_PATH` not set and `set_binary_path` never called. Either set the env, or have the agent call `set_binary_path(path="/path/to/idb")` first. |
| `from <plugin> import ...` raises `ImportError` | `IDA_MCP_PLUGIN_PATHS` does not include the plugin checkout root. Verify with `py_eval(code="import sys; sys.path[:5]")`. |
| `ModuleNotFoundError: No module named 'idapro'` (when using `uvx`) | `--with <wheel>` was missing or pointed at wrong path. Run `find /opt/ida-pro-9.3 -name 'idapro-*.whl'` and use the actual wheel path. |
| `error: Debugger not running` | Debug-only tool called without a live session. Either drive `dbg_start` first or use static analysis instead. |
| Tool call hangs | Almost always a `dbg_*` call expecting a debugger. Cancel and switch to static tools. |
| `error: Decompilation failed` | The function isn't decompilable (no hex-rays decompiler for the arch, or the function is malformed). Check with `disasm` first. |

## 11. See also

- [README.md](../README.md) — short project overview, 5-line quickstart
- [README_CN.md](../README_CN.md) — Chinese version of the project overview
- [`mrexodia/ida-pro-mcp`](https://github.com/mrexodia/ida-pro-mcp) — upstream
  source of the 81 vendored tools and 11 resources
