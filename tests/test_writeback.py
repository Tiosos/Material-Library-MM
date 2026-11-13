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
IMPORT_CLI = ROOT / "scripts" / "import_joineryflow.py"
PRUNE_CLI = ROOT / "scripts" / "prune_change_requests.py"
MAPPING = ROOT / "mapping" / "joineryflow.yaml"
WRITE_SQL = ROOT / "schema" / "writeback.sql"


@pytest.fixture(scope="module")
def importer():
    spec = importlib.util.spec_from_file_location("import_joineryflow", IMPORT_CLI)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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

    def do_GET(self):
        owner = self.server.owner
        owner.gets.append({"path": self.path, "headers": dict(self.headers)})
        if owner.get_delay:
            time.sleep(owner.get_delay)
        body = owner.get_body
        raw = body if isinstance(body, str) else json.dumps(body)
        payload = raw.encode("utf-8")
        try:
            self.send_response(owner.get_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


class FakeUpstream:
    def __init__(self):
        self.records: list[dict] = []
        self.gets: list[dict] = []
        self.status = 200
        self.delay = 0.0
        self.message = "ok"
        # --verify reads through the same server; a test sets get_body to the
        # row it wants the worker to see.
        self.get_status = 200
        self.get_body: object = {}
        self.get_delay = 0.0

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
    assert "requires ML_JF_BASE_URL" in src


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


# Every column a write-back can touch. The fixture restores them all, because a
# test that applies a five-field diff would otherwise leak into the next one.
WB_COLUMNS = ("name", "unit", "unit_cost", "lead_time_days", "product_url",
              "synonyms", "product_type", "attributes", "status", "superseded_by")
JSON_COLUMNS = {"attributes"}


def _restore_values(row: tuple) -> list:
    """psycopg does not adapt a dict to jsonb on its own; send JSON text."""
    values = list(row[1:])
    for index, column in enumerate(WB_COLUMNS):
        if column in JSON_COLUMNS and values[index] is not None:
            values[index] = json.dumps(values[index])
    return values


@pytest.fixture
def clean_db(wb_conn):
    cols = ", ".join(WB_COLUMNS)
    with wb_conn.cursor() as cur:
        cur.execute(f"SELECT material_uid, {cols} FROM material")
        before = cur.fetchall()
        cur.execute("DELETE FROM material_event WHERE entity='material_change_request'")
        cur.execute("DELETE FROM material_change_request")
        # Each test starts with no upstream-write watermark; retention tests
        # prove it survives pruning, but must not leak into the next test.
        cur.execute("DELETE FROM material_writeback_state")
    yield wb_conn
    # T3 x T2 tests supersede a row to prove the guards, and the write-back cases
    # change real field values; put both back so the next test and the duplicate
    # scan see the imported catalogue again.
    sets = ", ".join(f"{column} = %s" + ("::jsonb" if column in JSON_COLUMNS else "")
                     for column in WB_COLUMNS)
    with wb_conn.cursor() as cur:
        cur.execute(f"SELECT material_uid, {cols} FROM material")
        after = {row[0]: row[1:] for row in cur.fetchall()}
        for row in before:
            if after.get(row[0]) != row[1:]:
                cur.execute(f"UPDATE material SET {sets} WHERE material_uid = %s",
                            (*_restore_values(row), row[0]))


def one_uid(conn, index=0) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT material_uid FROM material WHERE source_system='joineryflow' "
            "ORDER BY source_id LIMIT 1 OFFSET %s", (index,))
        return cur.fetchone()[0]


def board_uid(conn) -> str:
    """A board_materials row: its attribute allowlist includes colour."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT material_uid FROM material WHERE source_system='joineryflow' "
            "AND source_id LIKE 'board_materials:%' ORDER BY source_id LIMIT 1")
        return cur.fetchone()[0]


def attributes_of(conn, uid: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT attributes FROM material WHERE material_uid=%s", (uid,))
        return cur.fetchone()[0] or {}


def supersede(conn, uid: str, by_uid: str | None = None) -> None:
    """The state T2's material_merge leaves behind: a tombstone pointing home."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE material SET status='superseded', superseded_by=%s "
            "WHERE material_uid=%s", (by_uid, uid))


def same_category_pair(conn) -> tuple[str, str]:
    """Two live joineryflow rows a merge preview can consider together."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT category_code, material_uid FROM material "
            "WHERE status='active' AND source_system='joineryflow' "
            "ORDER BY category_code, source_id")
        by_category: dict[str, list[str]] = {}
        for category, uid in cur.fetchall():
            by_category.setdefault(category, []).append(uid)
    for uids in by_category.values():
        if len(uids) >= 2:
            return uids[0], uids[1]
    raise AssertionError("the snapshot has no category with two live rows")


def make_request(conn, uid: str, after: float, *, approve: bool = True) -> int:
    return make_diff_request(conn, uid, [{"path": "unit_cost", "after": after}],
                             approve=approve)


def make_diff_request(conn, uid: str, diffs: list[dict], *,
                      approve: bool = True) -> int:
    diffs_json = json.dumps(diffs)
    with conn.cursor() as cur:
        cur.execute("SELECT material_change_request_submit(%s, %s, NULL, 'alice')",
                    (uid, diffs_json))
        result = cur.fetchone()[0]
        assert result["ok"] is True, result
        rid = result["id"]
        if approve:
            cur.execute("SELECT material_change_request_decide(%s, 'approve', NULL, 'bob')",
                        (rid,))
            assert cur.fetchone()[0]["ok"] is True
    return rid


def unknown_request(conn, upstream, dsn, uid: str, after: float | None = None, *,
                    diffs: list[dict] | None = None) -> int:
    """A request whose PATCH answered 5xx: the state only a GET can settle."""
    rid = (make_diff_request(conn, uid, diffs) if diffs is not None
           else make_request(conn, uid, after))
    upstream.status = 503
    proc = run_cli(upstream, dsn, "--apply", "--request-id", str(rid))
    assert proc.returncode == 1, proc.stderr
    upstream.status = 200
    upstream.records.clear()
    assert state(conn, rid)["error"] == "uncertain_outcome"
    return rid


def state(conn, rid) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, attempts, last_status, error, applied_at, claimed_at "
            "FROM material_change_request WHERE id=%s", (rid,))
        row = cur.fetchone()
    if row is None:
        return None
    return dict(zip(("status", "attempts", "last_status", "error",
                     "applied_at", "claimed_at"), row))


def live_cost(conn, uid: str):
    """The master record's unit_cost, which a confirmed write must have updated."""
    with conn.cursor() as cur:
        cur.execute("SELECT unit_cost FROM material WHERE material_uid=%s", (uid,))
        return cur.fetchone()[0]


def count_materials(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM material")
        return cur.fetchone()[0]


def watermark(conn) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT last_upstream_write_at, last_request_id, advances "
            "FROM material_writeback_state WHERE target_system='joineryflow'")
        row = cur.fetchone()
    return dict(zip(("at", "request_id", "advances"), row)) if row else None


def run_prune(dsn, *extra):
    return subprocess.run(
        [sys.executable, str(PRUNE_CLI), "--dsn", dsn, *extra, "--json", "-"],
        capture_output=True, text=True, cwd=ROOT, timeout=60, check=False,
    )


def run_import(dsn):
    return subprocess.run(
        [sys.executable, str(IMPORT_CLI), "--dsn", dsn, "--quiet"],
        capture_output=True, text=True, cwd=ROOT, timeout=120, check=False,
    )


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
    uid = one_uid(clean_db, 0)
    rid = make_request(clean_db, uid, 903.0)
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

    # T3 x T3: the confirmed write lands in the master record, so the same diff
    # is a no-op afterwards and cannot be filed (or PATCHed) a second time.
    assert float(live_cost(clean_db, uid)) == 903.0
    with clean_db.cursor() as cur:
        cur.execute("SELECT material_change_request_submit(%s, %s, NULL, 'alice')",
                    (uid, json.dumps([{"path": "unit_cost", "after": 903.0}])))
        assert cur.fetchone()[0]["code"] == "no_change"

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
    uid = one_uid(clean_db, 0)
    rid = make_request(clean_db, uid, 905.0)
    proc = run_cli(upstream, wb_dsn, "--apply", "--request-id", str(rid))
    assert proc.returncode == 1, proc.stderr
    rep = json.loads(proc.stdout)
    assert rep["failed"] == 1 and rep["unknown"] == 0
    st = state(clean_db, rid)
    assert st["status"] == "failed" and st["last_status"] == 400
    assert "bad request" in st["error"]
    assert len(upstream.records) == 1
    assert float(live_cost(clean_db, uid)) != 905.0


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
    # a stale confirmation must not overwrite the newer master value
    assert float(live_cost(clean_db, uid)) == 777.0


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


# ---------------------------------------------------------------------------
# T3 x T2: a merge and an in-flight request cannot both be true
# ---------------------------------------------------------------------------

def test_approving_a_merged_away_material_is_refused(clean_db):
    """The tombstone keeps its field values, so the base check alone would pass."""
    uid = one_uid(clean_db, 0)
    rid = make_request(clean_db, uid, 911.0, approve=False)
    supersede(clean_db, uid, one_uid(clean_db, 1))

    with clean_db.cursor() as cur:
        cur.execute("SELECT material_change_request_decide(%s, 'approve', NULL, 'bob')",
                    (rid,))
        result = cur.fetchone()[0]
    assert result["ok"] is False, result
    assert result["code"] == "material_inactive"
    assert result["material_status"] == "superseded"
    assert state(clean_db, rid)["status"] == "pending"


def test_apply_refuses_a_merged_away_material_without_an_http_call(clean_db, upstream, wb_dsn):
    """The plan is the guard the executor trusts: no plan, no PATCH."""
    uid = one_uid(clean_db, 0)
    rid = make_request(clean_db, uid, 912.0)          # approved before the merge
    supersede(clean_db, uid, one_uid(clean_db, 1))

    proc = run_cli(upstream, wb_dsn, "--apply", "--request-id", str(rid))
    assert proc.returncode == 1, proc.stderr
    rep = json.loads(proc.stdout)
    assert rep["applied"] == 0 and rep["failed"] == 1 and rep["unknown"] == 0
    assert rep["rows"][0]["error"] == "material_inactive"
    assert upstream.records == []

    st = state(clean_db, rid)
    assert st["status"] == "failed" and st["error"] == "material_inactive"
    assert st["last_status"] is None and st["applied_at"] is None
    assert count_events(clean_db, rid) == 3           # insert, approve, settle


def test_merge_is_blocked_while_a_request_is_open(clean_db):
    a, b = same_category_pair(clean_db)
    with clean_db.cursor() as cur:
        cur.execute("SELECT material_merge_preview(%s, %s)", (a, b))
        before = cur.fetchone()[0]
    assert before is not None
    assert "open_change_request" not in before["blockers"]
    assert before["blocking_requests"] == []

    rid = make_request(clean_db, a, 913.0, approve=False)
    with clean_db.cursor() as cur:
        cur.execute("SELECT material_merge_preview(%s, %s)", (a, b))
        blocked = cur.fetchone()[0]
        cur.execute("SELECT material_merge(%s, %s, %s, 'test', true, 'tester')",
                    (a, b, b))
        merge = cur.fetchone()[0]
    assert "open_change_request" in blocked["blockers"]
    assert blocked["blocking_requests"] == [rid]
    assert blocked["can_merge"] is False
    assert merge["ok"] is False and merge["code"] == "conflict"
    assert "open_change_request" in merge["blockers"]

    with clean_db.cursor() as cur:
        cur.execute("SELECT material_change_request_decide(%s, 'reject', NULL, 'bob')",
                    (rid,))
        assert cur.fetchone()[0]["code"] == "rejected"
        cur.execute("SELECT material_merge_preview(%s, %s)", (a, b))
        after = cur.fetchone()[0]
    assert "open_change_request" not in after["blockers"]
    assert after["blocking_requests"] == []


# ---------------------------------------------------------------------------
# T3 x T3: a confirmed write lands locally, and an old snapshot must not undo it
# ---------------------------------------------------------------------------

def test_import_refuses_a_snapshot_older_than_an_applied_change(clean_db, upstream, wb_dsn):
    """The importer copies source values over the master row: a stale snapshot
    would silently revert a change that was already applied upstream."""
    uid = one_uid(clean_db, 0)
    rid = make_request(clean_db, uid, 914.0)
    assert run_cli(upstream, wb_dsn, "--apply", "--request-id", str(rid)).returncode == 0
    assert float(live_cost(clean_db, uid)) == 914.0

    before = count_materials(clean_db)
    proc = subprocess.run(
        [sys.executable, str(IMPORT_CLI), "--dsn", wb_dsn, "--quiet"],
        capture_output=True, text=True, cwd=ROOT, timeout=120, check=False,
    )
    assert proc.returncode == 1, proc.stdout
    assert "predates an upstream write" in proc.stderr
    assert "watermark" in proc.stderr
    assert "Re-export the snapshot first" in proc.stderr
    # nothing was imported, so nothing was reverted
    assert count_materials(clean_db) == before
    assert float(live_cost(clean_db, uid)) == 914.0


# ---------------------------------------------------------------------------
# 6.3: retention and the upstream-write watermark
# ---------------------------------------------------------------------------

def test_watermark_survives_pruning_and_still_refuses_the_old_snapshot(
        clean_db, upstream, wb_dsn):
    """The D19 guard must not depend on the applied rows still being there."""
    uid = one_uid(clean_db, 0)
    rid = make_request(clean_db, uid, 915.0)
    assert run_cli(upstream, wb_dsn, "--apply", "--request-id", str(rid)).returncode == 0
    wm = watermark(clean_db)
    assert wm is not None and wm["request_id"] == rid and wm["advances"] == 1

    pruned = run_prune(wb_dsn, "--older-than", "0", "--apply")
    assert pruned.returncode == 0, pruned.stderr
    rep = json.loads(pruned.stdout)
    assert rep["mode"] == "apply" and rep["deleted"] == 1
    assert state(clean_db, rid) is None                 # row is gone
    assert watermark(clean_db) == wm                    # watermark untouched

    proc = run_import(wb_dsn)
    assert proc.returncode == 1, proc.stdout
    assert "predates an upstream write" in proc.stderr
    assert "still-applied request" not in proc.stderr  # no rows left to name
    assert float(live_cost(clean_db, uid)) == 915.0    # nothing was reverted


def test_prune_dry_run_writes_nothing_and_keeps_actionable_rows(clean_db, upstream, wb_dsn):
    applied = make_request(clean_db, one_uid(clean_db, 0), 916.0)
    assert run_cli(upstream, wb_dsn, "--apply", "--request-id", str(applied)).returncode == 0
    pending = make_request(clean_db, one_uid(clean_db, 1), 917.0, approve=False)

    proc = run_prune(wb_dsn, "--older-than", "0")
    assert proc.returncode == 0, proc.stderr
    rep = json.loads(proc.stdout)
    assert rep["mode"] == "dry-run" and rep["candidates"] == 1 and rep["deleted"] == 0
    assert state(clean_db, applied)["status"] == "applied"
    assert state(clean_db, pending)["status"] == "pending"
    assert watermark(clean_db)["advances"] == 1

    real = run_prune(wb_dsn, "--older-than", "0", "--apply")
    assert real.returncode == 0, real.stderr
    assert json.loads(real.stdout)["deleted"] == 1
    assert state(clean_db, pending)["status"] == "pending"
    assert watermark(clean_db)["advances"] == 1


def test_a_fresher_snapshot_passes_the_watermark(clean_db, upstream, wb_dsn, importer):
    """The guard is a comparison, not a permanent ban on imports."""
    rid = make_request(clean_db, one_uid(clean_db, 0), 918.0)
    assert run_cli(upstream, wb_dsn, "--apply", "--request-id", str(rid)).returncode == 0

    with (ROOT / "fixtures" / "joineryflow-hartwood.jsonl").open(encoding="utf-8") as fh:
        old = json.loads(fh.readline())["exported_at"]
    assert importer.snapshot_conflict(clean_db, old)["stale"] is True
    assert importer.snapshot_conflict(clean_db, "2099-01-01T00:00:00Z")["stale"] is False


# ---------------------------------------------------------------------------
# --verify: a GET that settles a write whose outcome was unknown (docs 6.4)
# ---------------------------------------------------------------------------

def test_verification_verdict_is_strict_about_missing_columns(worker):
    verdict = worker.verification_verdict
    expected = {"unit_cost": 903.0}
    assert verdict(expected, '{"unit_cost": 903}', 200) == "upstream_matches"
    assert verdict(expected, '{"unit_cost": 904}', 200) == "upstream_differs"
    # A missing column is "we do not know", never "the upstream did not take it":
    # a wrong field name must not turn a real write into a failed request.
    assert verdict(expected, '{"um": "pc"}', 200) == "unreachable"
    assert verdict(expected, '{"unit_cost": null}', 200) == "upstream_differs"
    assert verdict(expected, 'not json', 200) == "unreachable"
    assert verdict(expected, '{"unit_cost": 903}', 503) == "unreachable"
    assert verdict(expected, "", None) == "unreachable"
    assert worker.same_value(903.0, 903) is True
    assert worker.same_value(None, None) is True
    assert worker.same_value("pc", "pc") is True


def test_verify_is_read_only_by_default(clean_db, upstream, wb_dsn):
    uid = one_uid(clean_db, 0)
    rid = unknown_request(clean_db, upstream, wb_dsn, uid, 920.0)
    before_cost = float(live_cost(clean_db, uid))
    before_events = count_events(clean_db)
    before_wm = watermark(clean_db)
    upstream.get_body = {"unit_cost": 920.0, "um": "pc"}

    proc = run_cli(upstream, wb_dsn, "--verify", str(rid))
    assert proc.returncode == 0, proc.stderr
    rep = json.loads(proc.stdout)
    assert rep["mode"] == "verify" and rep["checked"] == 1
    row = rep["rows"][0]
    assert row["verdict"] == "upstream_matches"
    assert row["outcome"] == "verification_only"
    assert row["url"].startswith("/catalog/")
    assert row["resolved"] is False

    # exactly one GET, no PATCH, and the token is still sent but not reported
    assert len(upstream.gets) == 1
    assert upstream.gets[0]["path"] == row["url"]
    assert upstream.gets[0]["headers"]["Authorization"] == "Bearer fake-token"
    assert upstream.records == []

    st = state(clean_db, rid)
    assert st["status"] == "failed" and st["error"] == "uncertain_outcome"
    assert st["claimed_at"] is None
    assert float(live_cost(clean_db, uid)) == before_cost
    assert count_events(clean_db) == before_events
    assert watermark(clean_db) == before_wm


def test_verify_resolve_marks_it_applied(clean_db, upstream, wb_dsn):
    uid = one_uid(clean_db, 0)
    rid = unknown_request(clean_db, upstream, wb_dsn, uid, 921.0)
    before_events = count_events(clean_db, rid)
    upstream.get_body = {"unit_cost": 921.0}

    proc = run_cli(upstream, wb_dsn, "--verify", str(rid), "--resolve")
    assert proc.returncode == 0, proc.stderr
    rep = json.loads(proc.stdout)
    assert rep["applied"] == 1 and rep["rows"][0]["outcome"] == "applied"
    assert rep["rows"][0]["verdict"] == "upstream_matches"

    st = state(clean_db, rid)
    assert st["status"] == "applied" and st["error"] is None
    assert st["applied_at"] is not None and st["claimed_at"] is None
    assert float(live_cost(clean_db, uid)) == 921.0
    assert watermark(clean_db)["request_id"] == rid
    assert watermark(clean_db)["advances"] == 1
    assert count_events(clean_db, rid) == before_events + 1


def test_verify_reports_a_mismatch_and_never_claims_success(clean_db, upstream, wb_dsn):
    uid = one_uid(clean_db, 0)
    rid = unknown_request(clean_db, upstream, wb_dsn, uid, 922.0)
    before_cost = float(live_cost(clean_db, uid))
    before_wm = watermark(clean_db)
    upstream.get_body = {"unit_cost": 1.0}

    proc = run_cli(upstream, wb_dsn, "--verify", str(rid), "--resolve")
    assert proc.returncode == 0, proc.stderr
    rep = json.loads(proc.stdout)
    assert rep["rows"][0]["verdict"] == "upstream_differs"
    assert rep["rows"][0]["outcome"] == "verified_not_applied"

    st = state(clean_db, rid)
    assert st["status"] == "failed" and st["error"] == "verified_not_applied"
    assert float(live_cost(clean_db, uid)) == before_cost
    assert watermark(clean_db) == before_wm

    # a confirmed miss is now a known failure, so a human may retry it
    retry = run_cli(upstream, wb_dsn, "--retry", str(rid), "--actor", "carol")
    assert retry.returncode == 0, retry.stderr
    assert json.loads(retry.stdout)["rows"][0]["outcome"] == "approved"


def test_verify_never_reads_a_missing_column_as_not_applied(clean_db, upstream, wb_dsn):
    uid = one_uid(clean_db, 0)
    rid = unknown_request(clean_db, upstream, wb_dsn, uid, 923.0)
    before_cost = float(live_cost(clean_db, uid))
    upstream.get_body = {"um": "pc"}          # the field name we guessed is absent

    proc = run_cli(upstream, wb_dsn, "--verify", str(rid), "--resolve")
    assert proc.returncode == 0, proc.stderr
    rep = json.loads(proc.stdout)
    assert rep["rows"][0]["verdict"] == "unreachable"
    assert rep["rows"][0]["outcome"] == "verification_only"
    st = state(clean_db, rid)
    assert st["status"] == "failed" and st["error"] == "uncertain_outcome"
    assert float(live_cost(clean_db, uid)) == before_cost
    assert watermark(clean_db) is None


def test_verify_refuses_a_request_it_cannot_act_on(clean_db, upstream, wb_dsn):
    pending = make_request(clean_db, one_uid(clean_db, 0), 924.0, approve=False)
    proc = run_cli(upstream, wb_dsn, "--verify", str(pending))
    assert proc.returncode == 0, proc.stderr
    rep = json.loads(proc.stdout)
    assert rep["skipped"] == 1 and rep["failed"] == 0
    assert rep["rows"][0]["error"] == "not_candidate"
    assert upstream.gets == []                # no request was spent on it
    assert state(clean_db, pending)["status"] == "pending"


def test_verify_resolves_a_stale_base_2xx(clean_db, upstream, wb_dsn):
    """The 2xx cases are the reason the watermark tracks 2xx, not local writes."""
    uid = one_uid(clean_db, 0)
    rid = make_request(clean_db, uid, 925.0)
    with clean_db.cursor() as cur:
        cur.execute("UPDATE material SET unit_cost = 777 WHERE material_uid=%s", (uid,))
    assert run_cli(upstream, wb_dsn, "--apply", "--request-id", str(rid)).returncode == 1
    assert state(clean_db, rid)["error"] == "stale_base"
    assert watermark(clean_db)["advances"] == 1     # the 2xx already advanced it
    upstream.records.clear()

    upstream.get_body = {"unit_cost": 925.0}
    proc = run_cli(upstream, wb_dsn, "--verify", str(rid), "--resolve")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["rows"][0]["outcome"] == "applied"
    assert state(clean_db, rid)["status"] == "applied"
    assert float(live_cost(clean_db, uid)) == 925.0
    assert watermark(clean_db)["advances"] == 2
    assert upstream.records == []                    # resolved without a PATCH


def test_verify_mode_guards(clean_db, upstream, wb_dsn):
    both = run_cli(upstream, wb_dsn, "--verify", "1", "--apply")
    assert both.returncode == 2
    assert "--verify cannot be combined" in both.stderr
    alone = run_cli(upstream, wb_dsn, "--resolve")
    assert alone.returncode == 2
    assert "--resolve requires --verify" in alone.stderr


# ---------------------------------------------------------------------------
# The attributes namespace and the fields only the contract used to cover
# ---------------------------------------------------------------------------

def test_an_attribute_write_reaches_the_master_row(clean_db, upstream, wb_dsn):
    uid = board_uid(clean_db)
    before = attributes_of(clean_db, uid)
    rid = make_diff_request(clean_db, uid,
                            [{"path": "attributes.colour", "after": "Red"}])

    proc = run_cli(upstream, wb_dsn, "--apply", "--request-id", str(rid))
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["applied"] == 1

    # the PATCH body carries the bare upstream column, never the UMR path
    assert len(upstream.records) == 1
    assert upstream.records[0]["body"] == {"colour": "Red"}
    assert upstream.records[0]["path"].startswith("/catalog/board-materials/")

    after = attributes_of(clean_db, uid)
    assert after["colour"] == "Red"
    for key, value in before.items():
        assert after[key] == value          # an unrelated key is untouched
    assert state(clean_db, rid)["status"] == "applied"


def test_clearing_an_attribute_drops_the_key(clean_db, upstream, wb_dsn):
    uid = board_uid(clean_db)
    set_it = make_diff_request(clean_db, uid,
                               [{"path": "attributes.colour", "after": "Red"}])
    assert run_cli(upstream, wb_dsn, "--apply", "--request-id", str(set_it)).returncode == 0
    assert attributes_of(clean_db, uid)["colour"] == "Red"

    clear_it = make_diff_request(clean_db, uid,
                                 [{"path": "attributes.colour", "after": None}])
    proc = run_cli(upstream, wb_dsn, "--apply", "--request-id", str(clear_it))
    assert proc.returncode == 0, proc.stderr
    assert upstream.records[-1]["body"] == {"colour": None}

    after = attributes_of(clean_db, uid)
    # a JSON null left behind would make `attributes ? 'colour'` a lie
    assert "colour" not in after
    # and the fingerprint agrees, so the same clear cannot be filed twice
    with clean_db.cursor() as cur:
        cur.execute("SELECT material_change_request_submit(%s, %s, NULL, 'alice')",
                    (uid, json.dumps([{"path": "attributes.colour", "after": None}])))
        assert cur.fetchone()[0]["code"] == "no_change"


def test_verify_resolves_an_attribute_write(clean_db, upstream, wb_dsn):
    uid = board_uid(clean_db)
    rid = unknown_request(clean_db, upstream, wb_dsn, uid,
                          diffs=[{"path": "attributes.colour", "after": "Blue"}])
    upstream.get_body = {"colour": "Blue"}

    proc = run_cli(upstream, wb_dsn, "--verify", str(rid), "--resolve")
    assert proc.returncode == 0, proc.stderr
    rep = json.loads(proc.stdout)
    assert rep["rows"][0]["verdict"] == "upstream_matches"
    assert rep["rows"][0]["outcome"] == "applied"
    assert attributes_of(clean_db, uid)["colour"] == "Blue"


def test_a_five_field_write_lands_as_one_patch(clean_db, upstream, wb_dsn):
    uid = one_uid(clean_db, 10)
    rid = make_diff_request(clean_db, uid, [
        {"path": "name", "after": "Renamed by a test"},
        {"path": "unit", "after": "box"},
        {"path": "lead_time_days", "after": 7},
        {"path": "product_url", "after": "https://example.test/five"},
        {"path": "synonyms", "after": ["hinge 110", "soft close"]},
    ])

    proc = run_cli(upstream, wb_dsn, "--apply", "--request-id", str(rid))
    assert proc.returncode == 0, proc.stderr
    assert len(upstream.records) == 1
    assert upstream.records[0]["body"] == {
        "description": "Renamed by a test",
        "um": "box",
        "default_lead_time_days": 7,
        "product_website": "https://example.test/five",
        "synonyms": ["hinge 110", "soft close"],
    }

    with clean_db.cursor() as cur:
        cur.execute("SELECT name, unit, lead_time_days, product_url, synonyms "
                    "FROM material WHERE material_uid=%s", (uid,))
        name, unit, lead, url, synonyms = cur.fetchone()
    assert (name, unit, lead, url) == ("Renamed by a test", "box", 7,
                                       "https://example.test/five")
    assert synonyms == ["hinge 110", "soft close"]
