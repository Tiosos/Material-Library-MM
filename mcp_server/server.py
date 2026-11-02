"""MCP server assembly: one server, one client, tools registered in `tools.py`.

Step 0 keeps this file deliberately small - it builds a server with no tools so the transports and
the entry point can be exercised before any route is wrapped (docs/mcp-server.md 11).
"""

from __future__ import annotations

from mcp.server import MCPServer

from . import __version__
from .client import ApiClient
from .config import Settings

SERVER_NAME = "material-library"

INSTRUCTIONS = (
    "Read-only access to a building-materials catalogue. Search first with search_materials, "
    "then open one record with get_material. Results are trimmed for context; the API's own "
    "documentation is docs/api-v1.md."
)


def build_server(settings: Settings, *, api: ApiClient | None = None) -> MCPServer:
    """Create the server. `api` is injected by tests; production builds one from settings."""

    server = MCPServer(
        SERVER_NAME,
        version=__version__,
        instructions=INSTRUCTIONS,
    )
    client = api if api is not None else ApiClient(settings)
    server.api = client  # type: ignore[attr-defined]
    return server
