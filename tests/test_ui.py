"""HTTP checks for the query UI shell (docs/ui-bulk-edit.md section 12).

The browser code has no unit harness (U11): no npm, no build step. What can be
checked from Python is checked here - the shell is served without a token, the
assets have the right content types, and the endpoint the page depends on agrees
with the tables it claims to mirror. A search round trip through the HTTP API is
asserted in test_api.py; this file only checks that the page can reach it.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "api" / "ui"

ALLOWED_EDITORS = {"text", "textarea", "number", "integer", "url", "tokens", "boolean", "select"}


@pytest.fixture(scope="module")
def shell() -> str:
    return (UI / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def script() -> str:
    return (UI / "app.js").read_text(encoding="utf-8")


def test_ui_shell_is_served_without_a_token(client: TestClient, shell: str) -> None:
    response = client.get("/ui")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    assert '<html lang="en">' in response.text
    assert 'type="module" src="/ui/assets/review.js"' in response.text
    assert response.text.replace('\r\n', '\n') == shell.replace('\r\n', '\n')


@pytest.mark.parametrize("path,content_type", [
    ("/ui/assets/app.js", "text/javascript"),
    ("/ui/assets/styles.css", "text/css"),
    ("/ui/assets/index.html", "text/html"),
])
def test_assets_have_the_right_content_type(client: TestClient, path: str, content_type: str) -> None:
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(content_type)


def test_no_cjk_and_no_build_step(shell: str, script: str) -> None:
    for name, text in (("index.html", shell), ("app.js", script),
                       ("styles.css", (UI / "styles.css").read_text(encoding="utf-8"))):
        assert all(ord(ch) < 0x2E80 for ch in text), f"CJK found in {name}"
    assert not (ROOT / "package.json").exists()
    assert not (ROOT / "node_modules").exists()
    assert "import " not in script or "from '" not in script.replace("from 'ml", "")


def test_shell_exposes_the_query_controls(shell: str) -> None:
    for control in ("q", "sort", "order", "actor", "token", "filters-toggle",
                    "views-toggle", "filter-rows", "view-list", "rows", "next", "prev"):
        assert f'id="{control}"' in shell, control
    # step 5 adds staging; the grid editors are wired from edit.js
    for control in ("staging-bar", "review", "discard-all", "reload",
                    "editor-popover", "editor-apply", "review-sheet",
                    "batch-reason", "confirm-batch", "restore-banner"):
        assert f'id="{control}"' in shell, control
    assert 'src="/ui/assets/review.js"' in shell


def test_query_compiler_uses_only_whitelisted_parameters(script: str) -> None:
    # The compiler is a pure function; without a JS harness these assertions pin
    # the important invariant: nothing from a filter row is interpolated into a
    # column name, and an unknown field is reported rather than sent.
    assert "export function compileQuery" in script
    assert "SORT_FIELDS" in script and "FILTER_FIELDS" in script
    for parameter in ("q", "attrs", "status", "sort", "order", "limit", "cursor"):
        assert f"params.set('{parameter}'" in script, parameter
    assert "unit_cost_max' : 'unit_cost_min'" in script
    # facets are emitted from one loop over the whitelist, not three literals
    assert "for (const facet of FACETS)" in script
    assert "params.set(facet, value)" in script
    assert "warnings.push(`unknown filter field" in script
    assert "warnings.push(`unknown sort field" in script
    assert "unit_cost_max' : 'unit_cost_min'" in script


def test_writeback_schema_matches_the_tables(client: TestClient, conn) -> None:
    """The page renders editors from this endpoint, so it must mirror the tables."""
    body = client.get("/v1/writeback/schema").json()
    assert body["schema_version"] == "2"
    assert body["targets"] == ["joineryflow"]

    fields = body["fields"]
    expected = {
        tuple(row) for row in conn.execute(
            "SELECT umr_path, material_column, upstream_column FROM material_writeback_field"
        ).fetchall()
    }
    assert {(row["path"], row["material_column"], row["upstream_column"])
            for row in fields} == expected
    assert all(row["editor"] in ALLOWED_EDITORS for row in fields)

    by_path = {row["path"]: row for row in fields}
    assert by_path["name"]["editor"] == "textarea"
    assert by_path["synonyms"]["editor"] == "tokens"
    assert by_path["unit"]["editor"] == "select"
    assert "lead_time_days" not in by_path  # U22

    catalogs = {row["table_name"]: row for row in body["catalogs"]}
    assert set(catalogs) == {
        row[0] for row in conn.execute(
            "SELECT table_name FROM material_writeback_catalog").fetchall()
    }
    for row in body["catalogs"]:
        keys = conn.execute(
            "SELECT attribute_keys FROM material_writeback_catalog WHERE table_name=%s",
            (row["table_name"],),
        ).fetchone()[0]
        assert [a["key"] for a in row["attributes"]] == keys
        assert all(a["editor"] in ALLOWED_EDITORS for a in row["attributes"])
    notes = next(a for a in catalogs["board_materials"]["attributes"] if a["key"] == "notes")
    assert notes["editor"] == "textarea"

    assert "box" in {u["code"] for u in body["units"]}
    assert all({"code", "name", "dimension"} <= set(u) for u in body["units"])


def test_a_query_round_trip_works_from_the_shells_parameters(client: TestClient) -> None:
    """What `compileQuery` emits must be a working /v1/materials request."""
    response = client.get("/v1/materials", params={
        "q": "", "sort": "unit_cost", "order": "desc", "limit": 5,
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sort"] == "unit_cost" and body["order"] == "desc"
    assert len(body["results"]) <= 5
    assert all("updated_at" in row for row in body["results"])
    assert all("lead_time_days" not in row for row in body["results"])


@pytest.fixture(scope="module")
def edit_script() -> str:
    return (UI / "edit.js").read_text(encoding="utf-8")


def test_edit_module_uses_the_batch_route_not_direct_writes(edit_script: str) -> None:
    """The grid stages diffs; only /v1/change-requests/batch may write (U1)."""
    assert "from './app.js'" in edit_script
    assert "'/v1/change-requests/batch'" in edit_script
    assert "'/v1/writeback/schema'" in edit_script
    assert "`/v1/materials/${encodeURIComponent(row.material_uid)}`" in edit_script
    # no HTTP verb other than POST to the batch route, and never PATCH/PUT/DELETE
    assert "method: 'POST'" in edit_script
    for verb in ("method: 'PATCH'", "method: 'PUT'", "method: 'DELETE'"):
        assert verb not in edit_script
    # one request per material, all staged paths in one diffs array (U2)
    assert "export function groupStaged" in edit_script
    assert "material_uid," in edit_script and "diffs: [...diffs.entries()]" in edit_script
    assert "sessionStorage.setItem(STAGE_KEY" in edit_script
    assert "localStorage.setItem(STAGE_KEY" not in edit_script


def test_edit_module_enforces_the_field_rules(edit_script: str) -> None:
    """Client validation is a convenience; submit() stays the authority (U19/U20)."""
    for helper in ("normaliseValue", "validateStaged", "stagedMaterials",
                   "stagingSummary", "groupStaged", "hasLineBreak"):
        assert f"export function {helper}" in edit_script, helper
    assert "const MAX_CHARS = 1000" in edit_script
    assert "text.trim() === '' ? null : text" in edit_script   # clearing stages null
    assert "at most ${MAX_CHARS} characters" in edit_script
    assert "must start with http:// or https://" in edit_script
    # line breaks are kept and flagged in review, never blocked
    assert "hasLineBreak(entry.after)" in edit_script
    assert "upstream acceptance is unknown" in edit_script


def test_a_staged_batch_body_is_accepted_by_the_route(client: TestClient) -> None:
    """The body groupStaged() builds must be exactly what the batch route takes."""
    uid = client.get("/v1/materials", params={"limit": 1}).json()["results"][0]["material_uid"]
    response = client.post("/v1/change-requests/batch", json={
        "target_system": "joineryflow",
        "reason": "from the grid",
        "materials": [
            {"material_uid": uid, "diffs": [
                {"path": "product_url", "after": "https://example.test/from-grid"},
                {"path": "name", "after": "Line one\nLine two"},
            ]},
        ],
    }, headers={"X-Actor": "alice"})
    assert response.status_code == 200, response.text
    report = response.json()
    # Other cases in this module may already hold an open request on this row,
    # so assert the 200 + per-item shape rather than demanding submitted == 1.
    assert report["submitted"] + report["failed"] == 1, report
    result = report["results"][0]
    assert result["material_uid"] == uid
    assert result["ok"] in (True, False)
    if result["ok"]:
        detail = client.get(f"/v1/change-requests/{result['id']}").json()
        assert [d["path"] for d in detail["diffs"]] == ["product_url", "name"]
        assert detail["diffs"][1]["after"] == "Line one\nLine two"
        assert detail["reason"] == "from the grid"
    else:
        assert result["code"] == "open_request"


@pytest.fixture(scope="module")
def review_script() -> str:
    return (UI / "review.js").read_text(encoding="utf-8")


def test_review_module_only_uses_existing_routes(review_script: str) -> None:
    """No new API and no direct write: queue, detail, decide are the T3 routes."""
    assert "from './app.js'" in review_script
    for route in ("/v1/change-requests?${params", "/v1/change-requests/${row.id}",
                  "/v1/materials/${encodeURIComponent(row.material_uid)}",
                  "/v1/change-requests/${ids[0]}/${decision}",
                  "'/v1/change-requests/decide-batch'"):
        assert route in review_script, route
    for verb in ("method: 'PATCH'", "method: 'PUT'", "method: 'DELETE'"):
        assert verb not in review_script


def test_review_badges_and_selection_are_pure(review_script: str) -> None:
    """stale compares only the diffed paths; own rows are unchecked by default."""
    for helper in ("computeBadges", "ageText", "selectionState", "deepEqual"):
        assert f"export function {helper}" in review_script, helper
    assert "Object.keys(base).some((key) => !deepEqual(base[key], live[key]))" in review_script
    assert "actor === row.requested_by" in review_script
    assert "const BULK_LIMIT = 25" in review_script
    assert "selected.size > BULK_LIMIT" in review_script
    assert "own request" in review_script
    # decision_reason comes from the event via the existing detail payload
    assert "row.decision_reason" in review_script
    assert "row.decided_at" in review_script
    assert "row.decided_by" in review_script


def test_review_shell_exposes_the_controls(shell: str) -> None:
    for control in ("review-view", "review-count", "queue-rows", "queue-total",
                    "approve-selected", "reject-selected", "detail-drawer",
                    "detail-badges", "detail-approve", "detail-reject",
                    "decision-sheet", "decision-reason", "decision-confirm"):
        assert f'id="{control}"' in shell, control
    assert 'src="/ui/assets/review.js"' in shell
    for status in ("actionable", "pending", "approved", "failed", "all"):
        assert f'data-status="{status}"' in shell, status


def test_queue_and_detail_round_trip(client: TestClient, conn) -> None:
    """A filed request appears in the queue with its event-backed decision fields."""
    # material_uid accepts only Crockford base-32 characters (see the table CHECK)
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    raw = uuid.uuid4().hex.upper()
    body = "".join(ch for ch in raw if ch in alphabet)[:26]
    uid = "mat_" + (body + "Z" * 26)[:26]
    conn.execute(
        "INSERT INTO material (material_uid, category_code, name, unit_cost, "
        "source_system, source_id) SELECT %s, category_code, 'Review UI test', 45.00, "
        "'joineryflow', 'review_ui:1' FROM material WHERE status='active' LIMIT 1 "
        "ON CONFLICT (material_uid) DO NOTHING",
        (uid,),
    )
    conn.execute("DELETE FROM material_change_request WHERE material_uid=%s", (uid,))
    created = client.post("/v1/change-requests", json={
        "material_uid": uid,
        "diffs": [{"path": "product_url", "after": "https://example.test/review"}],
        "reason": "from the review test",
    }, headers={"X-Actor": "alice"})
    assert created.status_code == 201, created.text
    rid = created.json()["id"]

    queue = client.get("/v1/change-requests", params={"status": "pending"}).json()
    row = next(r for r in queue["rows"] if r["id"] == rid)
    assert row["material_name"] and row["paths"] == ["product_url"]
    assert row["requested_by"] == "alice" and row["reason"] == "from the review test"
    assert row["decision_reason"] is None

    # approve from a different actor, then the reason is read back from the event
    decided = client.post(f"/v1/change-requests/{rid}/approve",
                          json={"reason": "checked"}, headers={"X-Actor": "bob"})
    assert decided.status_code == 200, decided.text
    detail = client.get(f"/v1/change-requests/{rid}").json()
    assert detail["status"] == "approved"
    assert detail["decision_reason"] == "checked"
    assert detail["decided_by"] == "bob" and detail["decided_at"]
    # A plan can legitimately be invalid for a synthetic source_id; what matters
    # to the UI is that it never contains a token and always reports a code.
    plan = detail["plan"]
    assert "code" in plan
    assert "Authorization" not in plan.get("headers", {})

    # own-request guard is still the server's, not the form's
    other = client.post("/v1/change-requests", json={
        "material_uid": uid,
        "diffs": [{"path": "synonyms", "after": ["review", "own"]}],
    }, headers={"X-Actor": "bob"})
    assert other.status_code == 201
    refused = client.post(f"/v1/change-requests/{other.json()['id']}/approve",
                          json={"reason": "x"}, headers={"X-Actor": "bob"})
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "conflict"
    conn.execute("SELECT set_config('ml.allow_delete', 'on', false)")
    try:
        conn.execute("DELETE FROM material WHERE material_uid=%s", (uid,))
    finally:
        conn.execute("SELECT set_config('ml.allow_delete', 'off', false)")


@pytest.fixture(scope="module")
def create_script() -> str:
    return (UI / "create.js").read_text(encoding="utf-8")


def test_create_module_uses_only_the_three_routes(create_script: str) -> None:
    """No direct write and no new API: the step-3 HTTP surface is the only path."""
    assert "from './app.js'" in create_script
    assert "from './edit.js'" in create_script   # reuses normaliseValue / validateStaged
    for route in ("/v1/writeback/schema", "/v1/material-create-requests'",
                  "/v1/material-create-requests/${editingId}"):
        assert route in create_script, route
    assert "method: editingId ? 'PATCH' : 'POST'" in create_script
    assert "method: 'DELETE'" not in create_script
    assert "reason: reason || undefined" in create_script


def test_create_form_covers_allowlist_and_pure_helpers(create_script: str) -> None:
    """The field list mirrors the SQL allowlist; helpers are exported for tests."""
    for helper in ("buildProposed", "validateProposed", "editorForPath", "groupProposed",
                   "FIELD_GROUPS"):
        assert f"export function {helper}" in create_script or f"export const {helper}" in create_script
    # Every proposed field the SQL validator accepts must have a form definition.
    for path in ("category_code", "name", "product_type", "model_no", "brand_id",
                 "default_supplier_id", "default_supplier_ref", "family_id", "unit",
                 "unit_cost", "currency", "cost_basis", "lead_time_days", "description",
                 "product_url", "image_url", "synonyms", "tags", "source_system",
                 "source_id", "source_payload", "imported_at"):
        assert f"path: '{path}'" in create_script, path
    assert "attributes." in create_script            # dynamic per-category fields
    assert "category_code is required" in create_script
    assert "name is required" in create_script


def test_create_preview_is_grouped_with_collapsed_json(create_script: str) -> None:
    """Decision 1: a field table by default, raw JSON under a collapsed panel."""
    assert "groupProposed(proposed" in create_script
    assert "detail-diff" in create_script
    assert "add-preview-json" in create_script
    assert "JSON.stringify(proposed, null, 2)" in create_script
    assert "startsWith('editing ')" in create_script  # do not clobber the reopen message


def test_create_reopen_is_author_and_pending_only(create_script: str) -> None:
    """Decision 2: pending is editable in place; decided rows open read-only."""
    assert "row.status !== 'pending'" in create_script
    assert "only pending requests can be edited" in create_script
    assert "editingId = Number(id)" in create_script
    assert "Save changes" in create_script
    assert "the id stays the same" in create_script


def test_shell_exposes_the_add_tab_and_controls(shell: str) -> None:
    for control in ("add-view", "add-fields", "add-preview-table", "add-json-details",
                    "add-preview-json", "add-reopen-id", "add-reopen", "add-reason",
                    "add-submit", "add-status"):
        assert f'id="{control}"' in shell, control
    assert 'data-tab="add"' in shell
    assert 'src="/ui/assets/create.js"' in shell


def test_tab_switching_shows_one_view(script: str) -> None:
    """Edit, Add and Review are mutually exclusive; the event still fans out."""
    assert "body > main, #add-view, #review-view" in script
    assert "view.tagName === 'MAIN' ? 'edit'" in script
    assert "ml:tab-changed" in script
