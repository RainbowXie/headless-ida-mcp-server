"""
headless-ida-mcp-server

Configuration loading.

`load_dotenv` runs at import time so that `os.environ` is populated for any
caller. Required-field validation is **deferred** to `resolve_config()` so that
the CLI entry point in `__main__.py` can write CLI flags into `os.environ`
before validation runs. Module-level constants (`IDA_INSTALL_DIR`, `IDB_PATH`,
`PORT`, `HOST`, `TRANSPORT`) are exposed lazily via PEP 562 `__getattr__`:
they reflect the current `os.environ` snapshot at first access, and accessing
`IDA_INSTALL_DIR` triggers validation. Tests / tooling that need to import the
package without IDA configured can still do so.
"""
import os
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
