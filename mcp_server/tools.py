"""The seven read-only tools (docs/mcp-server.md 4).

Each function builds query parameters, makes one call through `ApiClient`, trims what a model does
not need and returns a plain dict. No SQL, no business rules, no retries: if a rule is not already in
the API or in SQL, it does not belong here (docs/mcp-server.md 3, principle 1).

`tools.SPECS` is the single list the server registers and the tests assert against, so a tool cannot
exist in one place and not the other.
"""

from __future__ import annotations

from typing import Any, Callable

from .client import ApiClient

# A hit carries thirteen fields; a model needs six to decide what to open next (doc 4.1).
HIT_FIELDS = ("material_uid", "name", "unit", "unit_cost", "variant_count", "variants")

# Envelope fields kept on a search: the answer, how sure the engine is, and how to ask for more.
SEARCH_FIELDS = (
    "schema_version",
    "q",
    "level",
    "total",
    "collapsed",
    "suggestion",
    "limit",
    "offset",
    "next_cursor",
    "took_ms",
    "log_id",
)

MAX_SEARCH_LIMIT = 50
MAX_CHANGES_LIMIT = 200


def _trim_hits(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep the envelope, trim every hit to `HIT_FIELDS` (doc 4.1)."""

    out = {key: payload[key] for key in SEARCH_FIELDS if key in payload}
    out["results"] = [
        {key: hit[key] for key in HIT_FIELDS if key in hit}
        for hit in payload.get("results") or []
        if isinstance(hit, dict)
    ]
    return out


def search_materials(
    api: ApiClient,
    *,
    q: str | None = None,
    category: str | None = None,
    brand: str | None = None,
    supplier: str | None = None,
    attrs: dict[str, Any] | None = None,
    measures: dict[str, Any] | None = None,
    limit: int = 10,
    cursor: str | None = None,
    expand: str | None = None,
) -> dict[str, Any]:
    """Free-text or filtered search over the catalogue, one hit per family by default."""

    payload = api.get(
        "/v1/materials",
        {
            "q": q,
            "category": category,
            "brand": brand,
            "supplier": supplier,
            "attrs": attrs,
            "measures": measures,
            "limit": max(1, min(int(limit), MAX_SEARCH_LIMIT)),
            "cursor": cursor,
            "expand": expand,
        },
    )
    return _trim_hits(payload)


def get_material(api: ApiClient, *, uid: str, measures: str = "preferred") -> dict[str, Any]:
    """One full UMR v2 document by material_uid; `measures` is `preferred` or `all`."""

    return api.get(f"/v1/materials/{uid}", {"measures": measures})


def get_family(api: ApiClient, *, uid: str) -> dict[str, Any]:
    """The family a material belongs to and every member variant, by material_uid."""

    return api.get(f"/v1/materials/{uid}/family")


def list_facets(
    api: ApiClient,
    *,
    q: str | None = None,
    category: str | None = None,
    brand: str | None = None,
    supplier: str | None = None,
    attrs: dict[str, Any] | None = None,
    measures: dict[str, Any] | None = None,
    per_facet: int = 12,
) -> dict[str, Any]:
    """Value counts for the same filters as search_materials, to narrow a search step by step."""

    return api.get(
        "/v1/facets",
        {
            "q": q,
            "category": category,
            "brand": brand,
            "supplier": supplier,
            "attrs": attrs,
            "measures": measures,
            "per_facet": max(1, min(int(per_facet), 100)),
        },
    )


def suggest_materials(api: ApiClient, *, q: str, limit: int = 5) -> dict[str, Any]:
    """Spelling suggestions for a query that found nothing or looks mistyped."""

    return api.get("/v1/suggest", {"q": q, "limit": max(1, min(int(limit), MAX_SEARCH_LIMIT))})


def list_changes(api: ApiClient, *, since: int = 0, limit: int = 100) -> dict[str, Any]:
    """Records changed after a sync_seq cursor, for keeping a local copy in step."""

    return api.get(
        "/v1/changes",
        {"since": max(0, int(since)), "limit": max(1, min(int(limit), MAX_CHANGES_LIMIT))},
    )


def catalogue_info(api: ApiClient) -> dict[str, Any]:
    """Catalogue size, highest sync_seq and the schema version the API speaks."""

    return api.get("/v1/meta")


# name -> (function, short title). The server registers exactly this list; the tests assert it.
SPECS: tuple[tuple[str, Callable[..., dict[str, Any]], str], ...] = (
    ("search_materials", search_materials, "Search the material catalogue"),
    ("get_material", get_material, "Open one material document"),
    ("get_family", get_family, "List a family and its variants"),
    ("list_facets", list_facets, "Count facet values"),
    ("suggest_materials", suggest_materials, "Suggest a spelling"),
    ("list_changes", list_changes, "Poll the change feed"),
    ("catalogue_info", catalogue_info, "Describe the catalogue"),
)

TOOL_NAMES = tuple(name for name, _, _ in SPECS)
