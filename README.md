# Acknowledgments

This project builds upon the work of:
- Tools code adapted from [ida-pro-mcp](https://github.com/mrexodia/ida-pro-mcp) by mrexodia
- Utilizes the [headless-ida](https://github.com/DennyDai/headless-ida) library by DennyDai
- Fork and develop from [headless-ida-mcp-server](https://github.com/cnitlrt/headless-ida-mcp-server) by cnitlrt

# Headless IDA MCP Server

Run an IDA Pro analysis backend headlessly and expose it as an MCP server.
Useful when you want to drive IDA from a CLI / agent / CI rather than as an
interactive plugin.

## Quick start

Four steps to get a server running locally:

1. Install the IDA Pro Python library wheel that ships with your IDA Pro
   install. From inside the IDA Pro install directory:

   ```bash
   uv pip install ./idapro-*.whl
   ```

2. Activate idalib so it can locate your IDA install:

   ```bash
   py-activate-idalib
   ```

3. Copy the example env file and edit it for your machine:

   ```bash
   cp .env_example .env
   # then edit .env: set IDA_INSTALL_DIR, optional IDB_PATH, PORT, etc.
   ```

4. Start the server:

   ```bash
   uv run headless_ida_mcp_server
   ```

## Configuration

Server startup reads its configuration from environment variables (typically
loaded via `.env`) and CLI flags. **CLI flags override env; env overrides
defaults.** Unset CLI flags do not clobber env values.

| Env | CLI flag | Required | Default | Purpose |
|---|---|---|---|---|
| `IDA_INSTALL_DIR` | `--ida-install-dir` | Yes (or fallback `IDA_PATH`) | — | IDA Pro install directory, e.g. `/opt/ida-pro-9.3`. |
| `IDA_PATH` | — | Deprecated | — | v1 field pointing at the `idat` binary. If set without `IDA_INSTALL_DIR`, the server infers `IDA_INSTALL_DIR = dirname(IDA_PATH)` and emits a deprecation warning. |
| `IDB_PATH` | `--idb-path` | No | (empty) | IDB file to auto-load at startup. When empty, agent must call `set_binary_path` first. |
| `PORT` | `--port` | No | `8888` | MCP server listen port. |
| `HOST` | `--host` | No | `0.0.0.0` | MCP server listen host. |
| `TRANSPORT` | `--transport` | No | `sse` | MCP transport mode: `sse` or `stdio`. |

`uv run headless_ida_mcp_server --help` prints every flag with its env var name
and default. Missing `IDA_INSTALL_DIR` (and no `IDA_PATH` fallback) is a fatal
startup error: the server raises `ValueError("IDA_INSTALL_DIR is not set; ...")`
at import time so the failure surfaces immediately.

### CLI examples

```bash
# Pure env-driven (uses .env)
uv run headless_ida_mcp_server

# Override port via CLI (env still supplies the rest)
uv run headless_ida_mcp_server --port 13337

# Switch to stdio transport for an MCP client that prefers it
uv run headless_ida_mcp_server --transport stdio
```

## MCP client config

Add the server to your MCP client config. Two transports are supported.

### stdio (recommended for desktop MCP clients)

```json
{
  "mcpServers": {
    "ida": {
      "command": "/path/to/uv",
      "args": [
        "--directory", "/path/to/headless-ida-mcp-server",
        "run", "headless_ida_mcp_server",
        "--transport", "stdio"
      ],
      "env": {
        "IDA_INSTALL_DIR": "/opt/ida-pro-9.3",
        "IDB_PATH": "/path/to/sample.i64"
      }
    }
  }
}
```

### sse (HTTP)

Start the server externally, then point your MCP client at the listening URL:

```bash
uv run headless_ida_mcp_server --transport sse --port 8888 --host 127.0.0.1
```

```json
{
  "mcpServers": {
    "ida": {
      "url": "http://127.0.0.1:8888/sse"
    }
  }
}
```

### Debugging

Use the MCP Inspector to poke the running server interactively:

```bash
npx -y @modelcontextprotocol/inspector
```

## Tools and resources

The server registers **84 MCP tools + 11 MCP resources** at startup (3 fork-only
lifecycle tools + 81 vendored upstream tools + 11 vendored upstream resources):

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
[`mrexodia/ida-pro-mcp`](https://github.com/mrexodia/ida-pro-mcp) and resynced
ad-hoc as upstream evolves. `api_discovery.py` (process discovery /
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

## Architecture notes

This fork tracks two execution lines:

- **v1**: original implementation forked from
  [cnitlrt/headless-ida-mcp-server](https://github.com/cnitlrt/headless-ida-mcp-server),
  using the `headless_ida` library to spawn `idat` per call. Async support
  added on top.
- **v2** (current default): rewrites all helpers in `helper.py` against the
  in-process `idalib` SDK. Removes the `headless_ida` dependency and the
  per-call `idat` startup, dramatically improving tool latency.

`IDA_INSTALL_DIR` (replacing the legacy `IDA_PATH`) drives both lines: v1 uses
it to locate `idat`, v2 hands it to idalib activation. The actual idalib
bootstrap logic — `idapro.open_database` / `close_database`, lifecycle
management, the `IDA_PRO_TIMEOUT` knob — is implemented in a separate change
(`add-idalib-bootstrap`) and is intentionally **out of scope** for this
configuration-only change.

## Prerequisites

- Python 3.12 or higher
- IDA Pro >= 9.0 with the `idapro` Python wheel installed
  ([idalib docs](https://docs.hex-rays.com/user-guide/idalib))
- v1 only: `headless_ida` and an accessible `idat` binary
  ([DennyDai/headless-ida](https://github.com/DennyDai/headless-ida))

## Installation

1. Clone the project locally:

   ```bash
   git clone https://github.com/A1Lin/headless-ida-mcp-server.git
   cd headless-ida-mcp-server
   git checkout v1   # or v2
   ```

2. Install dependencies:

   ```bash
   uv python install 3.12
   uv venv --python 3.12
   uv pip install -e .
   ```

3. Continue with [Quick start](#quick-start) above.

![](./images/pic.png)

![](./images/pic2.png)
