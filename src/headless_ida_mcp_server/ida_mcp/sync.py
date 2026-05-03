# vendored adaptation for headless-ida-mcp-server
# upstream source: mrexodia/ida-pro-mcp@40e94f36f94043fd1824661e32ad72a903ec9e2f, dated 2026-04-24
# upstream path: src/ida_pro_mcp/ida_mcp/sync.py
# adaptations vs upstream:
#   - removed `idaapi.execute_sync` / main-thread dispatch. headless-ida-mcp-server
#     runs idalib in-process on the calling thread; FastMCP serialises tool calls,
#     so there is no GUI thread to marshal onto. `@idasync` becomes the identity
#     decorator and `@tool_timeout` is a metadata-only decorator (the metadata is
#     surfaced for introspection by the registration layer; we do not enforce
#     timeouts at this layer).
#   - dropped imports of `idaapi`, `idc`, and `RequestCancelledError` (not needed
#     in idalib mode). `ida_major` / `ida_minor` are resolved lazily via idaapi
#     when first accessed so module import does not require IDA.
#   - kept `IDAError`, `IDASyncError`, `CancelledError` exception types so the
#     vendored api_*.py modules continue to raise typed errors. They subclass
#     plain `Exception` here (upstream subclasses zeromcp's McpToolError, which
#     is a plain Exception too).
import functools
import logging
from typing import Callable

logger = logging.getLogger(__name__)


class IDAError(Exception):
    """Tool-level error raised by api_*.py functions. Caught by the FastMCP
    registration layer in `server.py` and translated to a structured error
    string so the MCP transport stays alive.
    """

    def __init__(self, message: str):
        super().__init__(message)

    @property
    def message(self) -> str:
        return self.args[0] if self.args else ""


class IDASyncError(Exception):
    """Raised when IDA's main-thread dispatch detects nested calls. Kept for
    upstream compatibility; idalib runs in-process so this is unreachable.
    """


class CancelledError(Exception):
    """Raised when a request is cancelled. Kept for upstream compatibility."""


def __getattr__(name):
    """Lazy attribute access for `ida_major` / `ida_minor`.

    Upstream parses `idaapi.get_kernel_version()` at module load. We defer it
    so importing this module does not require idalib to be initialised yet.
    """
    if name in ("ida_major", "ida_minor"):
        try:
            import idaapi  # type: ignore[import-not-found]

            major, minor = map(int, idaapi.get_kernel_version().split("."))
        except Exception:
            major, minor = (9, 0)  # safe fallback
        # Cache in module globals so subsequent reads are O(1).
        globals()["ida_major"] = major
        globals()["ida_minor"] = minor
        return major if name == "ida_major" else minor
    raise AttributeError(name)


def idasync(f: Callable) -> Callable:
    """Identity decorator. Upstream marshals onto IDA's main thread; idalib
    runs in-process so no marshalling is required. We preserve the decorator
    so vendored api_*.py modules can be copied verbatim.
    """

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        return f(*args, **kwargs)

    # Mark wrapper so the registration layer can recover the underlying
    # function if it needs the original `__wrapped__`.
    wrapper.__ida_mcp_idasync__ = True  # type: ignore[attr-defined]
    return wrapper


def tool_timeout(seconds: float):
    """Annotate a tool function with a timeout (seconds). We do not enforce
    the timeout at this layer (FastMCP / the agent enforces request budgets);
    the metadata is exposed via `__ida_mcp_timeout_sec__` for introspection.
    """

    def decorator(func):
        setattr(func, "__ida_mcp_timeout_sec__", float(seconds))
        return func

    return decorator


def is_window_active() -> bool:
    """idalib runs without a Qt event loop. There is no active window."""
    return False


def sync_wrapper(ff, timeout_override: float | None = None):
    """Backward-compatible direct-call helper. Upstream marshals onto the
    main thread; idalib runs in-process so we just invoke `ff()`.
    """
    return ff()
