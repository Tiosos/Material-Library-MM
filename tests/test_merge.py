"""HTTP contract for the duplicate review queue, merge and ignore (docs/t2-merge.md 10.3).

The merge tests cannot borrow the read-only session connection: the app owns its pooled
connections, so a transaction opened here would not be visible to the route under test.
They create a fresh pair with random uids instead and assert against the database, so the
suite stays re-runnable and no assertion depends on how many merges ran before it.
"""

from __future__ import annotations

import uuid
from typing import Any, Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient

from api.main import app

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _uid(salt: str) -> str:
    """A material uid the schema accepts: `mat_` + 26 Crockford base32 characters."""
    raw = uuid.uuid4().hex.upper()
    body = "".join(ch for ch in raw if ch in ALPHABET)[:23]
    body = (body + "Z" * 23)[:23]
    return f"mat_{salt}{body}"


@pytest.fixture()
def pair(conn: psycopg.Connection) -> Iterator[dict[str, Any]]:
    """Two near-identical boards in `board.veneer`, scanned into an open candidate row."""
    brand_id = conn.execute(
        "SELECT id FROM party WHERE canonical_name = 'Fixture Dup'"
    ).fetchone()
    if brand_id is None:
        pytest.skip("fixture data is not loaded (eval/fixture.sql)")
    keep = _uid("MRG")
    merge = _uid("MRG")
    salt = uuid.uuid4().hex[:6]
    label = f"Merge Probe {salt} Smoked Oak Panel"
    names = (label, f"{label} 22mm")
    with conn.transaction():
        for uid, name in zip((keep, merge), names):
            conn.execute(
                """
                INSERT INTO material (material_uid, category_code, name, brand_id, unit, unit_cost)
                VALUES (%s, 'board.veneer', %s, %s, 'sheet', 10.00)
                """,
                (uid, name, brand_id[0]),
            )
        # Scanning is a CLI action (decision D6), so the test performs it directly.
        conn.execute("SELECT material_duplicate_scan()")
    yield {
        "keep": keep,
        "merge": merge,
        "uid_a": min(keep, merge),
        "uid_b": max(keep, merge),
        "label": label,
    }


def test_queue_lists_the_open_pair(client: TestClient, pair: dict[str, Any]) -> None:
    body = client.get("/v1/duplicates", params={"status": "all", "limit": 100}).json()
    assert body["schema_version"] == "2"
    uids = {row["uid_a"] for row in body["rows"]} | {row["uid_b"] for row in body["rows"]}
    assert {pair["uid_a"], pair["uid_b"]} <= uids
    assert body["status"] == "all"
    assert body["limit"] == 100


def test_queue_rejects_an_unknown_status(client: TestClient) -> None:
    response = client.get("/v1/duplicates", params={"status": "nonsense"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_queue_cursor_pages_without_repeating(client: TestClient) -> None:
    first = client.get("/v1/duplicates", params={"status": "all", "limit": 1}).json()
    total = first["total"]
    seen = [(row["uid_a"], row["uid_b"]) for row in first["rows"]]
    cursor = first["next_cursor"]
    while cursor and len(seen) < total:
        page = client.get(
            "/v1/duplicates", params={"status": "all", "limit": 1, "cursor": cursor}
        ).json()
        seen.extend((row["uid_a"], row["uid_b"]) for row in page["rows"])
        cursor = page["next_cursor"]
    assert len(seen) == len(set(seen)), "cursor paging repeated a pair"


def test_preview_returns_a_plan_and_writes_nothing(
    client: TestClient, conn: psycopg.Connection, pair: dict[str, Any]
) -> None:
    before = conn.execute("SELECT count(*) FROM material_event").fetchone()[0]
    response = client.get(f"/v1/duplicates/{pair['uid_a']}/{pair['uid_b']}")
    assert response.status_code == 200
    body = response.json()
    assert body["keep"] in (pair["uid_a"], pair["uid_b"])
    assert body["suggestion"] in (pair["uid_a"], pair["uid_b"])
    assert body["can_merge"] is True
    assert isinstance(body["fields"], dict)
    assert isinstance(body["children"], dict)
    assert isinstance(body["warnings"], list)
    assert body["pair"]["uid_a"] == pair["uid_a"]
    after = conn.execute("SELECT count(*) FROM material_event").fetchone()[0]
    assert before == after


def test_preview_unknown_pair_is_404(client: TestClient) -> None:
    response = client.get("/v1/duplicates/mat_nope1/mat_nope2")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_merge_dry_run_changes_nothing(
    client: TestClient, conn: psycopg.Connection, pair: dict[str, Any]
) -> None:
    before = conn.execute(
        "SELECT count(*) FROM material WHERE status = 'superseded'"
    ).fetchone()[0]
    response = client.post(
        f"/v1/duplicates/{pair['uid_a']}/{pair['uid_b']}/merge",
        json={"keep": pair["keep"], "reason": "dry run", "dry_run": True},
        headers={"X-Actor": "pytest-merge"},
    )
    assert response.status_code == 200
    assert response.json()["code"] == "dry_run"
    after = conn.execute(
        "SELECT count(*) FROM material WHERE status = 'superseded'"
    ).fetchone()[0]
    assert before == after


def test_merge_supersedes_the_survivor(
    client: TestClient, conn: psycopg.Connection, pair: dict[str, Any]
) -> None:
    total_before = client.get("/v1/materials").json()["total"]
    response = client.post(
        f"/v1/duplicates/{pair['uid_a']}/{pair['uid_b']}/merge",
        json={"keep": pair["keep"], "reason": "same board twice"},
        headers={"X-Actor": "pytest-merge"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "merged"
    assert body["keep"] == pair["keep"]
    assert body["merged"] == pair["merge"]
    assert body["anchors"] == 2

    total_after = client.get("/v1/materials").json()["total"]
    assert total_after == total_before - 1

    state = conn.execute(
        "SELECT status, superseded_by FROM material WHERE material_uid = %s", (pair["merge"],)
    ).fetchone()
    assert state == ("superseded", pair["keep"])

    assert client.get(f"/v1/materials/{pair['merge']}").json()["status"] == "superseded"
    kept = client.get("/v1/materials", params={"q": pair["label"]}).json()
    uids = [row["material_uid"] for row in kept["results"]]
    assert pair["keep"] in uids
    assert pair["merge"] not in uids

    candidate = conn.execute(
        """
        SELECT status, keep_uid, decided_by FROM material_duplicate_candidate
         WHERE uid_a = %s AND uid_b = %s
        """,
        (pair["uid_a"], pair["uid_b"]),
    ).fetchone()
    assert candidate == ("merged", pair["keep"], "pytest-merge")


def test_merge_is_idempotent(
    client: TestClient, conn: psycopg.Connection, pair: dict[str, Any]
) -> None:
    client.post(
        f"/v1/duplicates/{pair['uid_a']}/{pair['uid_b']}/merge",
        json={"keep": pair["keep"]},
    )
    events = conn.execute(
        "SELECT count(*) FROM material_event WHERE entity = 'material_merge'"
    ).fetchone()[0]
    again = client.post(
        f"/v1/duplicates/{pair['uid_a']}/{pair['uid_b']}/merge",
        json={"keep": pair["keep"]},
    )
    assert again.status_code == 200
    assert again.json()["code"] == "already_merged"
    events_after = conn.execute(
        "SELECT count(*) FROM material_event WHERE entity = 'material_merge'"
    ).fetchone()[0]
    assert events == events_after


def test_merge_rejects_a_keeper_outside_the_pair(
    client: TestClient, pair: dict[str, Any]
) -> None:
    response = client.post(
        f"/v1/duplicates/{pair['uid_a']}/{pair['uid_b']}/merge",
        json={"keep": "mat_other00000000000000000000"},
    )
    assert response.status_code == 400
    body = response.json()["error"]
    assert body["code"] == "bad_request"
    assert "keep must be one of the two pair members" in body["message"]


def test_merge_of_an_archived_pair_is_409(
    client: TestClient, conn: psycopg.Connection
) -> None:
    archived = conn.execute(
        "SELECT material_uid FROM material WHERE material_uid = 'mat_FX000000000000000000DXPK04'"
    ).fetchone()
    live = "mat_FX000000000000000000DXPK03"
    if archived is None:
        pytest.skip("fixture data is not loaded (eval/fixture.sql)")
    response = client.post(
        f"/v1/duplicates/{live}/{archived[0]}/merge", json={"keep": live}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


def test_ignore_records_the_decision(
    client: TestClient, conn: psycopg.Connection
) -> None:
    a, b = "mat_FX000000000000000000DXPK05", "mat_FX000000000000000000DXPK06"
    response = client.post(
        f"/v1/duplicates/{a}/{b}/ignore",
        json={"reason": "different products"},
        headers={"X-Actor": "pytest-merge"},
    )
    assert response.status_code == 200
    assert response.json()["code"] == "ignored"
    row = conn.execute(
        """
        SELECT status, decided_by, note FROM material_duplicate_candidate
         WHERE uid_a = %s AND uid_b = %s
        """,
        (min(a, b), max(a, b)),
    ).fetchone()
    assert row[:2] == ("ignored", "pytest-merge")
    assert row[2] == "different products"

    conflict = client.post(f"/v1/duplicates/{a}/{b}/ignore", json={})
    assert conflict.status_code == 200


def test_ignore_of_an_unknown_pair_is_404(client: TestClient) -> None:
    response = client.post("/v1/duplicates/mat_nope1/mat_nope2/ignore", json={})
    assert response.status_code == 404


def test_duplicate_routes_need_the_token(
    client: TestClient, pair: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(app.state.config, "token", "secret")
    assert client.get("/v1/duplicates").status_code == 401
    assert (
        client.get("/v1/duplicates", headers={"Authorization": "Bearer secret"}).status_code
        == 200
    )
