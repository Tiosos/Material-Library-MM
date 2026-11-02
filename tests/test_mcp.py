"""The MCP server's own suite (docs/mcp-server.md 10).

The server is exercised through a real MCP client session - in-memory transport, same SDK code
path a host uses - while the API behind it runs in-process through an ASGI transport. No port is
opened and no network is touched, so this runs in the same CI job as the API tests.

The point of these tests is the seam, not the SQL: the API suite already proves the payloads, and
the contract files prove the SQL. Here we prove that each tool reaches its route, that trimming and
capping happen, and that an API failure arrives as a tool error carrying the API's own words.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Iterator

import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from api.main import app
from mcp_server import tools
from mcp_server.client import ApiClient
from mcp_server.config import Settings
from mcp_server.server import build_server


class InProcessTransport(httpx2.BaseTransport):
    """Run `ApiClient` requests against the FastAPI app without a socket.

    `httpx2.ASGITransport` is async-only and `ApiClient` is sync (the MCP SDK runs sync tool bodies
    in a worker thread), so the bridge is a `TestClient`, which owns the app's lifespan and the
    database pool.
    """

    def __init__(self, client: TestClient) -> None:
        self._client = client

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        response = self._client.request(
            request.method,
            request.url.raw_path.decode(),
            headers=[(key.decode(), value.decode()) for key, value in request.headers.raw],
            content=request.read(),
        )
        return httpx2.Response(
            response.status_code, headers=response.headers, content=response.content, request=request
        )


@pytest.fixture(scope="module")
def api(client: TestClient) -> ApiClient:
    """The real `ApiClient`, pointed at the in-process app."""

    api_client = ApiClient(Settings(base_url="http://api.local"), transport=InProcessTransport(client))
    yield api_client
    api_client.close()


class Session:
    """A connected MCP client session, driven from the test thread.

    The SDK's session opens an anyio cancel scope, which must be entered and exited in the same
    task, so the session lives in one long-running task on its own loop instead of being created
    per call with `asyncio.run` (that raises "Attempted to exit cancel scope in a different task").
    """

    def __init__(self, api: ApiClient) -> None:
        self._api = api
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._client: Client | None = None
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._task = asyncio.run_coroutine_threadsafe(self._hold(), self._loop)
        assert self._ready.wait(timeout=30), "the MCP session did not start"

    async def _hold(self) -> None:
        self._stop = asyncio.Event()
        async with Client(build_server(Settings(), api=self._api)) as client:
            self._client = client
            self._ready.set()
            await self._stop.wait()

    @property
    def client(self) -> Client:
        assert self._client is not None
        return self._client

    def run(self, coro: Any) -> Any:
        """Run one coroutine on the session's loop and return its result."""

        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=120)

    def close(self) -> None:
        async def stop() -> None:
            assert self._stop is not None
            self._stop.set()

        asyncio.run_coroutine_threadsafe(stop(), self._loop).result(timeout=30)
        self._task.result(timeout=30)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=30)


@pytest.fixture(scope="module")
def session(api: ApiClient) -> Iterator[Session]:
    """A connected MCP client session over the in-memory transport."""

    live = Session(api)
    try:
        yield live
    finally:
        live.close()


def call(session: Session, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call one tool and decode its JSON text payload."""

    result = session.run(session.client.call_tool(name, arguments or {}))
    assert not result.is_error, f"{name} failed: {result.content[0].text[:300]}"
    return json.loads(result.content[0].text)


def call_error(session: Session, name: str, arguments: dict[str, Any] | None = None) -> str:
    """Call one tool expecting an anticipated failure; return the message the model would see."""

    result = session.run(session.client.call_tool(name, arguments or {}))
    assert result.is_error, f"{name} should have failed"
    return result.content[0].text


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def test_registers_exactly_the_seven_tools(session: Session) -> None:
    listed = session.run(session.client.list_tools()).tools
    assert [tool.name for tool in listed] == list(tools.TOOL_NAMES)


def test_every_tool_has_a_description_a_model_can_use(session: Session) -> None:
    for tool in session.run(session.client.list_tools()).tools:
        description = tool.description or ""
        assert 60 <= len(description) <= 400, f"{tool.name} description is {len(description)} chars"


def test_input_caps_are_in_the_schema(session: Session) -> None:
    listed = session.run(session.client.list_tools()).tools
    schemas = {tool.name: tool.input_schema for tool in listed}
    search = schemas["search_materials"]["properties"]
    assert search["limit"]["maximum"] == tools.MAX_SEARCH_LIMIT
    assert search["expand"]["anyOf"][0] == {"const": "variants", "type": "string"}
    assert schemas["list_changes"]["properties"]["limit"]["maximum"] == tools.MAX_CHANGES_LIMIT


def test_a_call_above_the_cap_is_rejected_before_it_reaches_the_api(session: Session) -> None:
    message = call_error(session, "search_materials", {"limit": tools.MAX_SEARCH_LIMIT + 1})
    assert "less than or equal to" in message


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------
def test_search_returns_the_envelope_and_trimmed_hits(session: Session) -> None:
    payload = call(session, "search_materials", {"q": "laminex", "limit": 2})
    assert payload["total"] >= 1
    assert payload["level"] in {"and", "prefix", "exact", "relaxed", "fuzzy", "browse"}
    assert payload["collapsed"] is True
    assert len(payload["results"]) <= 2
    for hit in payload["results"]:
        assert set(hit) == set(tools.HIT_FIELDS), hit


def test_search_logs_the_call_and_returns_its_id(session: Session) -> None:
    payload = call(session, "search_materials", {"q": "hinge", "limit": 1})
    assert isinstance(payload["log_id"], int) and payload["log_id"] > 0


def test_search_reports_nothing_found_as_an_answer(session: Session) -> None:
    payload = call(session, "search_materials", {"q": "zzzzzzzz"})
    assert payload["total"] == 0
    assert payload["level"] == "none"
    assert payload["results"] == []


def test_attrs_and_measures_are_objects(session: Session) -> None:
    measures = {"thickness_mm": {"gte": 17.5, "lte": 18.5, "unit": "mm"}}
    payload = call(session, "search_materials", {"measures": measures, "limit": 50})
    assert payload["total"] >= 1
    for hit in payload["results"]:
        assert hit["material_uid"].startswith("mat_")


def test_a_convertible_unit_with_no_matching_values_is_an_empty_page(session: Session) -> None:
    measures = {"thickness_mm": {"gte": 17.5, "lte": 18.5, "unit": "kg"}}
    payload = call(session, "search_materials", {"measures": measures})
    assert payload["total"] == 0


def test_a_malformed_measure_is_a_tool_error_with_the_api_sentence(session: Session) -> None:
    measures = {"thickness_mm": {"gte": 17.5, "lte": 18.5, "unit": "furlong"}}
    message = call_error(session, "search_materials", {"measures": measures})
    assert "bad_request" in message
    assert 'unknown unit "furlong"' in message
    assert "CONTEXT" not in message


def test_get_material_returns_a_schema_valid_document(
    session: Session, material_schema: dict[str, Any], conn: Any
) -> None:
    import jsonschema

    uid = conn.execute(
        "SELECT material_uid FROM material WHERE status = 'active' ORDER BY material_uid LIMIT 1"
    ).fetchone()[0]
    document = call(session, "get_material", {"uid": uid})
    jsonschema.validate(document, material_schema)
    assert document["material_uid"] == uid


def test_get_material_can_return_every_measure(session: Session, conn: Any) -> None:
    uid = conn.execute(
        """
        SELECT m.material_uid FROM material m
        JOIN material_measure x ON x.material_uid = m.material_uid
        WHERE NOT x.is_preferred AND m.status = 'active'
        ORDER BY m.material_uid LIMIT 1
        """
    ).fetchone()
    if uid is None:
        pytest.skip("no material carries a non-preferred measure")
    preferred = call(session, "get_material", {"uid": uid[0]})
    everything = call(session, "get_material", {"uid": uid[0], "measures": "all"})
    assert len(everything["measures"]) > len(preferred["measures"])


def test_unknown_uid_is_a_not_found_tool_error(session: Session) -> None:
    message = call_error(session, "get_material", {"uid": "mat_doesnotexist"})
    assert "not_found" in message


def test_get_family_lists_the_members(session: Session, conn: Any) -> None:
    row = conn.execute(
        """
        SELECT family_id FROM material
        WHERE family_id IS NOT NULL AND status = 'active'
        GROUP BY family_id HAVING count(*) > 1
        ORDER BY family_id LIMIT 1
        """
    ).fetchone()
    if row is None:
        pytest.skip("the database has no multi-member family")
    uid = conn.execute(
        "SELECT material_uid FROM material WHERE family_id = %s ORDER BY material_uid LIMIT 1",
        (row[0],),
    ).fetchone()[0]
    payload = call(session, "get_family", {"uid": uid})
    assert payload["family"]["family_id"] == row[0]
    assert {member["material_uid"] for member in payload["members"]} >= {uid}


def test_list_facets_counts_values_for_the_same_filters(session: Session) -> None:
    payload = call(session, "list_facets", {"q": "laminex"})
    assert payload["facets"], payload
    for facet in payload["facets"]:
        assert set(facet) == {"facet", "value", "count"}
        assert facet["count"] >= 1


def test_suggest_materials_finds_a_spelling(session: Session) -> None:
    payload = call(session, "suggest_materials", {"q": "laminx"})
    assert any(row["value"].lower() == "laminex" for row in payload["suggestions"])


def test_list_changes_returns_a_cursor_to_poll_from(session: Session) -> None:
    payload = call(session, "list_changes", {"since": 0, "limit": 2})
    assert len(payload["changes"]) <= 2
    assert payload["next_since"] >= 0
    assert payload["total_available"] >= len(payload["changes"])


def test_catalogue_info_matches_the_database(session: Session, conn: Any) -> None:
    payload = call(session, "catalogue_info")
    assert payload["materials"] == conn.execute("SELECT count(*) FROM material").fetchone()[0]
    assert payload["schema_version"] == "2"


# ---------------------------------------------------------------------------
# Resource, token, failures
# ---------------------------------------------------------------------------
def test_the_resource_template_is_registered(session: Session) -> None:
    templates = session.run(session.client.list_resource_templates()).resource_templates
    assert [template.uri_template for template in templates] == ["materials://{uid}"]


def test_the_resource_returns_the_same_document_as_the_tool(session: Session, conn: Any) -> None:
    uid = conn.execute(
        "SELECT material_uid FROM material WHERE status = 'active' ORDER BY material_uid LIMIT 1"
    ).fetchone()[0]
    contents = session.run(session.client.read_resource(f"materials://{uid}")).contents
    assert len(contents) == 1
    assert contents[0].mime_type == "application/json"
    assert json.loads(contents[0].text) == call(session, "get_material", {"uid": uid})


def test_the_resource_reports_a_missing_material(session: Session) -> None:
    with pytest.raises(Exception) as caught:
        session.run(session.client.read_resource("materials://mat_doesnotexist"))
    assert "not_found" in str(caught.value)


def test_a_token_protected_api_is_honoured(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(app.state.config, "token", "secret")
    message = call_error(session, "catalogue_info")
    assert "unauthorized" in message


def test_an_unreachable_api_is_a_tool_error_not_a_traceback() -> None:
    """A dead API must read as `unavailable`, not as a stack trace in the model's context."""

    settings = Settings(base_url="http://127.0.0.1:1", timeout=1.0)
    api_client = ApiClient(settings)
    live = Session(api_client)
    try:
        message = call_error(live, "catalogue_info")
    finally:
        live.close()
        api_client.close()
    assert "unavailable" in message
    assert "Traceback" not in message
