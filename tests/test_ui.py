"""HTTP checks for the query UI shell (docs/ui-bulk-edit.md section 12).

The browser code has no unit harness (U11): no npm, no build step. What can be
checked from Python is checked here - the shell is served without a token, the
assets have the right content types, and the endpoint the page depends on agrees
with the tables it claims to mirror. A search round trip through the HTTP API is
asserted in test_api.py; this file only checks that the page can reach it.
"""
from __future__ import annotations

import json
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
    assert 'type="module" src="/ui/assets/app.js"' in response.text
    assert response.text == shell


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
    # step 4 is read-only: the staging controls arrive with the grid in step 5
    assert "Review and submit" not in shell
    assert "Discard all" not in shell


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
