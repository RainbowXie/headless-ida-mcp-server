"""Vendored subset of `mrexodia/ida-pro-mcp`'s `ida_mcp` package.

Source: https://github.com/mrexodia/ida-pro-mcp
Pinned at commit `40e94f36f94043fd1824661e32ad72a903ec9e2f` (main, 2026-04-24).
Upstream path: `src/ida_pro_mcp/ida_mcp/`.

Layout
------
- `rpc.py` (stub): provides `@tool` / `@resource` / `@unsafe` / `@ext` /
  `MCP_TOOLS` / `MCP_RESOURCES` registries. The real upstream uses zeromcp;
  here we build registries that `headless_ida_mcp_server.server` walks to
  attach FastMCP decorators.
- `sync.py` (stub): `@idasync` is the identity decorator, `@tool_timeout`
  is metadata-only, and `IDAError` / `IDASyncError` / `CancelledError` mirror
  upstream's exception hierarchy. idalib runs in-process so no main-thread
  marshalling is required.
- `utils.py`, `compat.py`, `_sigmaker.py`: vendored verbatim from upstream.
- `api_*.py`: vendored verbatim. Importing them populates the registries.

api_python.py (and discovery / MCP_SERVER plumbing) are deliberately NOT
included here. py_eval already lives at `..api_python.py` (added by the
`add-py-eval-tool` change). api_discovery is upstream's process-discovery
subsystem and is meaningless for an idalib-only fork.
"""

# Import order: api_core sets up shared cache helpers consumed by api_survey.
# Importing each module is sufficient to register tools and resources.
from . import rpc  # noqa: F401  (registries live here)
from . import sync  # noqa: F401
from . import utils  # noqa: F401
from . import compat  # noqa: F401

from . import api_core  # noqa: F401
from . import api_analysis  # noqa: F401
from . import api_memory  # noqa: F401
from . import api_types  # noqa: F401
from . import api_modify  # noqa: F401
from . import api_stack  # noqa: F401
from . import api_debug  # noqa: F401
from . import api_resources  # noqa: F401
from . import api_survey  # noqa: F401
from . import api_composite  # noqa: F401
from . import api_sigmaker  # noqa: F401

from .rpc import MCP_TOOLS, MCP_RESOURCES, MCP_UNSAFE, MCP_EXTENSIONS
from .sync import IDAError, IDASyncError, CancelledError, idasync

__all__ = [
    "MCP_TOOLS",
    "MCP_RESOURCES",
    "MCP_UNSAFE",
    "MCP_EXTENSIONS",
    "IDAError",
    "IDASyncError",
    "CancelledError",
    "idasync",
]
