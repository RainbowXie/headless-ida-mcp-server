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
]

_DEFAULT_PORT = 8888
_DEFAULT_HOST = "0.0.0.0"
_DEFAULT_TRANSPORT = "sse"

# Load .env early so plain `import headless_ida_mcp_server` reflects file values.
# `override=True` matches v1 behavior.
load_dotenv(find_dotenv(), override=True)


def _resolve_ida_install_dir() -> str:
    """Resolve `IDA_INSTALL_DIR` with backward-compatible `IDA_PATH` fallback.

    - Prefer explicit `IDA_INSTALL_DIR`.
    - If unset, fall back to `dirname(IDA_PATH)` (v1 style where IDA_PATH points
      at the `idat` binary). Emit a deprecation warning and write the inferred
      value back to `os.environ` so downstream readers see a uniform field.
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
    print(f"[idalib] init_library rc=0 ({libpath})", flush=True)

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
        print(
            f"[idalib] version {major.value}.{minor.value}.{build.value}",
            flush=True,
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
        print(f"[idalib] opened: {idb_path}", flush=True)

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

    Raising `SystemExit` from a signal handler runs the normal interpreter
    teardown sequence (atexit hooks fire, finally blocks run). We use
    `128 + signum` for the exit code, the POSIX convention for "killed by
    signal N".
    """
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


def _cleanup_idalib() -> None:
    """`atexit` hook: best-effort `close_database(False)`. Never re-raises."""
    global _idb_open
    if _idapro_module is None or not _idb_open:
        return
    try:
        _idapro_module.close_database(False)
        print("[idalib] close_database(False) ok", flush=True)
    except Exception as exc:
        # Swallow: process is exiting; we just log so the user sees the trace.
        try:
            print(f"[idalib] close_database failed: {exc}", file=sys.stderr)
        except Exception:
            pass
    finally:
        _idb_open = False


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
