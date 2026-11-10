"""Pure-Python unit tests for scripts/import_joineryflow.py (ETL step 3).

No PostgreSQL is needed: a stateful recording connection stands in for the
database, so the CLI orchestration, the single-transaction rule and the
second-import idempotency are all exercised in the CI static job.

Run with:
    python -m unittest discover -s tests -p "test_unit_*.py" -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SPEC = importlib.util.spec_from_file_location(
    "import_joineryflow", ROOT / "scripts" / "import_joineryflow.py")
cli = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(cli)

from ingest.joineryflow import DEFAULT_SNAPSHOT, load_rules

RULES = load_rules()
CATEGORIES = sorted({spec["category_code"] for spec in RULES.data["tables"].values()})


class FakeDB:
    """Minimal PostgreSQL stand-in: it remembers identifiers and families."""

    def __init__(self, *, fail_on_insert=False):
        self.categories = CATEGORIES
        self.identifiers: dict[tuple[str, str, str], str] = {}
        self.families: dict[str, str] = {}
        self.calls: list[tuple[str, tuple]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0
        self.fail_on_insert = fail_on_insert
        self.connection = _Connection(self)

    def sql(self, prefix: str) -> list[tuple[str, tuple]]:
        return [(s, p) for s, p in self.calls if s.startswith(prefix)]


class _Cursor:
    def __init__(self, db: FakeDB):
        self.db = db
        self._next = None
        self._rows: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        p = tuple(params or ())
        flat = " ".join(sql.split())
        self.db.calls.append((sql, p))
        self._next = None
        self._rows = []
        if flat.startswith("SELECT code FROM material_category"):
            self._rows = [(c,) for c in self.db.categories]
        elif flat.startswith("SELECT material_uid FROM material_identifier"):
            uid = self.db.identifiers.get((p[0], p[1], p[2]))
            self._next = (uid,) if uid else None
        elif flat.startswith("SELECT family_id FROM material_family"):
            fid = self.db.families.get(p[0])
            self._next = (fid,) if fid else None
        elif flat.startswith("INSERT INTO party"):
            self._next = (1,)
        elif flat.startswith("INSERT INTO material ("):
            if self.db.fail_on_insert:
                raise RuntimeError("simulated insert failure")
        elif flat.startswith("INSERT INTO material_identifier"):
            self.db.identifiers[(p[1], p[2], p[3])] = p[0]
        elif flat.startswith("INSERT INTO material_family"):
            self.db.families[p[1]] = p[0]

    def fetchone(self):
        return self._next

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, db: FakeDB):
        self.db = db
        self._cursor = _Cursor(db)

    def cursor(self):
        return self._cursor

    def commit(self):
        self.db.commits += 1

    def rollback(self):
        self.db.rollbacks += 1

    def close(self):
        self.db.closes += 1


def run(argv, *, db=None, dsn="fake://db"):
    """Run the CLI, capturing stdout/stderr. Returns (exit_code, stdout, stderr)."""
    args = list(argv)
    if db is not None and not any(a.startswith("--dsn") for a in args):
        args += ["--dsn", dsn]
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(args, connect=(lambda _dsn: db.connection) if db else None)
    return code, out.getvalue(), err.getvalue()


class DryRunTests(unittest.TestCase):
    def test_dry_run_needs_no_dsn_and_never_connects(self):
        def forbidden(_dsn):
            raise AssertionError("dry-run must not open a database")

        out, err = StringIO(), StringIO()
        with patch.dict(os.environ, {"ML_DSN": ""}), \
             redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--dry-run", "--quiet"], connect=forbidden)
        self.assertEqual(code, 0)
        self.assertIn("mode=dry-run", out.getvalue())
        self.assertIn("rows=18 errors=0", out.getvalue())

    def test_dry_run_json_stdout_is_machine_readable(self):
        out, err = StringIO(), StringIO()
        with patch.dict(os.environ, {"ML_DSN": ""}), \
             redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--dry-run", "--json"])
        self.assertEqual(code, 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["rows"], 18)
        self.assertEqual(report["errors"], 0)
        self.assertEqual(report["prices"], 13)       # D2, not the stale 14
        self.assertEqual(report["families"], 17)
        self.assertTrue(report["dry_run"])
        self.assertTrue(report["contract"]["checked"])
        self.assertTrue(report["contract"]["ok"])
        self.assertNotIn("stats", report)

    def test_json_file_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "report.json"
            code, out, _ = run(["--dry-run", "--json", str(target)])
            self.assertEqual(code, 0)
            self.assertIn("report ->", out)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["rows"], 18)

    def test_quiet_prints_exactly_one_line(self):
        code, out, _ = run(["--dry-run", "--quiet"])
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_limit_and_contract_is_skipped(self):
        out = StringIO()
        with redirect_stdout(out), redirect_stderr(StringIO()):
            code = cli.main(["--dry-run", "--json", "--limit", "3"])
        self.assertEqual(code, 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["rows"], 3)
        self.assertEqual(report["source_rows"], 18)
        self.assertEqual(report["limit"], 3)
        self.assertFalse(report["contract"]["checked"])

    def test_include_archived_warns_when_the_snapshot_has_none(self):
        out = StringIO()
        with redirect_stdout(out), redirect_stderr(StringIO()):
            code = cli.main(["--dry-run", "--json", "--include-archived"])
        report = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(report["include_archived"])
        self.assertTrue(any("include_archived requested" in w
                            for w in report["meta_warnings"]))
        self.assertFalse(report["contract"]["checked"])

    def test_usage_errors(self):
        with patch.dict(os.environ, {"ML_DSN": ""}):
            code, _, err = run([])
        self.assertEqual(code, 2)
        self.assertIn("needs --dsn", err)
        code, _, err = run(["--dry-run", "--limit", "-1"])
        self.assertEqual(code, 2)
        self.assertIn("--limit", err)

    def test_unreadable_snapshot_is_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = run(["--dry-run", "--snapshot", str(Path(tmp) / "missing.jsonl")])
        self.assertEqual(code, 1)
        self.assertIn("cannot read snapshot", err)


class ApplyTests(unittest.TestCase):
    def test_first_import_writes_everything_in_one_transaction(self):
        db = FakeDB()
        code, out, _ = run(["--quiet"], db=db)
        self.assertEqual(code, 0)
        self.assertIn("new=18 updated=0", out)
        self.assertEqual(db.commits, 1)
        self.assertEqual(db.rollbacks, 0)
        self.assertEqual(db.closes, 1)
        self.assertEqual(len(db.sql("INSERT INTO material (")), 18)
        self.assertEqual(len(db.sql("INSERT INTO material_price")), 13)

    def test_second_import_is_a_noop_for_new_rows(self):
        db = FakeDB()
        first, _, _ = run(["--quiet"], db=db)
        self.assertEqual(first, 0)
        out = StringIO()
        with redirect_stdout(out), redirect_stderr(StringIO()):
            second = cli.main(["--dsn", "fake://db", "--json"],
                              connect=lambda _dsn: db.connection)
        report = json.loads(out.getvalue())
        self.assertEqual(second, 0)
        self.assertEqual(report["stats"]["new"], 0)        # design section 13, step 3
        self.assertEqual(report["stats"]["updated"], 18)
        self.assertEqual(db.commits, 2)
        self.assertEqual(db.rollbacks, 0)
        self.assertEqual(len(db.sql("INSERT INTO material (")), 18)   # no extra inserts

    def test_contract_numbers_survive_the_apply_path(self):
        db = FakeDB()
        out = StringIO()
        with redirect_stdout(out), redirect_stderr(StringIO()):
            code = cli.main(["--dsn", "fake://db", "--json"],
                            connect=lambda _dsn: db.connection)
        report = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertFalse(report["dry_run"])
        self.assertTrue(report["contract"]["ok"])
        self.assertEqual(report["derived_thickness_measures"], 10)
        self.assertEqual(report["cv_identifiers"], 2)
        self.assertEqual(report["source_payload_on_all_rows"], True)

    def test_row_errors_abort_the_batch_before_touching_the_database(self):
        lines = DEFAULT_SNAPSHOT.read_text(encoding="utf-8").splitlines()
        changed = False
        for i, line in enumerate(lines):
            obj = json.loads(line)
            if obj.get("kind") == "row":
                obj["row"]["um"] = "furlong"       # not a seeded unit code
                lines[i] = json.dumps(obj, sort_keys=True, ensure_ascii=False)
                changed = True
                break
        self.assertTrue(changed)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.jsonl"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            db = FakeDB()
            code, out, _ = run(["--snapshot", str(path), "--quiet"], db=db)
            self.assertEqual(code, 1)
            self.assertIn("errors=1", out)
            self.assertEqual(db.commits, 0)
            self.assertEqual(db.calls, [])

    def test_database_failure_rolls_back_and_reports_exit_one(self):
        db = FakeDB(fail_on_insert=True)
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--dsn", "fake://db", "--json"],
                            connect=lambda _dsn: db.connection)
        report = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertIn("simulated insert failure", report["database_error"])
        self.assertEqual(db.rollbacks, 1)
        self.assertEqual(db.commits, 0)
        self.assertIn("database error", err.getvalue())


if __name__ == "__main__":
    unittest.main()
