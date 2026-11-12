"""Tests for the approval-based write-back executor (T3, step 3).

Design: docs/t3-writeback.md sections 3, 8, 9, 11 and 12.

Two layers, matching tests/test_etl.py:

* Pure input-side checks need no database. They pin the inverse field
  translation, the nine catalog endpoints, the workspace guard and the rule
  that --apply refuses to start before its configuration is complete.
* Integration checks need ML_WRITEBACK_DSN pointing at a FRESH PostgreSQL 16
  database with schema/material.sql, search.sql, seed.sql, writeback.sql and
  the committed snapshot already loaded. They are skipped when that variable
  is absent, so the ordinary suite (which runs against ml_api) never asserts
  write-back counts against a database carrying the evaluation corpus.

CI never egresses to joineryflow. Every HTTP call in this file goes to a
stdlib http.server bound to 127.0.0.1 on an ephemeral port, with a dummy
token. No real credential or upstream URL is used anywhere.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "apply_change_requests.py"
MAPPING = ROOT / "mapping" / "joineryflow.yaml"
WRITE_SQL = ROOT / "schema" / "writeback.sql"


@pytest.fixture(scope="module")
def worker():
    spec = importlib.util.spec_from_file_location("apply_change_requests", CLI)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclass resolves postponed annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Fake upstream: a local HTTP server, never a real joineryflow endpoint
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def do_PATCH(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        owner = self.server.owner
        owner.records.append({
            "method": "PATCH",
            "path": self.path,
            "headers": dict(self.headers),
            "body": json.loads(raw.decode("utf-8")) if raw else {},
        })
        if owner.delay:
            time.sleep(owner.delay)
        payload = json.dumps({"error": owner.message}).encode("utf-8")
        try:
            self.send_response(owner.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client timed out; its outcome is deliberately unknown

    def log_message(self, *args):
        pass


class FakeUpstream:
    def __init__(self):
        self.records: list[dict] = []
        self.status = 200
        self.delay = 0.0
        self.message = "ok"

    def start(self):
        class _Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.owner = self
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=3)


@pytest.fixture
def upstream():
    server = FakeUpstream().start()
    try:
        yield server
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Pure unit checks: no database, no real network
# ---------------------------------------------------------------------------

def test_dry_run_is_the_default(worker, monkeypatch):
    for key in ("ML_JF_BASE_URL", "ML_JF_TOKEN", "ML_JF_TIMEOUT", "ML_JF_WORKSPACE"):
        monkeypatch.delenv(key, raising=False)
    args = worker.make_parser().parse_args([])
    assert args.apply is False and args.retry is None
    assert worker.build_config(args).mode == "dry-run"


def test_apply_requires_configuration_before_the_database(worker, monkeypatch):
    for key in ("ML_JF_BASE_URL", "ML_JF_TOKEN", "ML_JF_TIMEOUT", "ML_JF_WORKSPACE"):
        monkeypatch.delenv(key, raising=False)

    def explode(dsn):
        raise AssertionError("the database was opened before configuration was validated")

    assert worker.main(["--apply", "--dsn", "x"], connect=explode) == 2
    monkeypatch.setenv("ML_JF_BASE_URL", "http://127.0.0.1:1")
    assert worker.main(["--apply", "--dsn", "x"], connect=explode) == 2
    monkeypatch.setenv("ML_JF_TOKEN", "dummy")
    monkeypatch.setenv("ML_JF_TIMEOUT", "not-a-number")
    assert worker.main(["--apply", "--dsn", "x"], connect=explode) == 2


def test_workspace_guard_agrees_with_the_snapshot_header(worker, monkeypatch):
    monkeypatch.delenv("ML_JF_WORKSPACE", raising=False)
    assert worker.resolve_workspace() == "hartwood-joinery"
    monkeypatch.setenv("ML_JF_WORKSPACE", "another-workspace")
    with pytest.raises(worker.ConfigError, match="foreign workspace"):
        worker.resolve_workspace()


def test_inverse_field_translation_agrees_with_the_mapping():
    import yaml

    rules = yaml.safe_load(MAPPING.read_text(encoding="utf-8"))
    # The YAML writes fields as <upstream column>: <UMR field>; the executor
    # and material_writeback_field store the inverse direction.
    expected = {umr: upstream_col for upstream_col, umr in rules["write_back"]["fields"].items()}
    sql = WRITE_SQL.read_text(encoding="utf-8")
    block = sql.split("INSERT INTO material_writeback_field", 1)[1].split("ON CONFLICT", 1)[0]
    rows = re.findall(r"\('([^']+)',\s*'([^']+)',\s*'([^']+)'\)", block)
    got = {umr: (material_col, upstream_col) for umr, material_col, upstream_col in rows}
    assert set(got) == set(expected), got
    assert all(got[umr][1] == col for umr, col in expected.items())
    assert len(got) == 7


def test_all_nine_catalog_endpoints_agree_with_the_mapping():
    import yaml

    rules = yaml.safe_load(MAPPING.read_text(encoding="utf-8"))
    expected = {name: spec["endpoint"] for name, spec in rules["tables"].items()}
    sql = WRITE_SQL.read_text(encoding="utf-8")
    block = sql.split("INSERT INTO material_writeback_catalog", 1)[1].split("ON CONFLICT", 1)[0]
    rows = re.findall(r"\('([a-z_]+)',\s*'[a-z_]+',\s*'(/catalog/[a-z-]+)',", block)
    assert dict(rows) == expected
    assert len(rows) == 9


def test_no_hardcoded_live_upstream_in_the_worker():
    src = CLI.read_text(encoding="utf-8")
    assert "ML_JF_BASE_URL" in src
    assert "Authorization" in src
    assert not re.search(r"https?://(?!127\.0\.0\.1)", src)
    assert "dry-run" in src
    assert "--apply requires ML_JF_BASE_URL" in src


def test_client_sends_an_authorized_patch_to_the_local_fake(worker, upstream):
    client = worker.JoineryflowClient(upstream.base_url, "fake-token", 2.0)
    status, _ = client.patch(
        "/catalog/board-materials/1",
        {"Content-Type": "application/json", "Idempotency-Key": "mlcr-1"},
        {"unit_cost": 99.5},
    )
    assert status == 200
    rec = upstream.records[0]
    assert rec["method"] == "PATCH"
    assert rec["path"] == "/catalog/board-materials/1"
    assert rec["headers"]["Authorization"] == "Bearer fake-token"
    assert rec["headers"]["Idempotency-Key"] == "mlcr-1"
    assert rec["body"] == {"unit_cost": 99.5}


def test_upstream_message_is_short_and_report_safe(worker):
    assert worker.upstream_message('{"error":"bad request"}', 400) == "HTTP 400: bad request"
    assert len(worker.upstream_message("x" * 900, None)) == 400
    assert worker.upstream_message("", 503) == "HTTP 503"


# ---------------------------------------------------------------------------
# Integration: a fresh database with the snapshot imported
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def wb_dsn() -> str:
    value = os.environ.get("ML_WRITEBACK_DSN", "").strip()
    if not value:
        pytest.skip("ML_WRITEBACK_DSN is not set; write-back needs a fresh database")
    return value


@pytest.fixture(scope="session")
def wb_conn(wb_dsn):
    import psycopg

    with psycopg.connect(wb_dsn, autocommit=True) as conn:
        yield conn


@pytest.fixture
def clean_db(wb_conn):
    with wb_conn.cursor() as cur:
        cur.execute("SELECT material_uid, unit_cost FROM material")
        before = cur.fetchall()
        cur.execute("DELETE FROM material_event WHERE entity='material_change_request'")
        cur.execute("DELETE FROM material_change_request")
    yield wb_conn
    with wb_conn.cursor() as cur:
        cur.execute("SELECT material_uid, unit_cost FROM material")
        after = dict(cur.fetchall())
        for uid, value in before:
            if after.get(uid) != value:
                cur.execute("UPDATE material SET unit_cost=%s WHERE material_uid=%s",
                            (value, uid))


def one_uid(conn, index=0) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT material_uid FROM material WHERE source_system='joineryflow' "
            "ORDER BY source_id LIMIT 1 OFFSET %s", (index,))
        return cur.fetchone()[0]


def make_request(conn, uid: str, after: float, *, approve: bool = True) -> int:
    diffs = json.dumps([{"path": "unit_cost", "after": after}])
    with conn.cursor() as cur:
        cur.execute("SELECT material_change_request_submit(%s, %s, NULL, 'alice')",
                    (uid, diffs))
        result = cur.fetchone()[0]
        assert result["ok"] is True, result
        rid = result["id"]
        if approve:
            cur.execute("SELECT material_change_request_decide(%s, 'approve', NULL, 'bob')",
                        (rid,))
            assert cur.fetchone()[0]["ok"] is True
    return rid


def state(conn, rid) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, attempts, last_status, error, applied_at, claimed_at "
            "FROM material_change_request WHERE id=%s", (rid,))
        row = cur.fetchone()
    return dict(zip(("status", "attempts", "last_status", "error",
                     "applied_at", "claimed_at"), row))


def count_events(conn, rid=None) -> int:
    with conn.cursor() as cur:
        if rid is None:
            cur.execute("SELECT count(*) FROM material_event "
                        "WHERE entity='material_change_request'")
        else:
            cur.execute("SELECT count(*) FROM material_event "
                        "WHERE entity='material_change_request' "
                        "AND (payload->>'request_id')::bigint=%s", (rid,))
        return cur.fetchone()[0]


def cli_env(upstream, dsn, timeout="1.5") -> dict:
    env = os.environ.copy()
    env.update({"ML_DSN": dsn,
                "ML_JF_BASE_URL": upstream.base_url,
                "ML_JF_TOKEN": "fake-token",
                "ML_JF_TIMEOUT": timeout})
    env.pop("ML_JF_WORKSPACE", None)
    return env


def run_cli(upstream, dsn, *extra, timeout="1.5"):
    return subprocess.run(
        [sys.executable, str(CLI), "--dsn", dsn, *extra, "--json", "-"],
        capture_output=True, text=True, cwd=ROOT,
        env=cli_env(upstream, dsn, timeout), check=False, timeout=45,
    )


def test_dry_run_is_a_true_no_op(clean_db, upstream, wb_dsn):
    pending = make_request(clean_db, one_uid(clean_db, 0), 901.0, approve=False)
    approved = make_request(clean_db, one_uid(clean_db, 1), 902.0)
    events_before = count_events(clean_db)

    proc = run_cli(upstream, wb_dsn, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    rep = json.loads(proc.stdout)
    assert rep["mode"] == "dry-run" and rep["checked"] == 1 and rep["claimed"] == 0
    assert rep["applied"] == rep["failed"] == rep["unknown"] == 0
    row = rep["rows"][0]
    assert row["id"] == approved and row["url"].startswith("/catalog/")
    assert row["headers"]["Idempotency-Key"] == f"mlcr-{approved}"
    assert row["headers"]["X-Workspace-Slug"] == "hartwood-joinery"
    assert "Authorization" not in row["headers"]
    assert row["body"] == {"unit_cost": 902.0}
    assert upstream.records == []
    assert count_events(clean_db) == events_before
    assert state(clean_db, approved)["claimed_at"] is None
    assert state(clean_db, approved)["attempts"] == 0
    assert state(clean_db, pending)["status"] == "pending"


def test_happy_path_applies_exactly_once(clean_db, upstream, wb_dsn):
    rid = make_request(clean_db, one_uid(clean_db, 0), 903.0)
    proc = run_cli(upstream, wb_dsn, "--apply", "--request-id", str(rid))
    assert proc.returncode == 0, proc.stderr
    rep = json.loads(proc.stdout)
    assert rep["applied"] == 1 and rep["failed"] == 0 and rep["unknown"] == 0
    assert rep["rows"][0]["http_status"] == 200
    assert "fake-token" not in proc.stdout and "fake-token" not in proc.stderr

    st = state(clean_db, rid)
    assert st["status"] == "applied" and st["attempts"] == 1
    assert st["last_status"] == 200 and st["error"] is None
    assert st["applied_at"] is not None and st["claimed_at"] is None
    assert count_events(clean_db, rid) == 3

    assert len(upstream.records) == 1
    rec = upstream.records[0]
    assert rec["method"] == "PATCH" and rec["body"] == {"unit_cost": 903.0}
    assert rec["headers"]["Idempotency-Key"] == f"mlcr-{rid}"
    assert rec["headers"]["X-Workspace-Slug"] == "hartwood-joinery"
    assert rec["headers"]["Authorization"] == "Bearer fake-token"


def test_second_apply_sends_nothing(clean_db, upstream, wb_dsn):
    rid = make_request(clean_db, one_uid(clean_db, 0), 904.0)
    assert run_cli(upstream, wb_dsn, "--apply", "--request-id", str(rid)).returncode == 0
    upstream.records.clear()
    proc = run_cli(upstream, wb_dsn, "--apply")
    assert proc.returncode == 0, proc.stderr
    rep = json.loads(proc.stdout)
    assert rep["claimed"] == 0 and rep["applied"] == 0
    assert upstream.records == []
    assert state(clean_db, rid)["status"] == "applied"


def test_4xx_is_a_known_failure(clean_db, upstream, wb_dsn):
    upstream.status, upstream.message = 400, "bad request"
    rid = make_request(clean_db, one_uid(clean_db, 0), 905.0)
    proc = run_cli(upstream, wb_dsn, "--apply", "--request-id", str(rid))
    assert proc.returncode == 1, proc.stderr
    rep = json.loads(proc.stdout)
    assert rep["failed"] == 1 and rep["unknown"] == 0
    st = state(clean_db, rid)
    assert st["status"] == "failed" and st["last_status"] == 400
    assert "bad request" in st["error"]
    assert len(upstream.records) == 1


def test_5xx_is_unknown_and_refuses_retry(clean_db, upstream, wb_dsn):
    upstream.status = 503
    rid = make_request(clean_db, one_uid(clean_db, 0), 906.0)
    proc = run_cli(upstream, wb_dsn, "--apply", "--request-id", str(rid))
    assert proc.returncode == 1, proc.stderr
    rep = json.loads(proc.stdout)
    assert rep["unknown"] == 1 and rep["failed"] == 0
    st = state(clean_db, rid)
    assert st["status"] == "failed" and st["error"] == "uncertain_outcome"
    assert st["last_status"] == 503
    assert len(upstream.records) == 1  # no automatic retry, no second PATCH

    retry = run_cli(upstream, wb_dsn, "--retry", str(rid), "--actor", "carol")
    assert retry.returncode == 1, retry.stderr
    assert json.loads(retry.stdout)["rows"][0]["outcome"] == "uncertain_outcome"


def test_timeout_is_an_unknown_outcome(clean_db, upstream, wb_dsn):
    upstream.delay = 1.2
    rid = make_request(clean_db, one_uid(clean_db, 0), 907.0)
    proc = run_cli(upstream, wb_dsn, "--apply", "--request-id", str(rid), timeout="0.3")
    assert proc.returncode == 1, proc.stderr
    rep = json.loads(proc.stdout)
    assert rep["unknown"] == 1
    st = state(clean_db, rid)
    assert st["status"] == "failed" and st["error"] == "uncertain_outcome"
    assert st["last_status"] is None
    assert len(upstream.records) == 1


def test_stale_approval_becomes_failed_even_after_a_2xx(clean_db, upstream, wb_dsn):
    uid = one_uid(clean_db, 0)
    rid = make_request(clean_db, uid, 908.0)
    with clean_db.cursor() as cur:
        cur.execute("UPDATE material SET unit_cost = 777 WHERE material_uid=%s", (uid,))
    proc = run_cli(upstream, wb_dsn, "--apply", "--request-id", str(rid))
    assert proc.returncode == 1, proc.stderr
    rep = json.loads(proc.stdout)
    assert rep["failed"] == 1 and rep["applied_unverified"] == 1
    assert rep["rows"][0]["outcome"] == "applied_unverified"
    st = state(clean_db, rid)
    assert st["status"] == "failed" and st["error"] == "stale_base"
    assert st["applied_at"] is None
    assert len(upstream.records) == 1


def test_forbidden_paths_never_reach_the_worker(clean_db, upstream, wb_dsn):
    uid = one_uid(clean_db, 0)
    with clean_db.cursor() as cur:
        for path in ("archived_at", "price.unit_cost", "source_payload", "status"):
            cur.execute("SELECT material_change_request_submit(%s, %s, NULL, 'alice')",
                        (uid, json.dumps([{"path": path, "after": None}])))
            assert cur.fetchone()[0]["code"] == "forbidden_path", path
    proc = run_cli(upstream, wb_dsn, "--apply")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["claimed"] == 0
    assert upstream.records == []


def test_retry_then_apply_a_known_failure(clean_db, upstream, wb_dsn):
    upstream.status, upstream.message = 409, "conflict upstream"
    rid = make_request(clean_db, one_uid(clean_db, 0), 909.0)
    first = run_cli(upstream, wb_dsn, "--apply", "--request-id", str(rid))
    assert first.returncode == 1
    assert state(clean_db, rid)["status"] == "failed"

    retry = run_cli(upstream, wb_dsn, "--retry", str(rid), "--actor", "carol")
    assert retry.returncode == 0, retry.stderr
    assert state(clean_db, rid)["status"] == "approved"

    upstream.status, upstream.message = 200, "ok"
    upstream.records.clear()
    second = run_cli(upstream, wb_dsn, "--apply", "--request-id", str(rid))
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["applied"] == 1
    st = state(clean_db, rid)
    assert st["status"] == "applied" and st["attempts"] == 2
    assert len(upstream.records) == 1


def test_two_concurrent_workers_patch_each_request_once(clean_db, upstream, wb_dsn):
    rids = [make_request(clean_db, one_uid(clean_db, i), 910.0 + i) for i in range(4)]
    env = cli_env(upstream, wb_dsn)
    cmd = [sys.executable, str(CLI), "--dsn", wb_dsn, "--apply",
           "--limit", "2", "--json", "-"]
    procs = [subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, cwd=ROOT, env=env) for _ in range(2)]
    outputs = [p.communicate(timeout=45) for p in procs]
    assert all(p.returncode == 0 for p in procs), [o[1] for o in outputs]

    keys = [rec["headers"].get("Idempotency-Key") for rec in upstream.records]
    assert sorted(keys) == sorted(f"mlcr-{rid}" for rid in rids), keys
    assert all(state(clean_db, rid)["status"] == "applied" for rid in rids)
