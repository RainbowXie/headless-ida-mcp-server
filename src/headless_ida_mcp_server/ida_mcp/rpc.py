# vendored adaptation for headless-ida-mcp-server
# upstream source: mrexodia/ida-pro-mcp@40e94f36f94043fd1824661e32ad72a903ec9e2f, dated 2026-04-24
# upstream path: src/ida_pro_mcp/ida_mcp/rpc.py
# adaptations vs upstream:
#   - dropped the entire zeromcp / McpServer scaffold. headless-ida-mcp-server
#     uses FastMCP as the MCP transport, so we don't need a second JSON-RPC
#     registry. Instead, the decorators below record each function in
#     module-level registries (`MCP_TOOLS`, `MCP_RESOURCES`, `MCP_UNSAFE`,
#     `MCP_EXTENSIONS`). `server.py` walks these registries on import and
#     registers each entry with the FastMCP instance.
#   - dropped output-size limiting / output cache. FastMCP delivers raw return
#     values; large-result handling is the agent's responsibility (matches
#     pre-sync behaviour of headless-ida-mcp-server).
#   - dropped the SSE / external-base-URL plumbing (FastMCP owns transport).
from typing import Callable, Dict, List, Set, Tuple

# Public registries. Order is preserved so the registration layer in
# `server.py` can iterate deterministically.
MCP_TOOLS: List[Tuple[str, Callable]] = []
MCP_RESOURCES: List[Tuple[str, Callable]] = []
MCP_UNSAFE: Set[str] = set()
MCP_EXTENSIONS: Dict[str, Set[str]] = {}

# Index by name for O(1) lookup. Used for sanity-checks / introspection only;
# the registration layer iterates the lists above.
_TOOL_INDEX: Dict[str, Callable] = {}
_RESOURCE_INDEX: Dict[str, Callable] = {}


def tool(func: Callable) -> Callable:
    """Mark `func` as a tool. Returns the original function unmodified so the
    registration layer in `server.py` can attach FastMCP's own decorator.
    """
    name = func.__name__
    if name in _TOOL_INDEX:
        # Upstream allows overrides; we mirror that behaviour by replacing.
        for i, (existing, _) in enumerate(MCP_TOOLS):
            if existing == name:
                MCP_TOOLS[i] = (name, func)
                break
    else:
        MCP_TOOLS.append((name, func))
    _TOOL_INDEX[name] = func
    return func


def resource(uri: str):
    """Mark `func` as a resource bound to `uri`. URIs may include FastMCP
    template placeholders such as `ida://struct/{name}`.
    """

    def decorator(func: Callable) -> Callable:
        if uri in _RESOURCE_INDEX:
            for i, (existing_uri, _) in enumerate(MCP_RESOURCES):
                if existing_uri == uri:
                    MCP_RESOURCES[i] = (uri, func)
                    break
        else:
            MCP_RESOURCES.append((uri, func))
        _RESOURCE_INDEX[uri] = func
        # Tag the function so registration layer can correlate.
        setattr(func, "__ida_mcp_resource_uri__", uri)
        return func

    return decorator


def unsafe(func: Callable) -> Callable:
    """Mark a tool as unsafe (mutates IDB). Recorded for documentation /
    introspection; FastMCP does not consume this metadata.
    """
    MCP_UNSAFE.add(func.__name__)
    setattr(func, "__ida_mcp_unsafe__", True)
    return func


def ext(group: str):
    """Mark a tool as belonging to an extension group (e.g. "dbg"). Upstream
    hides extension tools from `list_tools` until the agent enables the group
    via `?ext=group`. We register every tool unconditionally so agents can
    call them directly; the metadata is preserved for downstream filtering.
    """

    def decorator(func: Callable) -> Callable:
        MCP_EXTENSIONS.setdefault(group, set()).add(func.__name__)
        setattr(func, "__ida_mcp_ext_group__", group)
        return func

    return decorator


# -- Compatibility shims for upstream code paths that reference these names ----
# api_discovery.py imports `MCP_SERVER`; we don't ship api_discovery so this
# stays unused, but keep it defined so accidental imports do not crash module
# load. Anything that pokes at .registry or .methods would need a real server,
# which is intentional: those features belong to upstream's process-discovery
# subsystem and are out of scope for an idalib-only fork.
class _MissingMcpServer:
    def __getattr__(self, name):
        raise AttributeError(
            "MCP_SERVER is not available in headless-ida-mcp-server (idalib-only fork). "
            "Upstream's zeromcp scaffold is replaced by FastMCP; see server.py."
        )


MCP_SERVER = _MissingMcpServer()


def get_cached_output(output_id):  # pragma: no cover - upstream API surface
    return None


def set_download_base_url(url):  # pragma: no cover
    pass


def get_download_base_url():  # pragma: no cover
    return ""


def get_current_transport_session_id():  # pragma: no cover
    return None


__all__ = [
    "MCP_TOOLS",
    "MCP_RESOURCES",
    "MCP_UNSAFE",
    "MCP_EXTENSIONS",
    "MCP_SERVER",
    "tool",
    "resource",
    "unsafe",
    "ext",
    "get_cached_output",
    "set_download_base_url",
    "get_download_base_url",
    "get_current_transport_session_id",
]
