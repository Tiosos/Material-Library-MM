"""HTTP contract for the five write-back review routes (T3, step 4).

Design: docs/t3-writeback.md section 10.

The api job's databases carry the evaluation corpus and the supplier demo but
NOT the joineryflow snapshot, so these tests create one isolated material row
with a valid joineryflow source_id and delete it afterwards. No assertion here
depends on the number of requests other suites created.

The routes are read/review only: there is deliberately no apply route (D1).
Integration of the worker itself lives in tests/test_writeback.py and runs in
the writeback CI job with ML_WRITEBACK_DSN.
"""
from __future__ import annotations

import uuid
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _uid() -> str:
    raw = uuid.uuid4().hex.upper()
    body = "".join(ch for ch in raw if ch in ALPHABET)[:26]
    return "mat_" + (body + "Z" * 26)[:26]


def assert_error(response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}, body
    assert body["error"]["code"] == code, body
    assert body["error"]["message"], body
    return body["error"]


@pytest.fixture(autouse=True)
def writeback_schema(conn) -> Iterator[None]:
    count = conn.execute(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name='material_change_request' AND column_name='base'"
    ).fetchone()[0]
    if not count:
        pytest.skip("schema/writeback.sql is not loaded; the api job adds it")
    conn.execute("DELETE FROM material_event WHERE entity='material_change_request'")
    conn.execute("DELETE FROM material_change_request")
    # Self-heal: a previous interrupted run may have left its disposable
    # material behind (hard delete is gated by ml.allow_delete). Those rows
    # would otherwise be picked up by the duplicate scan in the api job.
    conn.execute("SELECT set_config('ml.allow_delete', 'on', false)")
    try:
        conn.execute(
            "DELETE FROM material_event WHERE material_uid IN "
            "(SELECT material_uid FROM material WHERE name='T3 HTTP contract board')"
        )
        conn.execute("DELETE FROM material WHERE name='T3 HTTP contract board'")
    finally:
        conn.execute("SELECT set_config('ml.allow_delete', 'off', false)")
    yield


@pytest.fixture
def jf_uid(conn) -> Iterator[str]:
    """One disposable joineryflow-shaped material, invisible to other suites."""
    uid = _uid()
    conn.execute(
        """
        INSERT INTO material
            (material_uid, category_code, name, unit_cost, source_system, source_id)
        SELECT %s, category_code, 'T3 HTTP contract board', 45.00,
               'joineryflow', 'board_materials:9901'
          FROM material WHERE status='active' AND unit_cost IS NOT NULL LIMIT 1
        """,
        (uid,),
    )
    yield uid
    # material has a BEFORE DELETE trigger that forbids hard deletes unless
    # maintenance explicitly opts in. Open the switch for this one row only.
    conn.execute("SELECT set_config('ml.allow_delete', 'on', false)")
    try:
        conn.execute("DELETE FROM material_event WHERE material_uid=%s", (uid,))
        conn.execute("DELETE FROM material WHERE material_uid=%s", (uid,))
    finally:
        conn.execute("SELECT set_config('ml.allow_delete', 'off', false)")


def _submit(client: TestClient, uid: str, *, after: Any = 99.5,
            actor: str = "alice", path: str = "unit_cost"):
    return client.post(
        "/v1/change-requests",
        json={"material_uid": uid, "diffs": [{"path": path, "after": after}]},
        headers={"X-Actor": actor},
    )


def test_submit_returns_201_with_a_server_computed_base(client: TestClient, jf_uid: str):
    response = _submit(client, jf_uid)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["schema_version"] == "2"
    assert body["status"] == "pending"
    assert body["material_uid"] == jf_uid
    assert float(body["diffs"][0]["before"]) == 45.0
    assert body["diffs"][0]["after"] == 99.5
    assert float(body["base"]["values"]["unit_cost"]) == 45.0
    assert isinstance(body["id"], int)


def test_submit_maps_bad_input_to_400(client: TestClient, jf_uid: str):
    forbidden = client.post(
        "/v1/change-requests",
        json={"material_uid": jf_uid, "diffs": [{"path": "archived_at", "after": None}]},
    )
    error = assert_error(forbidden, 400, "bad_request")
    assert error["detail"]["path"] == "archived_at"

    unknown = client.post(
        "/v1/change-requests",
        json={"material_uid": _uid(), "diffs": [{"path": "unit_cost", "after": 1}]},
    )
    assert_error(unknown, 400, "bad_request")

    empty = client.post("/v1/change-requests",
                        json={"material_uid": jf_uid, "diffs": []})
    assert_error(empty, 400, "bad_request")

    duplicate = client.post(
        "/v1/change-requests",
        json={"material_uid": jf_uid, "diffs": [
            {"path": "unit_cost", "after": 1},
            {"path": "unit_cost", "after": 2},
        ]},
    )
    assert_error(duplicate, 400, "bad_request")


def test_open_request_is_a_409_conflict(client: TestClient, jf_uid: str):
    assert _submit(client, jf_uid).status_code == 201
    assert_error(_submit(client, jf_uid, after=100), 409, "conflict")


def test_list_defaults_to_actionable_and_pages(client: TestClient, jf_uid: str):
    _submit(client, jf_uid)
    _submit(client, jf_uid, after="T3 changed", path="product_type")

    body = client.get("/v1/change-requests").json()
    assert body["schema_version"] == "2" and body["status"] == "actionable"
    assert body["total"] == 2 and len(body["rows"]) == 2
    assert body["next_cursor"] is None

    first = client.get("/v1/change-requests", params={"limit": 1}).json()
    assert first["limit"] == 1 and len(first["rows"]) == 1
    assert first["next_cursor"]
    second = client.get("/v1/change-requests",
                        params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert second["offset"] == 1 and second["rows"][0]["id"] != first["rows"][0]["id"]

    assert client.get("/v1/change-requests",
                      params={"status": "applied"}).json()["total"] == 0
    assert_error(client.get("/v1/change-requests",
                            params={"status": "bogus"}), 400, "bad_request")
    assert_error(client.get("/v1/change-requests",
                            params={"limit": 0}), 400, "bad_request")


def test_detail_returns_a_safe_plan(client: TestClient, jf_uid: str):
    rid = _submit(client, jf_uid).json()["id"]
    body = client.get(f"/v1/change-requests/{rid}").json()
    assert body["schema_version"] == "2" and body["id"] == rid
    # no decision yet, and the key is present rather than missing
    assert body["decision_reason"] is None
    plan = body["plan"]
    assert plan["ok"] is True and plan["method"] == "PATCH"
    assert plan["url"] == "/catalog/board-materials/9901"
    assert plan["body"] == {"unit_cost": 99.5}
    assert plan["headers"]["Idempotency-Key"] == f"mlcr-{rid}"
    assert "Authorization" not in plan["headers"]
    assert not any(key in plan["body"] for key in
                   ("archived_at", "status", "material_uid", "source_system"))


def test_detail_unknown_id_is_404(client: TestClient):
    assert_error(client.get("/v1/change-requests/99999999"), 404, "not_found")


def test_the_decision_reason_is_read_from_the_event(client: TestClient, jf_uid: str):
    """decide() stores the reason in material_event, not in a column."""
    rid = _submit(client, jf_uid, actor="alice").json()["id"]
    assert client.get(f"/v1/change-requests/{rid}").json()["decision_reason"] is None

    client.post(f"/v1/change-requests/{rid}/approve",
                json={"reason": "checked against the supplier list"},
                headers={"X-Actor": "bob"})

    detail = client.get(f"/v1/change-requests/{rid}").json()
    assert detail["decision_reason"] == "checked against the supplier list"
    assert detail["decided_by"] == "bob" and detail["decided_at"]

    listed = client.get("/v1/change-requests", params={"status": "approved"}).json()
    row = next(r for r in listed["rows"] if r["id"] == rid)
    assert row["decision_reason"] == detail["decision_reason"]


def test_submit_refuses_a_blank_or_oversized_text_value(client: TestClient, jf_uid: str):
    blank = client.post(
        "/v1/change-requests",
        json={"material_uid": jf_uid, "diffs": [{"path": "name", "after": "\n\t "}]},
    )
    assert_error(blank, 400, "bad_request")

    oversized = client.post(
        "/v1/change-requests",
        json={"material_uid": jf_uid, "diffs": [{"path": "name", "after": "a" * 1001}]},
    )
    assert_error(oversized, 400, "bad_request")

    # product_url is exempt from the blank rule: it clears the field instead
    blank_url = client.post(
        "/v1/change-requests",
        json={"material_uid": jf_uid, "diffs": [{"path": "product_url", "after": ""}]},
    )
    assert blank_url.status_code == 201, blank_url.text


def test_approve_requires_a_different_actor(client: TestClient, jf_uid: str):
    rid = _submit(client, jf_uid, actor="alice").json()["id"]
    same = client.post(f"/v1/change-requests/{rid}/approve", json={},
                       headers={"X-Actor": "alice"})
    assert_error(same, 409, "conflict")

    ok = client.post(f"/v1/change-requests/{rid}/approve", json={"reason": "looks right"},
                     headers={"X-Actor": "bob"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "approved" and ok.json()["schema_version"] == "2"


def test_approve_refuses_a_stale_base(client: TestClient, conn, jf_uid: str):
    rid = _submit(client, jf_uid).json()["id"]
    conn.execute("UPDATE material SET unit_cost = 777 WHERE material_uid=%s", (jf_uid,))
    error = assert_error(
        client.post(f"/v1/change-requests/{rid}/approve", json={},
                    headers={"X-Actor": "bob"}), 409, "conflict")
    assert "base" in error["detail"] and "current" in error["detail"]


def test_approve_refuses_a_merged_away_material(client: TestClient, conn, jf_uid: str):
    """T3 x T2: a tombstone keeps its field values, so only the status check saves us."""
    rid = _submit(client, jf_uid).json()["id"]
    conn.execute("UPDATE material SET status='superseded' WHERE material_uid=%s", (jf_uid,))
    error = assert_error(
        client.post(f"/v1/change-requests/{rid}/approve", json={},
                    headers={"X-Actor": "bob"}), 409, "conflict")
    assert error["detail"]["material_status"] == "superseded"


def test_reject_is_terminal(client: TestClient, jf_uid: str):
    rid = _submit(client, jf_uid).json()["id"]
    ok = client.post(f"/v1/change-requests/{rid}/reject", json={"reason": "no"},
                     headers={"X-Actor": "bob"})
    assert ok.status_code == 200 and ok.json()["status"] == "rejected"
    assert_error(
        client.post(f"/v1/change-requests/{rid}/approve", json={},
                    headers={"X-Actor": "bob"}), 409, "conflict")


def test_decide_unknown_id_is_404(client: TestClient):
    assert_error(client.post("/v1/change-requests/99999999/approve", json={},
                             headers={"X-Actor": "bob"}), 404, "not_found")
    assert_error(client.post("/v1/change-requests/99999999/reject", json={},
                             headers={"X-Actor": "bob"}), 404, "not_found")


def test_there_is_no_apply_route(client: TestClient, jf_uid: str):
    rid = _submit(client, jf_uid).json()["id"]
    response = client.post(f"/v1/change-requests/{rid}/apply", json={})
    assert response.status_code in (404, 405), response.text


# ---------------------------------------------------------------------------
# Batch submit and batch decide (docs/ui-bulk-edit.md section 6.2, U15 / U16)
# ---------------------------------------------------------------------------

def _batch_item(uid: str, path: str = "unit_cost", after: Any = 301) -> dict[str, Any]:
    return {"material_uid": uid, "diffs": [{"path": path, "after": after}]}


def test_batch_submit_is_a_partial_success(client: TestClient, jf_uid: str) -> None:
    body = {
        "target_system": "joineryflow",
        "reason": "Q4 supplier list",
        "materials": [
            _batch_item(jf_uid, after=401),
            _batch_item("mat_does_not_exist", after=402),
            _batch_item(jf_uid, "name", "\n\n"),   # 22023 at SQL, not a 500
        ],
    }
    response = client.post("/v1/change-requests/batch", json=body,
                           headers={"X-Actor": "alice"})
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["submitted"] == 1 and report["failed"] == 2
    first, missing, blank = report["results"]
    assert first["ok"] is True and first["status"] == "pending" and first["id"]
    assert missing["ok"] is False and missing["code"] == "not_found"
    assert blank["ok"] is False and blank["code"] == "bad_request"
    assert "name must be a non-empty string" in blank["detail"]["message"]

    # the one good row is really pending, with the batch reason on it
    detail = client.get(f"/v1/change-requests/{first['id']}").json()
    assert detail["reason"] == "Q4 supplier list"
    assert detail["requested_by"] == "alice"


def test_batch_submit_per_item_reason_wins(client: TestClient, jf_uid: str) -> None:
    response = client.post(
        "/v1/change-requests/batch",
        json={"reason": "batch reason", "materials": [
            _batch_item(jf_uid, after=501),
            {**_batch_item(jf_uid, "product_type", "board"), "reason": "item reason"},
        ]},
        headers={"X-Actor": "alice"},
    )
    assert response.status_code == 200, response.text
    report = response.json()
    # both rows target a different path, so both land
    assert report["submitted"] == 2, report
    ids = [row["id"] for row in report["results"]]
    reasons = [client.get(f"/v1/change-requests/{rid}").json()["reason"] for rid in ids]
    assert reasons == ["batch reason", "item reason"]


def test_batch_submit_rejects_bad_batches_before_writing(client: TestClient, jf_uid: str) -> None:
    one = _batch_item(jf_uid)
    cases = [
        ("batch_too_large", {"reason": "x", "materials": [one] * 101}, 100),
        ("reason_required", {"materials": [one] * 6}, 5),
        ("reason_too_long", {"reason": "x" * 501, "materials": [one]}, 500),
    ]
    for code, body, limit in cases:
        error = assert_error(client.post("/v1/change-requests/batch", json=body),
                             400, code)
        assert error["detail"]["limit"] if "limit" in error["detail"] else True
        assert error["detail"].get("limit", error["detail"].get("threshold")) == limit
    assert client.get("/v1/change-requests", params={"status": "all"}).json()["total"] == 0

    # exactly at the reason threshold is allowed. Five different paths, because
    # a second request on the same path is an open_request by design.
    five = [
        _batch_item(jf_uid, "name", "Five field batch"),
        _batch_item(jf_uid, "product_type", "board"),
        _batch_item(jf_uid, "unit_cost", 700),
        _batch_item(jf_uid, "product_url", "https://example.test/five"),
        _batch_item(jf_uid, "synonyms", ["one", "two"]),
    ]
    ok = client.post("/v1/change-requests/batch",
                     json={"reason": "x", "materials": five})
    assert ok.status_code == 200 and ok.json()["submitted"] == 5, ok.text


def test_batch_decide_reports_each_decision(client: TestClient, jf_uid: str) -> None:
    ids = []
    for path, after in (("unit_cost", 601), ("product_type", "board")):
        created = client.post("/v1/change-requests/batch",
                              json={"reason": "batch",
                                    "materials": [_batch_item(jf_uid, path, after)]},
                              headers={"X-Actor": "alice"})
        ids.append(created.json()["results"][0]["id"])
    # one row was filed by the same actor who will decide -> self_approval
    response = client.post(
        "/v1/change-requests/decide-batch",
        json={"decision": "approve", "ids": ids + [ids[0]], "reason": "checked against the PDF"},
        headers={"X-Actor": "bob"},
    )
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["decided"] == 2 and report["failed"] == 1, report
    first, second, dup = report["results"]
    assert first["ok"] is True and first["status"] == "approved"
    assert first["decided_by"] == "bob" and first["decided_at"]
    assert second["ok"] is True and second["decided_at"]
    assert dup["ok"] is False and dup["code"] == "already_decided"
    # the per-row timestamps are real column values, not a shared batch stamp.
    # The detail route serialises the datetime as +00:00 and the batch route as
    # Z, so compare the instants rather than the spellings.
    from datetime import datetime
    parse = lambda text: datetime.fromisoformat(text.replace("Z", "+00:00"))
    stored = [client.get(f"/v1/change-requests/{rid}").json()["decided_at"] for rid in ids]
    assert [parse(x) for x in stored] == [parse(first["decided_at"]),
                                         parse(second["decided_at"])]
    assert parse(first["decided_at"]) != parse(second["decided_at"])


def test_batch_decide_requires_a_reason_and_a_valid_cap(client: TestClient) -> None:
    error = assert_error(client.post("/v1/change-requests/decide-batch",
                                     json={"decision": "approve", "ids": [1]}),
                         400, "reason_required")
    assert_error(client.post("/v1/change-requests/decide-batch",
                             json={"decision": "approve", "ids": list(range(1, 27)),
                                   "reason": "x"}), 400, "batch_too_large")
    assert_error(client.post("/v1/change-requests/decide-batch",
                             json={"decision": "maybe", "ids": [1], "reason": "x"}),
                 400, "bad_request")
    assert_error(client.post("/v1/change-requests/decide-batch",
                             json={"decision": "reject", "ids": [1], "reason": "x" * 501}),
                 400, "reason_too_long")
