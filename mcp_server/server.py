"""MCP server assembly: one server, one client, the tools from `tools.py`.

Registration lives here, not in `tools.py`, because the decorator needs the server instance and
because the descriptions below are part of the contract a host shows to a model
(docs/mcp-server.md 4.2). The functions themselves stay plain and testable without a session.

Argument bounds are declared with `Annotated[int, Field(...)]` and `Literal[...]` so the SDK rejects
an out-of-range call before it reaches the API, and so a model reading the tool schema sees the cap
(`docs/mcp-server.md` 12, "a model passes an unbounded limit").
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError
from pydantic import Field

from . import __version__, tools
from .client import ApiClient, ApiError
from .config import Settings

SERVER_NAME = "material-library"

INSTRUCTIONS = (
    "Read-only access to a building-materials catalogue. Search first with search_materials, "
    "then open one record with get_material. Search hits are trimmed for context; the full "
    "document is the get_material answer. The API's own reference is docs/api-v1.md."
)

DESCRIPTIONS = {
    "search_materials": (
        "Search the catalogue by free text and filters; returns one hit per family with "
        "material_uid, name, unit and unit_cost. Start here, then open a hit with get_material. "
        "level tells you how the query was interpreted: none means nothing matched."
    ),
    "get_material": (
        "Return the full UMR v2 document for one material_uid: identifiers, measures, prices, "
        "relations and attributes. Use it after a search, when the hit list is not enough to "
        "answer. measures=all also returns non-preferred values."
    ),
    "get_family": (
        "Return the family a material belongs to plus every member variant, with its axis keys. "
        "Use it when a search hit has variant_count above one and the differences matter."
    ),
    "list_facets": (
        "Count brand, category, supplier and unit values for the same filters as "
        "search_materials. Use it to discover filter values before searching, not to list "
        "products."
    ),
    "suggest_materials": (
        "Suggest catalogue spellings similar to a query. Use it when a search returned nothing "
        "or level is none, then search again with the suggestion."
    ),
    "list_changes": (
        "Return records whose sync_seq is above a cursor, plus the next cursor. Use it to keep a "
        "local copy in step; not needed for answering a question about products."
    ),
    "catalogue_info": (
        "Return catalogue size, the highest sync_seq and the schema version. Use it to check the "
        "API is reachable and current before trusting a sync or an export."
    ),
}

RESOURCE_DESCRIPTION = "One material document, the same bytes as GET /v1/materials/{uid}."

Limit = Annotated[int, Field(ge=1, le=tools.MAX_SEARCH_LIMIT)]
PerFacet = Annotated[int, Field(ge=1, le=100)]
ChangesLimit = Annotated[int, Field(ge=1, le=tools.MAX_CHANGES_LIMIT)]
MeasureMode = Literal["preferred", "all"]
Expand = Literal["variants"]


def build_server(settings: Settings, *, api: ApiClient | None = None) -> MCPServer:
    """Create the server. `api` is injected by tests; production builds one from settings."""

    server = MCPServer(SERVER_NAME, version=__version__, instructions=INSTRUCTIONS)
    client = api if api is not None else ApiClient(settings)

    def call(fn, **kwargs: Any) -> dict[str, Any]:
        """Run one tool body, turning an API failure into an anticipated tool error."""

        try:
            return fn(client, **kwargs)
        except ApiError as exc:
            raise exc.to_tool_error() from None

    @server.tool(name="search_materials", description=DESCRIPTIONS["search_materials"])
    def search_materials(
        q: str | None = None,
        category: str | None = None,
        brand: str | None = None,
        supplier: str | None = None,
        attrs: dict[str, Any] | None = None,
        measures: dict[str, Any] | None = None,
        limit: Limit = 10,
        cursor: str | None = None,
        expand: Expand | None = None,
    ) -> dict[str, Any]:
        return call(
            tools.search_materials,
            q=q,
            category=category,
            brand=brand,
            supplier=supplier,
            attrs=attrs,
            measures=measures,
            limit=limit,
            cursor=cursor,
            expand=expand,
        )

    @server.tool(name="get_material", description=DESCRIPTIONS["get_material"])
    def get_material(uid: str, measures: MeasureMode = "preferred") -> dict[str, Any]:
        return call(tools.get_material, uid=uid, measures=measures)

    @server.tool(name="get_family", description=DESCRIPTIONS["get_family"])
    def get_family(uid: str) -> dict[str, Any]:
        return call(tools.get_family, uid=uid)

    @server.tool(name="list_facets", description=DESCRIPTIONS["list_facets"])
    def list_facets(
        q: str | None = None,
        category: str | None = None,
        brand: str | None = None,
        supplier: str | None = None,
        attrs: dict[str, Any] | None = None,
        measures: dict[str, Any] | None = None,
        per_facet: PerFacet = 12,
    ) -> dict[str, Any]:
        return call(
            tools.list_facets,
            q=q,
            category=category,
            brand=brand,
            supplier=supplier,
            attrs=attrs,
            measures=measures,
            per_facet=per_facet,
        )

    @server.tool(name="suggest_materials", description=DESCRIPTIONS["suggest_materials"])
    def suggest_materials(q: str, limit: Limit = 5) -> dict[str, Any]:
        return call(tools.suggest_materials, q=q, limit=limit)

    @server.tool(name="list_changes", description=DESCRIPTIONS["list_changes"])
    def list_changes(since: int = 0, limit: ChangesLimit = 100) -> dict[str, Any]:
        return call(tools.list_changes, since=since, limit=limit)

    @server.tool(name="catalogue_info", description=DESCRIPTIONS["catalogue_info"])
    def catalogue_info() -> dict[str, Any]:
        return call(tools.catalogue_info)

    @server.resource(
        "materials://{uid}",
        name="material",
        title="Material document",
        description=RESOURCE_DESCRIPTION,
        mime_type="application/json",
    )
    def material_resource(uid: str) -> dict[str, Any]:
        try:
            return client.get(f"/v1/materials/{uid}")
        except ApiError as exc:
            raise ResourceError(f"{exc.code}: {exc.message}") from None

    return server
