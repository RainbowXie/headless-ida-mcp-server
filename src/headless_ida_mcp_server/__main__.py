"""CLI entry point.

Parses CLI flags and writes them back to `os.environ`, then runs
`resolve_config()` to validate required fields before importing `server`.

`__init__.py` is intentionally side-effect-free w.r.t. validation: it only
calls `load_dotenv()` so plain `import headless_ida_mcp_server` works in
tests / tooling that do not have IDA configured. Required-field validation is
deferred to `resolve_config()` (and the lazy `__getattr__` for
`IDA_INSTALL_DIR`).

CLI flag priority is higher than env. Unset flags do not clobber env.
"""
import argparse
import os
import sys


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="headless_ida_mcp_server",
        description=(
            "Headless IDA MCP server. CLI flags override values from .env / "
            "environment. Unset flags fall back to env, then defaults."
        ),
    )
    parser.add_argument(
        "--ida-install-dir",
        dest="ida_install_dir",
        default=None,
        help=(
            "IDA Pro install directory (env: IDA_INSTALL_DIR). Required unless "
            "the legacy IDA_PATH env is set. No default."
        ),
    )
    parser.add_argument(
        "--idb-path",
        dest="idb_path",
        default=None,
        help=(
            "Path to an IDB to auto-load at startup (env: IDB_PATH). "
            "Default: empty (wait for set_binary_path tool call)."
        ),
    )
    parser.add_argument(
        "--plugin-paths",
        dest="plugin_paths",
        default=None,
        help=(
            "Colon-separated paths to inject at sys.path[0] after idalib "
            "bootstrap so agents can `import <plugin>` via py_eval (env: "
            "IDA_MCP_PLUGIN_PATHS). Example: /path/to/your-plugin or "
            "/path/to/plugin-a:/path/to/plugin-b. Empty / unset = no injection. "
            "Default: empty."
        ),
    )
    parser.add_argument(
        "--port",
        dest="port",
        default=None,
        help="MCP server listen port (env: PORT). Default: 8888.",
    )
    parser.add_argument(
        "--host",
        dest="host",
        default=None,
        help="MCP server listen host (env: HOST). Default: 0.0.0.0.",
    )
    parser.add_argument(
        "--transport",
        dest="transport",
        default=None,
        choices=["sse", "stdio", "streamable-http"],
        help="MCP transport mode (env: TRANSPORT). Default: sse.",
    )
    parser.add_argument(
        "--exclude-tags",
        dest="exclude_tags",
        default=None,
        help=(
            "Comma-separated fnmatch globs for capability tags. Tools / "
            "resources whose tag list matches any glob are dropped from "
            "the registered MCP surface (env: IDA_MCP_EXCLUDE_TAGS). "
            "Examples: 'kind:write,kind:unsafe' (read-only mode), "
            "'kind:unsafe' (allow normal writes but block destructive "
            "tools), 'core::debug::*' (drop every dbg_* tool). Empty / "
            "unset = no filtering. See docs/agent-quickstart.md "
            "'Capability gating and undo' for the full tag taxonomy."
        ),
    )
    return parser


# Map argparse dest -> env var name. Order does not matter.
_FLAG_TO_ENV = {
    "ida_install_dir": "IDA_INSTALL_DIR",
    "idb_path": "IDB_PATH",
    "plugin_paths": "IDA_MCP_PLUGIN_PATHS",
    "port": "PORT",
    "host": "HOST",
    "transport": "TRANSPORT",
    # exclude-tags participates in the same CLI > env > default
    # precedence as the other flags. CLI explicitly passing --exclude-tags ""
    # writes "" to env, which `parse_exclude_patterns` decodes as an empty
    # list, achieving the spec's "CLI 显式空串覆盖 env" scenario.
    "exclude_tags": "IDA_MCP_EXCLUDE_TAGS",
}


def _apply_cli_to_env(args: argparse.Namespace) -> None:
    """Write CLI flag values back to `os.environ` so downstream env readers
    pick them up. Skip flags the user did not pass (value is None)."""
    for dest, env_key in _FLAG_TO_ENV.items():
        value = getattr(args, dest, None)
        if value is not None:
            os.environ[env_key] = str(value)


def main() -> None:
    parser = _build_parser()
    # argparse on parse error: prints message to stderr and exits with code 2.
    args = parser.parse_args()
    _apply_cli_to_env(args)

    # Validate required fields now, with a clean stderr message + exit 1 on
    # failure rather than letting the ValueError traceback leak through.
    from . import (
        resolve_config,
        _bootstrap_idalib,
        _inject_plugin_paths,
        _install_stdio_isolation_if_needed,
    )
    try:
        resolve_config()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)

    # transport-stdio-isolation: when TRANSPORT=stdio, redirect OS fd 1 ->
    # fd 2 BEFORE _bootstrap_idalib() so libidalib's C-layer printf and any
    # auto-loaded ~/.idapro/plugins/ chatter never lands on the JSON-RPC
    # channel that FastMCP's stdio writer reads. SSE mode skips the redirect
    # so operator-visible idalib logs remain on the terminal stdout.
    _install_stdio_isolation_if_needed()

    # Eager idalib bootstrap: load libidalib, init_library, sys.path setup,
    # `import idapro`, and (if IDB_PATH set) `open_database`. Any failure
    # exits non-zero with a clear stderr message. Must happen BEFORE we import
    # `server` because `helper.py` imports `idapro` / `ida_*` at module load.
    _bootstrap_idalib()

    # Inject IDA_MCP_PLUGIN_PATHS entries into sys.path so agents can
    # `import <plugin>` via py_eval. Must run AFTER idalib bootstrap (plugins
    # typically import `idapro` / `ida_*`, only resolvable once idalib's
    # sys.path entries are in place) and BEFORE `from .server import main`.
    # NEVER imports any plugin eagerly: agents drive the first import
    # explicitly via py_eval.
    _inject_plugin_paths()

    # Import AFTER env is updated, validated, and idalib is ready.
    from .server import main as server_main
    server_main()


if __name__ == "__main__":
    main()
