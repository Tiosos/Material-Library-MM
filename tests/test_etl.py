"""Integration tests for the joineryflow ETL (delivery step 4).

Design: docs/etl-joineryflow.md sections 10 and 11.

Two layers live here:

* Pure input-side checks run everywhere, including the API job, with no
  database. They stop scripts/etl_contract.sql from drifting away from
  mapping/joineryflow.yaml.
* Integration checks need ML_ETL_DSN pointing at a FRESH database with the
  schema loaded and the committed snapshot already imported twice. They are
  skipped when that variable is absent, because the ordinary pytest suite runs
  against ml_api, which also holds the evaluation corpus and the supplier demo.

ML_ETL_DSN is deliberately separate from ML_DSN: the API job sets ML_DSN to a
database whose counts are not the ETL counts. Running these tests against it
would assert the wrong thing rather than merely fail.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_SQL = ROOT / "scripts" / "etl_contract.sql"
IMPORT_CLI = ROOT / "scripts" / "import_joineryflow.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mapping_expectations() -> dict:
    from ingest.joineryflow import load_rules

    rules = load_rules()
    return (rules.data.get("import_contract") or {})["expected"]


def _contract_body() -> str:
    """The DO block, with psql meta commands removed so a driver can run it.

    psql needs the leading ``\\set ON_ERROR_STOP on`` line; psycopg would reject
    it as a syntax error. Both paths execute exactly the same assertions.
    """
    sql = CONTRACT_SQL.read_text(encoding="utf-8")
    return "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("\\")
    )


def _rows(conn, sql: str, params=None) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _run_contract(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(_contract_body())


# ---------------------------------------------------------------------------
# Input-side contract: no database needed
# ---------------------------------------------------------------------------

def test_snapshot_satisfies_the_mapping_contract() -> None:
    from ingest.joineryflow import (
        build_drafts,
        build_report,
        check_contract,
        load_rules,
        read_snapshot,
    )

    rules = load_rules()
    snapshot = read_snapshot(rules.snapshot_path)
    drafts, meta = build_drafts(snapshot, rules)
    report = build_report(drafts, meta, rules)

    assert check_contract(report, rules) == []
    assert report["rows"] == 18
    assert report["errors"] == 0
    assert report["source_payload_on_all_rows"] is True
    # Decision D2, not the 14 printed in design sections 10 and 13.
    assert report["prices"] == 13
    # Ten derived from the description plus one from a real column: the design's
    # "ten thickness_mm measures" counts the derived ones only.
    assert report["derived_thickness_measures"] == 10
    assert report["measures_from_columns"] == 1


def test_sql_contract_constants_match_the_mapping() -> None:
    expected = _mapping_expectations()
    sql = CONTRACT_SQL.read_text(encoding="utf-8")
    needles = {
        "materials": "v_total <>",
        "archived": "v_archived <>",
        "categories_with_rows": "v_categories <>",
        "derived_thickness_measures": "v_derived <>",
        "prices": "v_prices <>",
        "cv_identifiers": "v_cv <>",
        "families": "v_families <>",
        "single_member_families": "v_singles <>",
    }
    for key, prefix in needles.items():
        assert f"{prefix} {expected[key]}" in sql, (
            f"scripts/etl_contract.sql disagrees with import_contract.expected.{key}"
        )

    body = _contract_body()
    assert body.count("DO $$") == 1
    assert "RAISE EXCEPTION" in body


# ---------------------------------------------------------------------------
# Integration: a fresh PostgreSQL 16 database with two imports already applied
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def etl_dsn() -> str:
    value = os.environ.get("ML_ETL_DSN", "").strip()
    if not value:
        pytest.skip("ML_ETL_DSN is not set; the ETL contract needs a fresh database")
    return value


@pytest.fixture(scope="session")
def etl_conn(etl_dsn: str):
    import psycopg

    with psycopg.connect(etl_dsn, autocommit=True) as conn:
        yield conn


def test_sql_contract_passes_on_the_imported_database(etl_conn) -> None:
    _run_contract(etl_conn)


def test_measure_window_agrees_across_units(etl_conn) -> None:
    """Design section 11.4: mm, cm and m must select the same two boards."""
    specs = {
        "mm": '{"thickness_mm":{"gte":17.5,"lte":18.5,"unit":"mm"}}',
        "cm": '{"thickness_mm":{"gte":1.75,"lte":1.85,"unit":"cm"}}',
        "m": '{"thickness_mm":{"gte":0.0175,"lte":0.0185,"unit":"m"}}',
    }
    found = {}
    for unit, spec in specs.items():
        rows = _rows(
            etl_conn,
            "SELECT material_uid FROM material_search(p_measures => %s::jsonb)",
            (spec,),
        )
        found[unit] = {row[0] for row in rows}

    assert all(len(uids) == 2 for uids in found.values()), found
    assert found["mm"] == found["cm"] == found["m"]
    named = {
        row[0] for row in _rows(etl_conn, "SELECT material_uid FROM material_search('18mm')")
    }
    assert named == found["mm"]


def test_hire_rate_is_an_attribute_not_a_price(etl_conn) -> None:
    """Decision D2: there is no 'day' unit, so the hire rate is not a price."""
    assert _rows(etl_conn, "SELECT count(*) FROM material_price")[0][0] == 13
    assert _rows(
        etl_conn,
        "SELECT count(*) FROM material_price p JOIN material m USING (material_uid) "
        "WHERE m.category_code = 'hire'",
    )[0][0] == 0
    assert _rows(
        etl_conn,
        "SELECT count(*) FROM material WHERE category_code = 'hire' AND attributes ? 'rate'",
    )[0][0] == 1


def test_reimport_is_a_true_noop(etl_dsn: str, etl_conn) -> None:
    """Design section 11.5: another import must not create a new revision.

    rev and the revision log must not move. sync_seq is different: the schema's
    ml_child_touch advances the parent cursor on rows whose measure is rewritten,
    even though the parent row itself is unchanged. Rows without a measure keep
    their cursor, and no cursor may move on a row without a measure.
    """
    before = {
        row[0]: (row[1], row[2])
        for row in _rows(etl_conn, "SELECT material_uid, rev, sync_seq FROM material")
    }
    assert len(before) == 18
    revisions_before = _rows(etl_conn, "SELECT count(*) FROM material_revision")[0][0]
    updates_before = _rows(
        etl_conn,
        "SELECT count(*) FROM material_event WHERE entity = 'material' AND op = 'update'",
    )[0][0]
    with_measures = {
        row[0]
        for row in _rows(etl_conn, "SELECT DISTINCT material_uid FROM material_measure")
    }
    assert len(with_measures) == 11

    proc = subprocess.run(
        [sys.executable, str(IMPORT_CLI), "--dsn", etl_dsn, "--json", "--quiet"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["errors"] == 0
    assert report["contract"]["ok"] is True
    assert report["stats"]["new"] == 0
    assert report["stats"]["updated"] == 18
    assert report["stats"]["skipped"] == 0

    after = {
        row[0]: (row[1], row[2])
        for row in _rows(etl_conn, "SELECT material_uid, rev, sync_seq FROM material")
    }
    assert set(after) == set(before)
    assert {k: v[0] for k, v in after.items()} == {k: v[0] for k, v in before.items()}
    assert _rows(etl_conn, "SELECT count(*) FROM material_revision")[0][0] == revisions_before
    assert _rows(
        etl_conn,
        "SELECT count(*) FROM material_event WHERE entity = 'material' AND op = 'update'",
    )[0][0] == updates_before

    untouched = set(before) - with_measures
    assert all(after[k][1] == before[k][1] for k in untouched)
    moved = {k for k in before if after[k][1] != before[k][1]}
    assert moved <= with_measures, moved

    # The SQL contract is a statement about steady state, so it must still hold
    # after a third import.
    _run_contract(etl_conn)
