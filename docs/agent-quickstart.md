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
- The `idapro` Python wheel that ships with IDA Pro. On a stock IDA 9.3
  install the wheel lives at:
  ```
  /opt/ida-pro-9.3/idalib/python/idapro-*.whl
  ```
  Use a glob (above) so the command stays portable across IDA versions; if
  your shell does not expand globs in this position, run
  `find /opt/ida-pro-9.3 -name 'idapro-*.whl'` once and substitute the
  exact filename.
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
| `IDA_PATH` | — | Deprecated | — | Legacy env pointing at the `idat` binary. If set without `IDA_INSTALL_DIR`, the server infers `IDA_INSTALL_DIR = dirname(IDA_PATH)` and emits a deprecation warning. New deployments should set `IDA_INSTALL_DIR` directly. |
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

### stdio mode noise isolation

Under `--transport stdio` the server's stdout (fd 1) IS the JSON-RPC channel
the MCP client reads. Three sources unrelated to this fork's code path
otherwise pollute that channel:

- `libidalib.so` writes its own startup banner from C via `printf`
  (`[idalib] init_library rc=0 ...`, `[idalib] version 9.3.260213`,
  `[idalib] opened: ...`). These bypass `sys.stdout` and hit fd 1
  directly, so a Python-level `sys.stdout` swap cannot suppress them.
- Any IDA plugin auto-loaded from `~/.idapro/plugins/` may print on
  `init` / `term` (D-810 PK's `D-810 PK initialized (version 0.11)` /
  `Terminating D-810 PK...`, `mrexodia/ida-pro-mcp`'s
  `[MCP] Plugin loaded, ...`, etc.). These are operator-installed
  third-party plugins; the fork has no leverage over their output.
- Anything else that auto-loads under IDA's process and writes to fd 1.

A strict JSON-RPC client drops the connection on the first non-JSON
line. To make stdio mode robust, the server, **only when
`TRANSPORT=stdio` is resolved**, redirects fd 1 to fd 2 (stderr) before
`_bootstrap_idalib()` runs. After the redirect:

- The original fd 1 (the parent process's pipe) is preserved as
  `sys.stdout`'s underlying fd. FastMCP's stdio writer continues to
  write JSON-RPC frames to it, untouched.
- Any subsequent `printf` from C, or any plugin that prints during
  `init` / `term`, lands on stderr — the operator's terminal by default.
- `sys.stderr` itself is untouched: the Python `logger` already writes
  to stderr today and continues to.

If you want a file capture of the stderr stream, use a standard shell
redirection — the server does not own a `--diag-target=` knob:

```bash
TRANSPORT=stdio IDB_PATH=/path/to/sample.i64 \
  uvx headless_ida_mcp_server --transport stdio 2>>/tmp/headless-ida.diag.log
```

`TRANSPORT=sse` mode skips the redirect entirely. fd 1 keeps its
original meaning, and `[idalib] init_library rc=0` etc. remain visible
on the operator's terminal — useful as a sanity check that the IDA
install is correctly wired.

The fork cannot suppress the third-party plugins' print behaviour at
its source; the fd-level redirect is the only lever the server has.
See §6 for the difference between the two plugin-loading mechanisms
that produce these lines.

### Debugging

Use the MCP Inspector to poke the running server interactively:

```bash
npx -y @modelcontextprotocol/inspector
```

## 6. Plugin loading

The server discovers plugins through three first-class paths. Each path
imports a top-level `mcp_manifest.py` describing the plugin's MCP-facing
tools (see §12 below for the manifest contract). All three paths are
documented and supported public configuration knobs.

**Path A — pip entry points.** Plugins shipped as Python distributions
declare an entry point in their `pyproject.toml`:

```toml
[project.entry-points."headless_ida_mcp.plugins"]
your_plugin = "your_plugin.mcp_manifest"
```

`pip install`ing the distribution makes the plugin visible to every
`headless_ida_mcp_server` process in the same Python environment.

**Path B — directory scan.** The server scans two directory roots one
level deep for any subdirectory containing `mcp_manifest.py`:

1. `~/.idapro/plugins/*` — the conventional IDA plugins folder.
2. Every non-empty path in `IDA_MCP_PLUGIN_PATHS` (colon-separated, same
   parser as PYTHONPATH).

`IDA_MCP_PLUGIN_PATHS` is **public, agent-visible configuration** — feel
free to use it from CI / agent harnesses / documentation, including in
end-user instructions. (Earlier guidance to "hide this env from
end-users" is explicitly withdrawn.) The env retains its existing role
of front-injecting each path onto `sys.path` for legacy `py_eval`
imports; the manifest discovery and the `sys.path` injection share the
same parser.

When the same `PLUGIN.name` is discovered through both Path A and Path B,
**Path A wins** and Path B is skipped with an INFO log naming both
sources. Two manifests declaring the same `PLUGIN.name` (along the same
path or across paths) abort startup with both source locations listed —
this keeps plugin identity unambiguous.

### Per-session enable / disable lifecycle

The server registers four meta tools that drive the plugin lifecycle.
They are tagged `kind:read` plus the sentinel `core::plugin-meta` and
are immune to `--exclude-tags`:

* `plugins()` — return one entry per loaded plugin (`name`,
  `description`, `version`, `tool_count`, `enabled` per calling session).
* `plugin_tools(name)` — peek the prefixed tool names + signatures
  exposed by `<name>` without changing session state.
* `enable_plugin(name)` — add the plugin to the calling session's
  enabled set. Server emits `notifications/tools/list_changed` so the
  client refetches `list_tools` and sees the plugin tools.
* `disable_plugin(name)` — mirror operation, drops them again.

Every fresh session starts with an **empty enabled set**. Plugin enable
state is per-session and ephemeral — reconnects start over. The default
`list_tools` surface is `built-in + 4 meta`; plugin tools become visible
only after the session enables their parent plugin.

### Examples

Single directory plugin (Path B):

```bash
IDA_MCP_PLUGIN_PATHS=/path/to/your-plugin-root \
  uvx --python 3.12 \
      --with /opt/ida-pro-9.3/idalib/python/idapro-*.whl \
      --from git+https://github.com/RainbowXie/headless-ida-mcp-server \
      headless_ida_mcp_server
```

The path should contain subdirectories with `mcp_manifest.py`, e.g.
`/path/to/your-plugin-root/your_plugin/mcp_manifest.py`. The root itself
is also injected into `sys.path[0]` so legacy `py_eval` imports keep
working.

```python
# Agent-side flow:
mcp.call_tool("plugins", {})                          # list loaded plugins
mcp.call_tool("plugin_tools", {"name": "your_plugin"})# peek tools
mcp.call_tool("enable_plugin", {"name": "your_plugin"})  # opt session in
# list_tools now contains your_plugin__<short> entries; call them directly:
mcp.call_tool("your_plugin__do_thing", {...})
```

Multiple plugin roots (left-most wins on the `sys.path` side):

```bash
IDA_MCP_PLUGIN_PATHS=/path/to/plugin-a-root:/path/to/HexRaysCodeXplorer-root \
  ...
```

If the plugin is `pip install`-able with an entry point, you do not
need `IDA_MCP_PLUGIN_PATHS` at all — `site-packages` is already on
`sys.path` and Path A picks it up automatically.

**Invariant:** discovery only **imports** manifests; it never invokes
any handler. Plugin handlers must keep `import idaapi` / `ida_*`
calls inside the handler body so discovery does not touch idalib state.

### `IDA_MCP_PLUGIN_PATHS` vs `~/.idapro/plugins/`

These are two distinct loading mechanisms with different lifetimes,
visibility, and side effects. They are not mutually exclusive — they
overlap, and a single plugin can live in either (or both).

| Property | `IDA_MCP_PLUGIN_PATHS` | `~/.idapro/plugins/` |
|---|---|---|
| Trigger | Lazy: agent calls `enable_plugin(name)`. | Eager: IDA auto-loads at every process start (incl. `idalib`). |
| Scope | Fork namespace; only `headless_ida_mcp_server` reads it. | IDA install global: every `idat` / `idalib` process. |
| IDA event hooks | Cannot install IDA UI / event hooks (loaded too late, agent-driven). | Can install IDA UI / `idaapi.plugin_t` event hooks. |
| Packaging | None required: any directory with `mcp_manifest.py`. | None required: any directory IDA recognises as a plugin. |
| Per-session | Per session enable / disable via meta tools. | Loaded once per process; no per-session toggle. |
| Output side effect | Quiet (manifest import is free of side effects). | Emits init / term lines on fd 1 — see §"stdio mode noise isolation". |

`~/.idapro/plugins/` lines (e.g. `D-810 PK initialized (version 0.11)`,
`[MCP] Plugin loaded, ...`) are exactly the third-party noise the
fd-redirect from §"stdio mode noise isolation" pushes to stderr in
stdio mode. The fork cannot suppress those prints at their source —
they live outside this codebase — so the only lever is the redirect.
If you want a fully quiet stdio session, prefer `IDA_MCP_PLUGIN_PATHS`
(lazy / agent-driven / quiet at startup) over installing the same
plugin into `~/.idapro/plugins/` (eager / global / chatty).

## 7. Tools and resources

The server exposes **85 MCP tools + 11 MCP resources** (4 fork-only
tools + 81 vendored upstream tools + 11 vendored upstream resources):

| Group | Source | Examples |
|---|---|---|
| Lifecycle / undo | fork-only | `set_binary_path`, `unset`, `py_eval`, `undo` (call `unset` before re-`set_binary_path` to switch IDB; see §11 for the undo contract) |
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

**Resync workflow.** Every time the vendored `ida_mcp/api_*.py` files are
re-synced from upstream, maintainers MUST audit
`src/headless_ida_mcp_server/tags.py`: any tool name added by upstream
that is missing from `TOOL_TAGS` falls through to the conservative
`kind:read` default and is reported on every server start as a
`WARNING` log line `untagged tools default to kind:read: [...]`. Read
that log line after each resync and either confirm the new tool really
is read-only, or add the correct `kind:write` / `kind:unsafe` entry.
Same rule applies when adding a new fork-only tool: the entry MUST land
in `tags.py` in the same change as the tool itself.

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
| `error: Binary path already set ...` | A previous `set_binary_path` left the IDB loaded. Call `unset()` first, then `set_binary_path` for the new IDB. |
| Server log shows `<plugin> initialized → Terminating <plugin> ...` at startup | Normal IDA plugin auto-discovery probe. If a directory you injected via `IDA_MCP_PLUGIN_PATHS` happens to be both an importable Python package AND an IDA plugin (defines `PLUGIN_ENTRY`), idalib boots that plugin once and tears it down. Server itself is fine; agent-side `import <plugin>` still works. |
| `from <plugin> import ...` raises `ImportError` | `IDA_MCP_PLUGIN_PATHS` does not include the plugin checkout root. Verify with `py_eval(code="import sys; sys.path[:5]")`. |
| `ModuleNotFoundError: No module named 'idapro'` (when using `uvx`) | `--with <wheel>` was missing or pointed at wrong path. Run `find /opt/ida-pro-9.3 -name 'idapro-*.whl'` and use the actual wheel path. |
| `error: Debugger not running` | Debug-only tool called without a live session. Either drive `dbg_start` first or use static analysis instead. |
| Tool call hangs | Almost always a `dbg_*` call expecting a debugger. Cancel and switch to static tools. |
| `error: Decompilation failed` | The function isn't decompilable (no hex-rays decompiler for the arch, or the function is malformed). Check with `disasm` first. |

## 11. Capability gating and undo

The server classifies every registered tool with one of three capability
tiers (`kind:read` / `kind:write` / `kind:unsafe`) so unattended
deployments can shrink the exposed surface and so writes can be rolled
back with a single `undo()` call. Tag data lives in
`src/headless_ida_mcp_server/tags.py`; the wrapper layer in
`server.py` consumes it.

### The three tiers

| Tier | Behaviour | Examples |
|---|---|---|
| `kind:read` | No IDB mutation. Wrapper does nothing extra. | `decompile`, `disasm`, `xrefs_to`, `list_funcs`, `survey_binary`, `undo`, all `dbg_*` |
| `kind:write` | Mutates IDB metadata. Wrapper auto-creates an `ida_undo` undo point **before** the call so a subsequent `undo()` reverts it. | `rename`, `set_type`, `set_comments`, `define_func`, `idb_save`, `set_binary_path` |
| `kind:unsafe` | Destructive / irreversible (closes IDB, rewrites raw bytes that may straddle instruction boundaries, runs arbitrary Python, modifies state outside the IDB). Wrapper does **NOT** create an undo point — `ida_undo` cannot recover this kind of mutation. | `unset`, `py_eval`, `patch`, `patch_asm`, `undefine` |

Optional secondary tags (`core::*` / `ext::*`) form a group path:
`core::analysis::decompile`, `core::debug::registers`,
`core::lifecycle`, `ext::py_eval`. They exist purely so glob filters can
target a coarse functional area; the wrapper ignores them.

### `--exclude-tags` / `IDA_MCP_EXCLUDE_TAGS`

Comma-separated `fnmatch` globs. Any tool whose tag list matches any
glob is dropped from the registered MCP surface at server startup.
Resources are filtered with the same predicate (the resource URI is fed
into `tags_for` and defaults to `kind:read` when not enumerated).

| Goal | Flag |
|---|---|
| Read-only batch analysis (no IDB mutation, no destructive tools) | `--exclude-tags 'kind:write,kind:unsafe'` |
| Allow normal writes but block destructive tools | `--exclude-tags 'kind:unsafe'` |
| Drop the entire `dbg_*` surface in deployments without a debugger | `--exclude-tags 'core::debug::*'` |

CLI flag wins over env (same precedence as the other CLI flags). An
explicit `--exclude-tags ""` writes an empty value to the env, which
decodes back to "no filter" — the documented way to reset a filter set
in the env from a wrapper script.

```bash
# Read-only mode: rename / set_type / patch / py_eval / unset / set_binary_path are NOT registered
$ uvx headless-ida-mcp-server --exclude-tags 'kind:write,kind:unsafe'
# server log:
# registered 64 tools (21 excluded by tag filter), 11 resources (0 excluded)
# (4 fork-only tools: set_binary_path, unset, py_eval, undo)
```

The startup log always reports both totals and excluded counts so an
operator can sanity-check the resulting surface.

### The `undo()` MCP tool

`undo(steps: int = 1)` drives `ida_undo.perform_undo()` from agents.
The tool is itself tagged `kind:read` (so it remains usable in
read-only mode and so the wrapper does NOT recursively create undo
points around the undo call itself).

```python
# After a wrong rename:
mcp.call_tool("undo", {})
# → {
#     "label_before": "rename",
#     "steps_requested": 1,
#     "steps_executed": 1,
#     "error": None,
#   }

# Walk back three writes:
mcp.call_tool("undo", {"steps": 3})

# More steps requested than the undo stack has:
mcp.call_tool("undo", {"steps": 99})
# → {"steps_executed": 5, "error": "perform_undo returned False (no more undo points?)"}
```

### Undo boundaries

Each `kind:write` tool call becomes exactly one `ida_undo` group:
the wrapper calls `ida_undo.create_undo_point(name, name)` before the
call, IDA closes the previous group and opens a new one, and the
business logic appends to the fresh group. Two consecutive write
calls therefore occupy two distinct undo groups, and `undo(steps=1)`
reverts only the most recent one.

### Important: `kind:unsafe` has NO auto-undo

`patch_asm`, `patch`, `undefine`, `py_eval`, and `unset` deliberately
skip undo-point creation. Calling `undo()` after one of those will
roll back to whatever undo point was current **before** the unsafe
call — i.e. you may end up rolling back a previous *legitimate* write
instead of the unsafe one. Treat the unsafe tier as one-way; in
unattended deployments combine `--exclude-tags 'kind:unsafe'` with
explicit human review for any destructive change.

### Adding a fork-only tool

When introducing a new fork-only MCP tool, add a matching entry in
`tags.py` in the same change. Untagged fork-only tools default to
`kind:read` (which is wrong for any writer), and the startup
`untagged tools default to kind:read: [...]` warning will surface the
gap on the next run.

## 12. Plugin contract

This section is for **plugin authors** writing an `mcp_manifest.py` that
exposes typed MCP tools to agents.

### Manifest shape

```python
# your_plugin/mcp_manifest.py
from typing import Annotated, Optional


def rename_function(
    addr: Annotated[int, "Effective address of the function"],
    new_name: str,
) -> dict:
    # Lazy IDA imports inside the handler body keep discovery cheap and
    # let the manifest module import without idalib being bootstrapped.
    import ida_funcs, ida_name

    fn = ida_funcs.get_func(addr)
    if fn is None:
        from headless_ida_mcp_server.plugins import ToolError
        raise ToolError(-1, f"no function at {addr:#x}")
    if not ida_name.set_name(fn.start_ea, new_name):
        from headless_ida_mcp_server.plugins import ToolError
        raise ToolError(-2, "set_name failed")
    return {"addr": fn.start_ea, "name": new_name}


PLUGIN = {
    "name": "your_plugin",          # ^[a-z][a-z0-9_]*$, length 1-32
    "description": "Short summary",   # required, non-empty
    "version": "1.0.0",                # required
    "categories": ["analysis"],        # optional, informational
}

TOOLS = [
    {
        "name": "rename_function",          # short name only; server prefixes
        "handler": rename_function,         # callable, sync or async
        "description": "Rename one function",
        "tags": ["kind:write"],             # exactly one kind:* required
        "timeout": 15,                       # optional, seconds, default 30
        # "params": {                        # optional manifest overrides:
        #     "new_name": {"description": "Desired new name"},
        # },
        # "mcp": False,                      # hide from list_tools / plugin_tools
    },
]
```

The full prefixed tool name agents see is `<plugin>__<short>` —
`your_plugin__rename_function` in the example above. Combined name
length must be ≤ 64 characters; longer combinations abort startup.

### Schema reflection

The server reflects the handler's signature with `inspect.signature` and
maps types to JSON Schema:

| Python                                    | JSON Schema                                    |
|-------------------------------------------|------------------------------------------------|
| `int`                                     | `{"type": "integer"}`                         |
| `float`                                   | `{"type": "number"}`                          |
| `str`                                     | `{"type": "string"}`                          |
| `bool`                                    | `{"type": "boolean"}`                         |
| `list[T]`                                 | `{"type": "array", "items": <T>}`             |
| `dict`, `dict[str, T]`                    | `{"type": "object"}`                          |
| `Optional[T]` / `T \| None`               | as `T`, not required, default `null`          |
| `Annotated[T, "doc"]`                     | as `T` with `description: "doc"`              |
| no annotation                             | `{"type": "string"}` (conservative)           |

The optional `params` block in a tool entry overrides reflected fields
field-by-field. Declaring a parameter not present in the signature is a
hard error caught at startup.

### Capability tags

Each tool entry MUST declare exactly one of:

* `kind:read` — pure query; no IDB mutation.
* `kind:write` — IDB mutation that `ida_undo.perform_undo()` can revert.
  The wrapper auto-creates an undo point before invoking the handler so
  a single `undo()` call rolls back the change.
* `kind:unsafe` — destructive / unrecoverable. The wrapper does NOT
  auto-create an undo point.

Every plugin tool also gets an auto-injected `ext::<plugin>::<short>`
tag so operators can drop a plugin / tool with `--exclude-tags
'ext::<plugin>::*'`. Patterns like `ext::*` drop every plugin tool.
A fully tag-excluded plugin reports `enable_plugin(name)` failures with
the `not loaded (excluded by tag filter)` message.

### Errors

Handlers raise `headless_ida_mcp_server.plugins.ToolError(code, message)`
to signal expected failures. The wrapper converts that into the string
`error: <code>: <message>`. Any other exception type is caught, the
traceback logged at ERROR level, and the call returns
`error: <ExcType>: <message>`. The server stays up; subsequent calls
are unaffected.

### Timeouts

`timeout` defaults to 30 seconds. Sync handlers run on a shared thread
pool; async handlers are awaited under `asyncio.wait_for`. On expiry the
wrapper returns `error: timeout`. The server cannot interrupt arbitrary
IDA SDK calls cleanly, so the underlying thread / task is detached;
plan handler implementations accordingly.

### `mcp:false`

Set `"mcp": False` on a tool entry to keep it dispatchable but hide it
from `list_tools` / `plugin_tools` in every session. Useful for
plugin-internal helpers a plugin wants to keep callable from agents that
already know the name (or from another tool inside the same plugin).
`enable_plugin` / `disable_plugin` do not affect `mcp:false` tools —
they are always callable.

### Meta tools agents use

* `plugins()` — list loaded plugins with the per-session `enabled` flag.
* `plugin_tools(name)` — peek the prefixed tool names + reflected
  schemas. Side-effect-free.
* `enable_plugin(name)` — opt this session into the plugin. Emits
  `notifications/tools/list_changed`.
* `disable_plugin(name)` — drop the plugin from this session. Emits
  `notifications/tools/list_changed`.

All four are immune to `--exclude-tags` so the agent always has a path
to discover and enable plugins.

### Worked example: `your_plugin/mcp_manifest.py`

The example at the top of this section, plus the on-disk layout below,
makes the plugin discoverable through Path B (filesystem):

```
~/.idapro/plugins/
└── your_plugin/
    ├── mcp_manifest.py    # the manifest above
    └── your_plugin/        # ordinary Python package
        ├── __init__.py
        └── api.py
```

Or, for `pip install`-able plugins, declare the entry point in
`pyproject.toml`:

```toml
[project.entry-points."headless_ida_mcp.plugins"]
your_plugin = "your_plugin.mcp_manifest"
```

Either way, `plugins()` will list `your_plugin` after the next server
start; agents call `enable_plugin("your_plugin")` and the prefixed tool
`your_plugin__rename_function` becomes callable from that session.

## 13. See also

- [README.md](../README.md) — short project overview, 5-line quickstart
- [README_CN.md](../README_CN.md) — Chinese version of the project overview
- [`mrexodia/ida-pro-mcp`](https://github.com/mrexodia/ida-pro-mcp) — upstream
  source of the 81 vendored tools and 11 resources
- [`RamuneIDA/Ramune-ida`](https://github.com/RamuneIDA/Ramune-ida) — origin of
  the three-tier `kind:*` capability tag scheme this fork ports
