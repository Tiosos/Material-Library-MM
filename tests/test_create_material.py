"""Integration tests for approval-based material creation (step 2).

The apply function inserts a real material row and its audit events, so these
tests need the live schema (ML_DSN) and undo their writes in a rolled-back
transaction. The two CLIs are exercised through subprocess with the same DSN.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "scripts" / "apply_change_requests.py"
PRUNE = ROOT / "scripts" / "prune_change_requests.py"


def cleanup(conn, rids, uids=(), sources=()):
    """Undo a test's rows. Hard delete needs the maintenance switch."""
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('ml.allow_delete', 'on', true)")
        for rid in rids:
            cur.execute("DELETE FROM material_event WHERE entity='material_create_request' "
                        "AND payload->>'request_id'=%s", (str(rid),))
            cur.execute("DELETE FROM material_create_request WHERE id=%s", (rid,))
        for uid in uids:
            cur.execute("DELETE FROM material_event WHERE material_uid=%s", (uid,))
            cur.execute("DELETE FROM material WHERE material_uid=%s", (uid,))
        for system, source_id in sources:
            cur.execute("DELETE FROM material_event WHERE material_uid IN "
                        "(SELECT material_uid FROM material WHERE source_system=%s "
                        "AND source_id=%s)", (system, source_id))
            cur.execute("DELETE FROM material WHERE source_system=%s AND source_id=%s",
                        (system, source_id))
    conn.commit()


@pytest.fixture
def create_conn(dsn):
    """Start with no create requests; undo this test's writes afterwards."""
    with psycopg.connect(dsn, autocommit=False) as conn:
        cleanup(conn, [r[0] for r in _all_request_ids(conn)])
        yield conn
        conn.rollback()


def _all_request_ids(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM material_create_request")
        return cur.fetchall()


def file_request(conn, proposed: dict, actor="alice"):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT material_create_request_submit(%s, %s, %s)",
            (json.dumps(proposed), "from test", actor),
        )
        report = cur.fetchone()[0]
    return report["id"]


def decide(conn, rid, actor="bob"):
    with conn.cursor() as cur:
        cur.execute("SELECT material_create_request_decide(%s, 'approve', 'ok', %s)",
                    (rid, actor))
        return cur.fetchone()[0]


def run_cli(script: Path, dsn: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), "--dsn", dsn, *args],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def test_apply_creates_worker_inserts_and_is_idempotent(dsn, create_conn):
    rid = file_request(create_conn, {
        "category_code": "board", "name": "Worker created",
        "source_system": "test", "source_id": "worker-1",
    })
    assert decide(create_conn, rid)["code"] == "approved"
    # the worker needs a committed row: close this transaction's view by
    # committing through a second connection is unnecessary - the CLI uses
    # its own connection, so commit here first and roll back by DELETE after.
    create_conn.commit()

    try:
        first = run_cli(WORKER, dsn, "--apply-creates", "--json", "-")
        report = json.loads(first.stdout)
        assert report["mode"] == "apply-creates"
        row = next(r for r in report["rows"] if r["id"] == rid)
        assert row["status"] == "applied" and row["error"] is None
        uid = row["material_uid"]

        with create_conn.cursor() as cur:
            cur.execute("SELECT status FROM material_create_request WHERE id=%s", (rid,))
            assert cur.fetchone()[0] == "applied"
            cur.execute("SELECT count(*) FROM material_event WHERE material_uid=%s "
                        "AND entity='material' AND op='insert'", (uid,))
            assert cur.fetchone()[0] == 1

        second = run_cli(WORKER, dsn, "--apply-creates", "--json", "-")
        report2 = json.loads(second.stdout)
        assert all(r["id"] != rid for r in report2["rows"])  # no longer approved
    finally:
        cleanup(create_conn, [rid], sources=[('test', 'worker-1')])


def test_apply_creates_reports_duplicate_source(dsn, create_conn):
    with create_conn.cursor() as cur:
        cur.execute("INSERT INTO material (material_uid, category_code, name, "
                    "source_system, source_id) VALUES (ml_new_material_uid(), 'board', "
                    "'Existing', 'test', 'dup-1')")
    create_conn.commit()
    rid = file_request(create_conn, {
        "category_code": "board", "name": "Duplicate",
        "source_system": "test", "source_id": "dup-1",
    })
    assert decide(create_conn, rid)["code"] == "approved"
    create_conn.commit()

    try:
        proc = run_cli(WORKER, dsn, "--apply-creates", "--json", "-")
        rows = json.loads(proc.stdout)["rows"]
        row = next(r for r in rows if r["id"] == rid)
        assert row["status"] == "duplicate_source"
        with create_conn.cursor() as cur:
            cur.execute("SELECT status, error FROM material_create_request WHERE id=%s", (rid,))
            status, error = cur.fetchone()
            assert status == "failed" and error.startswith("duplicate_source:")
            cur.execute("SELECT count(*) FROM material WHERE source_system='test' "
                        "AND source_id='dup-1'")
            assert cur.fetchone()[0] == 1
    finally:
        cleanup(create_conn, [rid], sources=[('test', 'dup-1')])


def test_prune_clears_create_rows_but_keeps_the_event(dsn, create_conn):
    rid = file_request(create_conn, {
        "category_code": "board", "name": "Pruned",
        "source_system": "test", "source_id": "prune-1",
    })
    assert decide(create_conn, rid)["code"] == "approved"
    create_conn.commit()
    applied = run_cli(WORKER, dsn, "--apply-creates", "--json", "-")
    uid = next(r for r in json.loads(applied.stdout)["rows"]
               if r["id"] == rid)["material_uid"]

    try:
        dry = run_cli(PRUNE, dsn, "--older-than", "0", "--json", "-")
        assert dry.returncode == 0, dry.stderr
        # The create row is terminal and old; the material and events survive.
        rep = json.loads(dry.stdout)
        assert any(r["id"] == rid for r in rep["create_requests"]["rows"])

        real = run_cli(PRUNE, dsn, "--older-than", "0", "--apply", "--json", "-")
        assert real.returncode == 0, real.stderr
        rep = json.loads(real.stdout)
        assert rep["create_requests"]["deleted"] >= 1

        with create_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM material_create_request WHERE id=%s", (rid,))
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM material WHERE material_uid=%s", (uid,))
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT count(*) FROM material_event WHERE material_uid=%s", (uid,))
            assert cur.fetchone()[0] >= 2  # material insert + create-request event
    finally:
        cleanup(create_conn, [rid], uids=[uid])
