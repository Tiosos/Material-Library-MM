"""HTTP smoke tests for the read API (docs/api-v1.md 11).

Every assertion here is something a client would notice: the shape of an error, the meaning of
a cursor, whether an exported document validates. Run with ML_DSN pointing at a prepared
database, e.g. `ML_DSN=postgresql://postgres@127.0.0.1:55432/ml_test pytest -q tests`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from fastapi.testclient import TestClient


def active_count(conn) -> int:
    return conn.execute("SELECT count(*) FROM material WHERE status NOT IN ('archived','superseded')").fetchone()[0]


def group_count(conn) -> int:
    """Families collapse to one hit, so the default total counts live groups, not rows."""
    return conn.execute(
        "SELECT count(DISTINCT coalesce(family_id, material_uid)) FROM material "
        "WHERE status NOT IN ('archived','superseded')"
    ).fetchone()[0]


def assert_error(response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}, body
    assert body["error"]["code"] == code, body
    assert body["error"]["message"], body
    return body["error"]


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_meta(client: TestClient, conn) -> None:
    body = client.get("/v1/meta").json()
    assert body["schema_version"] == "2"
    assert body["materials"] == conn.execute("SELECT count(*) FROM material").fetchone()[0]
    assert body["max_sync_seq"] >= 1


def test_browse_total_is_truthful(client: TestClient, conn) -> None:
    body = client.get("/v1/materials", params={"limit": 1}).json()
    assert body["level"] == "browse"
    assert body["total"] == group_count(conn)
    assert body["collapsed"] is True
    assert len(body["results"]) == 1
    assert body["next_cursor"] is not None

    expanded = client.get("/v1/materials", params={"limit": 1, "expand": "variants"}).json()
    assert expanded["collapsed"] is False
    assert expanded["total"] == active_count(conn)


def test_collapsed_family_hit_lists_its_variants(client: TestClient, conn) -> None:
    family_uid = conn.execute(
        "SELECT material_uid FROM material WHERE family_id IS NOT NULL "
        "AND status NOT IN ('archived','superseded') ORDER BY material_uid LIMIT 1"
    ).fetchone()
    if family_uid is None:
        pytest.skip("no live family in this database")
    family_id = conn.execute(
        "SELECT family_id FROM material WHERE material_uid = %s", (family_uid[0],)
    ).fetchone()[0]
    # The family is found through its own name so the assertion does not depend on ranking.
    name = conn.execute("SELECT name FROM material WHERE material_uid = %s", (family_uid[0],)).fetchone()[0]
    hits = client.get("/v1/materials", params={"q": name, "limit": 50}).json()
    hit = next((item for item in hits["results"] if item["family_id"] == family_id), None)
    assert hit is not None, hits
    expanded = client.get(
        "/v1/materials", params={"q": name, "limit": 50, "expand": "variants"}
    ).json()
    matched = {item["material_uid"] for item in expanded["results"] if item["family_id"] == family_id}
    # variants lists the other members that matched this query, not the whole family (docs/api-v1.md 3.4).
    assert hit["variant_count"] == len(matched)
    assert set(hit["variants"]) == matched - {hit["material_uid"]}
    assert hit["material_uid"] not in hit["variants"]


def test_paging_visits_every_row_once(client: TestClient) -> None:
    """Expanded paging still walks every material record exactly once."""
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 7, "expand": "variants"}
        if cursor:
            params["cursor"] = cursor
        body = client.get("/v1/materials", params=params).json()
        assert len(body["results"]) <= 7
        seen.extend(item["material_uid"] for item in body["results"])
        pages += 1
        cursor = body["next_cursor"]
        if not cursor:
            break
        assert pages < 100, "paging did not terminate"
    assert len(seen) == len(set(seen)) == body["total"], (len(seen), len(set(seen)), body["total"])


def test_paging_visits_every_group_once(client: TestClient, conn) -> None:
    """The default (collapsed) walk is one representative per family and no short page."""
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 7}
        if cursor:
            params["cursor"] = cursor
        body = client.get("/v1/materials", params=params).json()
        assert len(body["results"]) <= 7
        seen.extend(item["material_uid"] for item in body["results"])
        pages += 1
        cursor = body["next_cursor"]
        if not cursor:
            break
        assert pages < 100, "paging did not terminate"
    assert len(seen) == len(set(seen)) == group_count(conn), (len(seen), len(set(seen)), body["total"])


def test_bad_cursor_is_400(client: TestClient) -> None:
    assert_error(client.get("/v1/materials", params={"cursor": "not-a-cursor"}), 400, "bad_request")
    assert_error(client.get("/v1/materials", params={"cursor": "!!!!"}), 400, "bad_request")


def test_bad_attrs_and_measures_are_400(client: TestClient) -> None:
    assert_error(client.get("/v1/materials", params={"attrs": "nope"}), 400, "bad_request")
    assert_error(client.get("/v1/materials", params={"attrs": "[1,2]"}), 400, "bad_request")
    error = assert_error(
        client.get("/v1/materials", params={"measures": '{"thickness_mm": {"gte": 18}}'}),
        400,
        "bad_request",
    )
    assert "unit" in error["message"]
    assert "CONTEXT" not in error["message"], "PL/pgSQL internals leaked to the client"


def test_level_names(client: TestClient) -> None:
    assert client.get("/v1/materials", params={"q": "laminex"}).json()["level"] in {"and", "prefix", "relaxed", "fuzzy"}
    exact = client.get("/v1/materials", params={"q": "71B3590"}).json()
    if exact["total"]:
        assert exact["level"] == "exact"
    none = client.get("/v1/materials", params={"q": "zzzz qqqq"}).json()
    assert none["level"] == "none"
    assert none["total"] == 0
    assert none["results"] == []


def test_detail_document_matches_schema(client: TestClient, conn, material_schema: dict[str, Any]) -> None:
    # Acceptance criterion 2 (docs/api-v1.md 11): EVERY row, asserted by a loop, not by eye.
    # The fixture + sample databases here have ~109 rows, so the full walk costs a second.
    uids = [row[0] for row in conn.execute("SELECT material_uid FROM material ORDER BY material_uid")]
    assert len(uids) > 50, "the database under test is too small to prove anything"
    validator = jsonschema.Draft202012Validator(material_schema)
    for uid in uids:
        response = client.get(f"/v1/materials/{uid}")
        assert response.status_code == 200, (uid, response.text)
        doc = response.json()
        validator.validate(doc)
        assert doc["material_uid"] == uid
        assert doc["schema_version"] == "2"


def test_detail_unknown_uid_is_404(client: TestClient) -> None:
    error = assert_error(client.get("/v1/materials/mat_doesnotexist"), 404, "not_found")
    assert error["detail"]["uid"] == "mat_doesnotexist"


def test_detail_measures_switch(client: TestClient, conn) -> None:
    row = conn.execute(
        """
        SELECT material_uid
          FROM material_measure
         GROUP BY material_uid
        HAVING count(*) > count(*) FILTER (WHERE is_preferred)
         ORDER BY material_uid
         LIMIT 1
        """
    ).fetchone()
    if row is None:
        pytest.skip("no material has a non-preferred measure in this database")
    uid = row[0]
    preferred = client.get(f"/v1/materials/{uid}").json()
    every = client.get(f"/v1/materials/{uid}", params={"measures": "all"}).json()
    assert len(every["measures"]) > len(preferred["measures"])
    assert all(measure["is_preferred"] for measure in preferred["measures"])


def test_family(client: TestClient, conn) -> None:
    row = conn.execute(
        "SELECT material_uid FROM material WHERE family_id IS NOT NULL ORDER BY material_uid LIMIT 1"
    ).fetchone()
    if row is None:
        pytest.skip("no material belongs to a family in this database")
    body = client.get(f"/v1/materials/{row[0]}/family").json()
    assert body["family"]["family_id"]
    assert any(member["material_uid"] == row[0] for member in body["members"])


def test_family_absent_is_404(client: TestClient, conn) -> None:
    row = conn.execute(
        "SELECT material_uid FROM material WHERE family_id IS NULL ORDER BY material_uid LIMIT 1"
    ).fetchone()
    if row is None:
        pytest.skip("every material belongs to a family in this database")
    assert_error(client.get(f"/v1/materials/{row[0]}/family"), 404, "not_found")


def test_facets_match_total(client: TestClient) -> None:
    # Facets count material records, while the default page total counts collapsed groups.
    search = client.get("/v1/materials", params={"q": "laminex", "limit": 1, "expand": "variants"}).json()
    facets = client.get("/v1/facets", params={"q": "laminex"}).json()
    groups: dict[str, int] = {}
    for facet in facets["facets"]:
        groups[facet["facet"]] = groups.get(facet["facet"], 0) + facet["count"]
    assert groups, "facets returned nothing"
    for name, count in groups.items():
        assert count == search["total"], (name, count, search["total"])


def test_suggest(client: TestClient) -> None:
    body = client.get("/v1/suggest", params={"q": "laminx"}).json()
    values = [item["value"] for item in body["suggestions"]]
    assert any("laminex" in value.lower() for value in values), values


def test_changes_cursor_does_not_skip(client: TestClient, conn) -> None:
    total = conn.execute("SELECT count(*) FROM material").fetchone()[0]
    collected: list[int] = []
    since = 0
    for _ in range(total + 2):
        body = client.get("/v1/changes", params={"since": since, "limit": 3}).json()
        assert body["total_available"] >= len(body["changes"])
        received = [record["sync_seq"] for record in body["changes"]]
        assert received == sorted(received)
        collected.extend(received)
        if not received:
            assert body["next_since"] == since
            break
        assert body["next_since"] >= received[-1]
        since = body["next_since"]
    assert len(collected) == total, (len(collected), total)
    assert len(collected) == len(set(collected))


def test_export_jsonl(client: TestClient, conn, material_schema: dict[str, Any]) -> None:
    response = client.get("/v1/export", params={"format": "jsonl"})
    assert response.status_code == 200
    assert response.headers["x-schema-version"] == "2"
    lines = [line for line in response.text.splitlines() if line.strip()]
    assert len(lines) == int(response.headers["x-record-count"])
    expected = conn.execute("SELECT count(*) FROM material WHERE status <> 'archived'").fetchone()[0]
    assert len(lines) == expected
    validator = jsonschema.Draft202012Validator(material_schema)
    for line in lines[:5]:
        validator.validate(json.loads(line))
    assert {item["material_uid"] for item in map(json.loads, lines)} == {
        row[0] for row in conn.execute("SELECT material_uid FROM material WHERE status <> 'archived'")
    }


def test_export_json(client: TestClient, conn, export_schema: dict[str, Any]) -> None:
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    material_schema = json.loads((Path(__file__).resolve().parents[1] / "schema" / "material.schema.json").read_text(encoding="utf-8"))
    registry = Registry().with_resources(
        [
            (material_schema["$id"], Resource.from_contents(material_schema, default_specification=DRAFT202012)),
            (export_schema["$id"], Resource.from_contents(export_schema, default_specification=DRAFT202012)),
        ]
    )
    response = client.get("/v1/export", params={"format": "json"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    document = json.loads(response.text)
    jsonschema.Draft202012Validator(export_schema, registry=registry).validate(document)
    expected = conn.execute("SELECT count(*) FROM material WHERE status <> 'archived'").fetchone()[0]
    assert document["count"] == len(document["records"]) == expected
    assert document["schema_version"] == "2"
    assert document["exported_at"]


def test_export_csv(client: TestClient, export_columns: list[str]) -> None:
    response = client.get("/v1/export", params={"format": "csv"})
    assert response.status_code == 200
    rows = response.text.splitlines()
    assert rows[0].split(",") == export_columns
    assert len(rows) - 1 == int(response.headers["x-record-count"])


def test_export_include_archived(client: TestClient, conn) -> None:
    archived = conn.execute("SELECT count(*) FROM material WHERE status = 'archived'").fetchone()[0]
    default = client.get("/v1/export", params={"format": "jsonl"})
    every = client.get("/v1/export", params={"format": "jsonl", "include_archived": "true"})
    assert int(every.headers["x-record-count"]) - int(default.headers["x-record-count"]) == archived


def test_search_logging_and_clicks(client: TestClient, conn) -> None:
    before = conn.execute("SELECT count(*) FROM search_log").fetchone()[0]
    body = client.get("/v1/materials", params={"q": "laminex"}, headers={"X-Actor": "pytest"}).json()
    assert conn.execute("SELECT count(*) FROM search_log").fetchone()[0] == before + 1
    assert body["log_id"], body
    row = conn.execute(
        "SELECT q, level, total, actor FROM search_log WHERE id = %s", (body["log_id"],)
    ).fetchone()
    assert row[0] == "laminex" and row[1] == body["level"] and row[2] == body["total"] and row[3] == "pytest"

    uid = body["results"][0]["material_uid"]
    click = client.post(f"/v1/searches/{body['log_id']}/clicks", json={"material_uid": uid, "rank": 1})
    assert click.status_code == 202, click.text
    again = client.post(f"/v1/searches/{body['log_id']}/clicks", json={"material_uid": uid, "rank": 1})
    assert again.status_code == 202, again.text
    assert conn.execute("SELECT count(*) FROM search_click WHERE log_id = %s", (body["log_id"],)).fetchone()[0] == 1

    assert_error(client.post("/v1/searches/999999999/clicks", json={"material_uid": uid, "rank": 1}), 404, "not_found")
    assert_error(client.post(f"/v1/searches/{body['log_id']}/clicks", json={"material_uid": uid, "rank": 0}), 400, "bad_request")


def test_no_log_header(client: TestClient) -> None:
    before = client.get("/v1/materials", params={"q": "laminex"}).json()["log_id"]
    body = client.get("/v1/materials", params={"q": "laminex"}, headers={"X-No-Log": "1"}).json()
    assert body["log_id"] is None
    assert before is not None


def test_token_gate(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    config = client.app.state.config
    monkeypatch.setitem(config, "token", "secret-token")
    try:
        assert_error(client.get("/v1/materials"), 401, "unauthorized")
        ok = client.get("/v1/materials", headers={"Authorization": "Bearer secret-token"})
        assert ok.status_code == 200
    finally:
        config["token"] = ""
