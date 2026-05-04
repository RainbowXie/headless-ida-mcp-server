"""
headless-ida-mcp-server

Configuration loading + idalib bootstrap.

`load_dotenv` runs at import time so that `os.environ` is populated for any
caller. Required-field validation is **deferred** to `resolve_config()` so that
the CLI entry point in `__main__.py` can write CLI flags into `os.environ`
before validation runs. Module-level constants (`IDA_INSTALL_DIR`, `IDB_PATH`,
`PORT`, `HOST`, `TRANSPORT`) are exposed lazily via PEP 562 `__getattr__`:
they reflect the current `os.environ` snapshot at first access, and accessing
`IDA_INSTALL_DIR` triggers validation. Tests / tooling that need to import the
package without IDA configured can still do so.

`_bootstrap_idalib()` is the explicit, eager idalib loader. The CLI entry point
in `__main__.py` calls it after `resolve_config()` succeeds, before importing
`server`. It (a) `ctypes.cdll.LoadLibrary`'s `libidalib.{so,dylib,dll}`, (b)
calls `init_library(0, None)` and prints the version, (c) inserts the IDA
`idalib/python` and `python` dirs onto `sys.path`, (d) imports `idapro` to
verify, (e) optionally `open_database(IDB_PATH, True)`, and (f) registers an
`atexit` cleanup that calls `idapro.close_database(False)`. Any failure prints
to stderr and raises `SystemExit(1)`.
"""
import atexit
import ctypes
import os
import signal
import sys
import warnings
from dotenv import load_dotenv, find_dotenv

from .logger import *

__all__ = [
    'IDA_INSTALL_DIR',
    'IDB_PATH',
    'IDA_MCP_PLUGIN_PATHS',
    'PORT',
    'HOST',
    'TRANSPORT',
    'resolve_config',
    '_bootstrap_idalib',
    '_inject_plugin_paths',
    '_install_stdio_isolation_if_needed',
]

_DEFAULT_PORT = 8888
_DEFAULT_HOST = "0.0.0.0"
_DEFAULT_TRANSPORT = "sse"

# Load .env early so plain `import headless_ida_mcp_server` reflects file values.
# `override=True` ensures .env wins over an empty pre-existing env var so
# operators can flip configuration without unsetting shell-level junk first.
load_dotenv(find_dotenv(), override=True)


def _resolve_ida_install_dir() -> str:
    """Resolve `IDA_INSTALL_DIR` with backward-compatible `IDA_PATH` fallback.

    - Prefer explicit `IDA_INSTALL_DIR`.
    - If unset, fall back to `dirname(IDA_PATH)` (legacy env where IDA_PATH
      points at the `idat` binary). Emit a deprecation warning and write the
      inferred value back to `os.environ` so downstream readers see a uniform
      field.
    - If neither is set, raise `ValueError` so the failure surfaces early.
    """
    install_dir = os.environ.get("IDA_INSTALL_DIR")
    if install_dir:
        return install_dir

    legacy = os.environ.get("IDA_PATH")
    if legacy:
        inferred = os.path.dirname(legacy) or legacy
        warnings.warn(
            "IDA_PATH is deprecated; please set IDA_INSTALL_DIR instead. "
            f"Inferred IDA_INSTALL_DIR={inferred!r} from IDA_PATH={legacy!r}.",
            DeprecationWarning,
            stacklevel=2,
        )
        os.environ["IDA_INSTALL_DIR"] = inferred
        return inferred

    raise ValueError(
        "IDA_INSTALL_DIR is not set; please set IDA_INSTALL_DIR (or legacy "
        "IDA_PATH) in your .env or pass --ida-install-dir on the CLI."
    )


def resolve_config() -> dict:
    """Resolve and validate all config from the current `os.environ`.

    Call once at server startup, AFTER any CLI flags have been written to
    `os.environ`. Returns the resolved values as a dict and writes any inferred
    values back to `os.environ` so downstream readers see a consistent view.

    Raises `ValueError` if `IDA_INSTALL_DIR` (and the `IDA_PATH` fallback) are
    both unset.
    """
    ida_install_dir = _resolve_ida_install_dir()
    idb_path = os.environ.get("IDB_PATH", "")
    port = os.environ.get("PORT", _DEFAULT_PORT)
    host = os.environ.get("HOST", _DEFAULT_HOST)
    transport = os.environ.get("TRANSPORT", _DEFAULT_TRANSPORT)
    return {
        "IDA_INSTALL_DIR": ida_install_dir,
        "IDB_PATH": idb_path,
        "PORT": port,
        "HOST": host,
        "TRANSPORT": transport,
    }


# ----------------------------------------------------------------------------
# transport-stdio-isolation: redirect fd 1 -> fd 2 in stdio mode
# ----------------------------------------------------------------------------

# Tracks whether the stdio fd-redirect has already been applied. The redirect
# is one-shot at bootstrap time; calling the helper twice MUST be a no-op so
# tests / reentrancy do not leak fds or break the saved JSON-RPC channel.
_stdio_isolation_done = False

# Holds the line-buffered UTF-8 TextIOWrapper over the saved JSON-RPC fd
# (fd that was originally fd 1 at process start, before `_install_stdio_
# isolation_if_needed` redirected fd 1 to stderr). FastMCP's
# `mcp.server.stdio.stdio_server()` reads it via the `stdout=` parameter
# (see `MCPServer.run_stdio_async` for the wiring) so JSON-RPC frames go
# back through the parent process's pipe even though `sys.stdout` is now
# pointed at stderr to absorb plugin / library noise.
_jsonrpc_writer = None


def _install_stdio_isolation_if_needed() -> None:
    """When `TRANSPORT=stdio`, redirect OS fd 1 -> fd 2 before idalib boots.

    Rationale (see openspec capability `transport-stdio-isolation`): under
    stdio transport the FastMCP server uses fd 1 as the JSON-RPC channel.
    Three sources outside this fork's code can write to fd 1 and pollute
    that channel:

      - `libidalib.so` writes its banner from C via `printf`; this bypasses
        Python's `sys.stdout` entirely.
      - Any Python plugin auto-loaded from `~/.idapro/plugins/` (e.g.
        D-810, `mrexodia/ida-pro-mcp` GUI variant) calls `print(...)` on
        init / teardown; this DOES go through `sys.stdout`.
      - Anything else that auto-loads under IDA's process that writes to
        fd 1 (cosmetic banners, deprecation warnings, etc.).

    A strict JSON-RPC client drops the connection on the first non-JSON
    byte, so all three sources MUST be diverted. Mechanism (only when
    `TRANSPORT=stdio`, BEFORE `_bootstrap_idalib`):

      1. `saved_jsonrpc_fd = os.dup(1)` — save the original fd 1 (the
         pipe the parent MCP client opened). This fd remains the JSON-RPC
         channel for FastMCP.
      2. `target_fd = sys.stderr.fileno()` (typically fd 2) — the
         unconditional diagnostic target. There is no env / CLI knob.
      3. `os.dup2(target_fd, 1)` — fd 1 now aliases stderr. Subsequent
         C-layer `printf` and any Python `print()` (which writes to
         `sys.stdout`, whose underlying fd is fd 1) goes to stderr.
      4. Build `_jsonrpc_writer = os.fdopen(saved_jsonrpc_fd, "w",
         buffering=1, encoding="utf-8", newline="")` — line-buffered UTF-8
         text writer over the saved fd. FastMCP's stdio writer accepts a
         `stdout=` argument; `MCPServer.run_stdio_async` passes
         `_jsonrpc_writer` so JSON-RPC frames flow through the saved fd
         and never touch fd 1 (now stderr).
      5. Note: we intentionally do NOT rebind `sys.stdout`. Leaving it
         alone (pointing at the now-redirected fd 1 = stderr) is what
         absorbs the auto-loaded plugin `print()` calls. FastMCP gets
         the JSON-RPC channel via the explicit `stdout=` argument
         instead of via `sys.stdout.buffer`.

    SSE mode (`TRANSPORT=sse`, the default) skips the redirect entirely so
    operator-visible idalib logs continue to land on the terminal stdout.
    """
    global _stdio_isolation_done, _jsonrpc_writer
    if _stdio_isolation_done:
        return
    if os.environ.get("TRANSPORT", _DEFAULT_TRANSPORT) != "stdio":
        return

    # Resolve diagnostic target fd (always stderr; no knob).
    try:
        target_fd = sys.stderr.fileno()
    except (AttributeError, OSError, ValueError):
        # If stderr has no real fd (highly unusual: e.g. tests replaced it
        # with a StringIO), there is nothing meaningful to redirect to. Skip
        # the redirect entirely rather than corrupt fd 1.
        return

    # Save original fd 1 — this remains the JSON-RPC channel.
    try:
        saved_jsonrpc_fd = os.dup(1)
    except OSError:
        # No fd 1 (e.g. detached). Nothing to isolate.
        return

    # Best-effort flush of the existing sys.stdout BEFORE we redirect fd 1
    # so any already-buffered Python output reaches the parent's stdout
    # (the JSON-RPC pipe) rather than getting lost when fd 1 changes.
    # This buffer is normally empty at this point in main() but better
    # safe than sorry.
    try:
        sys.stdout.flush()
    except Exception:
        pass

    # Point fd 1 at the diagnostic target (stderr). After this, every
    # write to fd 1 (C-layer printf, Python `print()` via sys.stdout)
    # lands on stderr.
    try:
        os.dup2(target_fd, 1)
    except OSError:
        # Restore original mapping if dup2 fails so we don't leak the dup.
        try:
            os.close(saved_jsonrpc_fd)
        except OSError:
            pass
        return

    # Build the JSON-RPC writer FastMCP will be handed via stdio_server's
    # `stdout=` parameter. Line-buffered, UTF-8, no newline translation
    # (each frame is already \n-terminated by FastMCP).
    _jsonrpc_writer = os.fdopen(
        saved_jsonrpc_fd,
        mode="w",
        buffering=1,
        encoding="utf-8",
        newline="",
    )

    _stdio_isolation_done = True


def _get_jsonrpc_writer():
    """Return the FastMCP stdio writer prepared by
    `_install_stdio_isolation_if_needed()`, or None if isolation did not
    run (SSE mode, or stdin/stdout shape did not allow the redirect)."""
    return _jsonrpc_writer


# ----------------------------------------------------------------------------
# idalib bootstrap
# ----------------------------------------------------------------------------

# Filled in by `_bootstrap_idalib()` once `idapro` has been imported. The
# `atexit` cleanup hook reads this so it stays a no-op if bootstrap never ran.
_idapro_module = None
# Tracks whether `open_database` succeeded for this process. The cleanup hook
# only calls `close_database` when this is True so we don't crash on an idalib
# state that was never opened (e.g. server started without IDB_PATH).
_idb_open = False


def _idalib_libname() -> str:
    """Per-platform shared-library filename for idalib."""
    if sys.platform == "darwin":
        return "libidalib.dylib"
    if sys.platform.startswith("win") or os.name == "nt":
        return "idalib.dll"
    # Default Linux / other POSIX.
    return "libidalib.so"


def _fail(msg: str) -> "None":
    """Print a clean error to stderr and exit non-zero."""
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _diag_fd1(line: str) -> None:
    """Write a diagnostic line directly to fd 1 (bypassing `sys.stdout`).

    In SSE mode fd 1 is the operator's terminal stdout — exactly today's
    behaviour. In stdio mode `_install_stdio_isolation_if_needed()` has
    already pointed fd 1 at stderr, so the same write lands on stderr
    where the operator wants startup diagnostics. Critically, this never
    touches the saved JSON-RPC fd that `sys.stdout` now wraps in stdio
    mode, keeping the JSON-RPC channel pristine.
    """
    try:
        data = line.encode("utf-8", errors="replace")
        # os.write is unbuffered: each call is one writev, no Python buffer
        # ordering hazards relative to the C-layer printf interleaving.
        os.write(1, data)
    except OSError:
        # fd 1 closed or otherwise broken; degrade silently — diagnostics
        # are not load-bearing.
        pass


def _bootstrap_idalib() -> None:
    """Eager idalib startup: load lib, init, set sys.path, import, open IDB.

    Sequence (matches `runtime-idalib` spec):
      1. Validate `IDA_INSTALL_DIR` is a directory.
      2. Locate `<dir>/lib<idalib>.<ext>` and `ctypes.cdll.LoadLibrary` it.
      3. Call `init_library(0, None)`; non-zero rc -> exit.
      4. Print idalib version via `get_library_version`.
      5. Insert `<dir>/idalib/python` at `sys.path[0]` and `<dir>/python` at
         `sys.path[1]` so `import idapro` and `import ida_*` stubs work.
      6. `import idapro`; ImportError -> exit.
      7. If `IDB_PATH` is set, validate path then `open_database(path, True)`.
      8. Register `atexit` cleanup -> `close_database(False)`.

    Any failure short-circuits with a stderr message and `SystemExit(1)`.
    """
    global _idapro_module, _idb_open

    # 1. IDA_INSTALL_DIR must be a real directory.
    install_dir = os.environ.get("IDA_INSTALL_DIR", "")
    if not install_dir or not os.path.isdir(install_dir):
        _fail(
            f"IDA_INSTALL_DIR is not a directory: {install_dir!r}. Set "
            f"IDA_INSTALL_DIR (or --ida-install-dir) to your IDA install root."
        )

    # 2. Locate and load libidalib.
    libname = _idalib_libname()
    libpath = os.path.join(install_dir, libname)
    if not os.path.isfile(libpath):
        _fail(
            f"{libname} not found at {libpath!r}. Verify IDA_INSTALL_DIR points "
            f"to a complete IDA installation."
        )

    try:
        idalib = ctypes.cdll.LoadLibrary(libpath)
    except OSError as exc:
        _fail(f"failed to load idalib at {libpath!r}: {exc}")

    # 3. init_library(int, char**) -> int. Non-zero == failure.
    init_fn = idalib.init_library
    init_fn.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_char_p)]
    init_fn.restype = ctypes.c_int
    rc = init_fn(0, None)
    if rc != 0:
        _fail(
            f"init_library rc={rc} for {libpath!r}. Verify the IDA license is "
            f"installed (~/.idapro/idapro.hexlic) and matches this build."
        )
    # Diagnostic line goes to fd 1 directly. In SSE mode fd 1 is the
    # operator's terminal stdout (the original behaviour). In stdio mode
    # `_install_stdio_isolation_if_needed()` has already pointed fd 1 at
    # stderr, so this line lands on stderr where it belongs — `sys.stdout`
    # in stdio mode IS the JSON-RPC channel and MUST NOT receive Python
    # diagnostics.
    _diag_fd1(f"[idalib] init_library rc=0 ({libpath})\n")

    # 4. Print version.
    try:
        major = ctypes.c_int(0)
        minor = ctypes.c_int(0)
        build = ctypes.c_int(0)
        ver_fn = idalib.get_library_version
        ver_fn.argtypes = [
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        ver_fn.restype = ctypes.c_int
        ver_fn(ctypes.byref(major), ctypes.byref(minor), ctypes.byref(build))
        _diag_fd1(
            f"[idalib] version {major.value}.{minor.value}.{build.value}\n"
        )
    except Exception as exc:
        # Non-fatal: just warn. Some idalib builds may not export this symbol.
        print(f"[idalib] get_library_version unavailable: {exc}", file=sys.stderr)

    # 5. sys.path setup. Order matters: put idalib's `idapro` package at [0]
    # so it wins against any pip-installed wheel that may be stale relative to
    # the actual idalib.so we just loaded; put IDA Python stubs at [1].
    idalib_python = os.path.join(install_dir, "idalib", "python")
    ida_stubs = os.path.join(install_dir, "python")
    if os.path.isdir(idalib_python):
        sys.path.insert(0, idalib_python)
    else:
        _fail(
            f"idalib python package missing: {idalib_python!r}. Expected "
            f"<IDA_INSTALL_DIR>/idalib/python/idapro/__init__.py."
        )
    if os.path.isdir(ida_stubs):
        sys.path.insert(1, ida_stubs)
    else:
        # Stubs are advisory; only warn. helper.py needs them but the failure
        # will surface there with a clearer ImportError on the specific module.
        print(
            f"[idalib] warning: IDA Python stubs dir not found: {ida_stubs!r}",
            file=sys.stderr,
        )

    # 6. Verify `import idapro` resolves.
    try:
        import idapro  # noqa: F401  (validated for side effect)
    except ImportError as exc:
        _fail(
            f"failed to import idapro from {idalib_python!r}: {exc}. "
            f"Verify <IDA_INSTALL_DIR>/idalib/python/idapro/__init__.py exists."
        )
    _idapro_module = idapro

    # 7. Optional IDB_PATH auto-open.
    idb_path = os.environ.get("IDB_PATH", "")
    if idb_path:
        if not os.path.exists(idb_path):
            _fail(f"IDB_PATH does not exist: {idb_path!r}")
        # idapro.open_database returns 0 on success.
        rc = idapro.open_database(idb_path, True)
        if rc != 0:
            _fail(
                f"idapro.open_database({idb_path!r}, True) returned rc={rc}. "
                f"Check that the IDB is not corrupted and license covers it."
            )
        _idb_open = True
        _diag_fd1(f"[idalib] opened: {idb_path}\n")

    # 8. Register cleanup. SIGINT / SIGTERM under the default Python handlers
    # raise KeyboardInterrupt / propagate -> interpreter shutdown -> atexit.
    atexit.register(_cleanup_idalib)

    # 9. Re-install signal handlers. idalib's `init_library` /
    # `open_database` swap the Python SIGINT handler to `SIG_DFL`, which
    # bypasses Python's interpreter shutdown -> atexit hooks NEVER fire on
    # Ctrl-C / SIGTERM. We install a thin handler that converts SIGINT /
    # SIGTERM into `SystemExit(128 + signum)` so the normal shutdown path
    # (interpreter teardown -> `atexit` -> `_cleanup_idalib`) runs.
    #
    # Compatibility: only overwrite handlers that look "default" (SIG_DFL,
    # SIG_IGN, or Python's `default_int_handler` for SIGINT). If a framework
    # already installed its own handler (e.g. uvicorn / FastMCP), preserve it
    # and trust that path to drive a clean shutdown.
    _install_shutdown_signal_handlers()


def _signal_to_systemexit(signum, frame):  # noqa: ARG001 (frame unused)
    """SIGINT/SIGTERM handler that triggers a clean Python shutdown.

    Run `_shutdown_idalib_now()` BEFORE `raise SystemExit(...)` so that the
    OK / failure log line lands on a still-open stream. The atexit fallback
    (`_cleanup_idalib`) becomes a quiet no-op afterwards thanks to the
    `_shutdown_done` idempotency flag.

    Raising `SystemExit` from a signal handler runs the normal interpreter
    teardown sequence (atexit hooks fire, finally blocks run). We use
    `128 + signum` for the exit code, the POSIX convention for "killed by
    signal N".
    """
    _shutdown_idalib_now()
    raise SystemExit(128 + signum)


def _install_shutdown_signal_handlers() -> None:
    """Reinstall SIGINT/SIGTERM handlers so atexit fires on signal exit.

    idalib internals (init_library / open_database) reset SIGINT to SIG_DFL,
    skipping Python's interpreter shutdown. We re-install our own handler.

    Skip if a non-default handler is already in place: respect frameworks
    that registered their own teardown logic.
    """
    # SIGINT: default Python handler is `signal.default_int_handler` (raises
    # KeyboardInterrupt). After idalib touches it, `getsignal` returns
    # `signal.SIG_DFL`. Treat both as "safe to overwrite".
    safe_sigint = {signal.SIG_DFL, signal.SIG_IGN, signal.default_int_handler}
    current_sigint = signal.getsignal(signal.SIGINT)
    if current_sigint in safe_sigint:
        try:
            signal.signal(signal.SIGINT, _signal_to_systemexit)
        except (ValueError, OSError):
            # Not in the main thread (rare in our bootstrap path) -> skip.
            pass

    # SIGTERM: Python's default is SIG_DFL (kernel terminate, no atexit).
    # Always install our handler if the slot is still default.
    current_sigterm = signal.getsignal(signal.SIGTERM)
    if current_sigterm in (signal.SIG_DFL, signal.SIG_IGN):
        try:
            signal.signal(signal.SIGTERM, _signal_to_systemexit)
        except (ValueError, OSError):
            pass


# Set by `_shutdown_idalib_now()` after a successful (or attempted) close so
# the atexit fallback knows not to re-run. Required because the spec calls
# the OK / failure print "once and only once" across signal-handler + atexit.
_shutdown_done = False


def _shutdown_idalib_now() -> None:
    """Idempotent close_database. Called from signal handler and as atexit fallback.

    Sequence:
      - No-op if `_idapro_module is None` or `_idb_open is False` (matches
        original `_cleanup_idalib` guards: nothing to close).
      - No-op if `_shutdown_done` is already True (signal-handler path
        already ran; atexit must not double-print).
      - Otherwise: call `idapro.close_database(False)` and print
        `[idalib] close_database(False) ok` (or the failure variant). Set
        the idempotency flag and clear `_idb_open`.

    Errors writing to stdout/stderr (e.g. `ValueError: I/O operation on
    closed file` on the racy atexit-after-finalize path) are silently
    swallowed — the spec mandates we do not let that error reach stderr.
    """
    global _idb_open, _shutdown_done
    if _shutdown_done:
        return
    if _idapro_module is None or not _idb_open:
        return
    try:
        _idapro_module.close_database(False)
        try:
            _diag_fd1("[idalib] close_database(False) ok\n")
        except (ValueError, OSError):
            # stdio already finalised; swallow per spec.
            pass
    except Exception as exc:
        # Process is exiting; surface the trace to stderr if we still can.
        try:
            print(f"[idalib] close_database failed: {exc}", file=sys.stderr)
        except (ValueError, OSError):
            # stderr also finalised; nothing actionable to do.
            pass
    finally:
        _idb_open = False
        _shutdown_done = True


def _cleanup_idalib() -> None:
    """atexit fallback: delegate to idempotent `_shutdown_idalib_now()`.

    Silently swallow `ValueError` (the `I/O operation on closed file` race
    when Python's stdio finaliser beat us) so the operator never sees the
    cosmetic shutdown error in the natural-exit path.
    """
    try:
        _shutdown_idalib_now()
    except ValueError:
        # Spec: never let `I/O operation on closed file` reach stderr.
        pass


# ----------------------------------------------------------------------------
# Generic plugin path injection
# ----------------------------------------------------------------------------


def _inject_plugin_paths() -> None:
    """Insert each path in `IDA_MCP_PLUGIN_PATHS` at `sys.path[0]`.

    Reads `IDA_MCP_PLUGIN_PATHS` (colon-separated, `PYTHONPATH`-style),
    splits, drops empty tokens, and inserts each non-empty path at the
    front of `sys.path`. We iterate **right-to-left** with
    `sys.path.insert(0, p)` so the final ordering matches the user's
    left-to-right writing order in the env var.

    Sequence (matches `runtime-plugin-paths` spec):
      1. Read `IDA_MCP_PLUGIN_PATHS` from `os.environ`. Empty / unset is a
         strict no-op: no log, no warning, no `sys.path` change.
      2. Split on `:`, drop empty tokens (covers leading / trailing /
         consecutive `::`).
      3. For each path right-to-left, `sys.path.insert(0, path)` — front
         of path so the user's plugin checkout shadows any stale
         pip-installed wheel of the same name.
      4. For each path, log `[plugin-paths] sys.path injected: <path>
         (exists: <bool>)` to stdout in left-to-right order so the visible
         log mirrors the env writing order.
      5. If a path is missing or not a directory, additionally print
         `[plugin-paths] warning: <path> does not exist` to stderr — but
         DO NOT exit. Server stays usable for tools that don't need any
         plugin (general-purpose IDA tools remain fully functional).

    INVARIANT: this function MUST NOT `import` any plugin. IDA plugins
    typically have global side effects (register_action, hook
    installation, etc.); the agent triggers the first import explicitly
    via `py_eval`. Server startup never does it.
    """
    raw = os.environ.get("IDA_MCP_PLUGIN_PATHS", "")
    if not raw:
        return  # strict no-op: no log, no warning

    # Drop empty tokens (leading / trailing / consecutive `::`).
    paths = [p for p in raw.split(":") if p]
    if not paths:
        return

    # Insert right-to-left so the final order matches env writing order.
    for path in reversed(paths):
        sys.path.insert(0, path)

    # Log each path left-to-right (mirrors how the user wrote it).
    for path in paths:
        exists = os.path.isdir(path)
        print(
            f"[plugin-paths] sys.path injected: {path} (exists: {exists})",
            flush=True,
        )
        if not exists:
            print(
                f"[plugin-paths] warning: {path} does not exist",
                file=sys.stderr,
                flush=True,
            )


# PEP 562 lazy module attributes. Reading `headless_ida_mcp_server.PORT` etc.
# returns the current `os.environ` snapshot. Reading `IDA_INSTALL_DIR` triggers
# the same validation as `resolve_config()` so eager consumers (e.g.
# `from headless_ida_mcp_server import IDA_INSTALL_DIR`) still fail fast.
_LAZY_ATTRS = {
    "IDB_PATH": lambda: os.environ.get("IDB_PATH", ""),
    "IDA_MCP_PLUGIN_PATHS": lambda: os.environ.get("IDA_MCP_PLUGIN_PATHS", ""),
    "PORT": lambda: os.environ.get("PORT", _DEFAULT_PORT),
    "HOST": lambda: os.environ.get("HOST", _DEFAULT_HOST),
    "TRANSPORT": lambda: os.environ.get("TRANSPORT", _DEFAULT_TRANSPORT),
    "IDA_INSTALL_DIR": _resolve_ida_install_dir,
}


def __getattr__(name: str):
    # Only handle the documented config attributes here. Anything else
    # (e.g. submodule access like `headless_ida_mcp_server.server`) must fall
    # through to Python's default lookup, which is what the AttributeError
    # below signals to the import machinery.
    if name in _LAZY_ATTRS:
        return _LAZY_ATTRS[name]()
    raise AttributeError(name)
