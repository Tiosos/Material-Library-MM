#!/usr/bin/env python3
"""CLI: import a committed joineryflow JSONL snapshot into Material Library v2.

Design: docs/etl-joineryflow.md sections 9-11 (step 3 of the delivery plan).

    python scripts/import_joineryflow.py --dsn "$ML_DSN" \
      --snapshot fixtures/joineryflow-hartwood.jsonl --mapping mapping/joineryflow.yaml
    python scripts/import_joineryflow.py --dsn "$ML_DSN" --dry-run --json report.json

Reading and building are pure functions shared with the tests. Only an import
without --dry-run opens a database; the whole file is one transaction, rolled
back on failure, following the mapping's all-or-nothing batch convention.

Exit codes: 0 success, 1 row errors or a database failure, 2 usage.

The committed snapshot is the only input. This CLI never connects to the
joineryflow database itself; that is the exporter's job (step 0).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingest.importer import apply as apply_drafts
from ingest.joineryflow import (
    Rules,
    RulesError,
    SnapshotError,
    build_drafts,
    build_report,
    check_contract,
    load_rules,
    read_snapshot,
)
from ingest.mapping import Mapping

USAGE = 2
FAILURE = 1

# A snapshot older than a write-back that already landed would silently revert
# it, because the importer copies source values over the master row. The check
# is a hard error: re-export is cheap, losing an applied change is not.
STALE_SNAPSHOT_SQL = """
SELECT r.id, m.source_id, r.applied_at
  FROM material_change_request r
  JOIN material m ON m.material_uid = r.material_uid
 WHERE r.status = 'applied'
   AND r.applied_at IS NOT NULL
   AND r.applied_at > %(exported_at)s::timestamptz
 ORDER BY r.id
"""


class StaleSnapshotError(RuntimeError):
    """The snapshot predates a write-back request that was already applied."""


def make_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--snapshot", default=None,
                    help="JSONL snapshot (default: the mapping's source.snapshot)")
    ap.add_argument("--mapping", default=None,
                    help="mapping rules (default: mapping/joineryflow.yaml)")
    ap.add_argument("--dsn", default=os.environ.get("ML_DSN"),
                    help="PostgreSQL DSN (default: $ML_DSN)")
    ap.add_argument("--dry-run", action="store_true",
                    help="build and report without writing anything")
    ap.add_argument("--limit", type=int, default=0,
                    help="import only the first N snapshot rows (0 = all)")
    ap.add_argument("--include-archived", action="store_true",
                    help="override the header flag and import archived rows as status='archived'")
    ap.add_argument("--json", dest="json_out", nargs="?", const="-", metavar="FILE",
                    help="write the full report to FILE, or print it as JSON to stdout")
    ap.add_argument("--quiet", action="store_true", help="print only one summary line")
    return ap


def build_plan(args: argparse.Namespace) -> tuple[dict, list, Rules]:
    """Pure part: parse, build drafts and assemble the report. No database."""
    rules = load_rules(args.mapping)
    snapshot = read_snapshot(args.snapshot or rules.snapshot_path)
    source_rows = len(snapshot.rows)
    if args.limit:
        snapshot = replace(snapshot, rows=snapshot.rows[:args.limit])
    include_archived = args.include_archived or bool(snapshot.header.get("include_archived"))
    drafts, meta = build_drafts(snapshot, rules, include_archived=include_archived)
    report = build_report(drafts, meta, rules)

    report.update({
        "snapshot": str(snapshot.path),
        "workspace": meta["workspace"],
        "exported_at": snapshot.header.get("exported_at"),
        "source_rows": source_rows,
        "limit": args.limit,
        "include_archived": include_archived,
        "dry_run": args.dry_run,
        "contract": {"checked": False, "ok": None, "problems": []},
    })
    # The contract is stated for a complete, live-only import. A limited run or
    # one that includes archived rows deliberately cannot satisfy those numbers.
    if not args.limit and not include_archived:
        problems = check_contract(report, rules)
        report["contract"] = {"checked": True, "ok": not problems, "problems": problems}
    return report, drafts, rules


def stale_snapshot_conflicts(conn, exported_at: str) -> list[tuple]:
    """Applied write-backs that happened after the snapshot was exported."""
    with conn.cursor() as cur:
        cur.execute(STALE_SNAPSHOT_SQL, {"exported_at": exported_at})
        return cur.fetchall()


def _stale_message(exported_at: str, conflicts: list[tuple]) -> str:
    ids = ", ".join(str(row[0]) for row in conflicts[:5])
    more = "" if len(conflicts) <= 5 else f" (+{len(conflicts) - 5} more)"
    first = conflicts[0][1]
    return (
        f"snapshot exported_at {exported_at} predates {len(conflicts)} applied "
        f"write-back request(s) [{ids}{more}]; the first row is {first}. "
        "Re-export the snapshot first: an older snapshot would overwrite a change "
        "that was already applied upstream."
    )


def execute(dsn: str, drafts: list, rules: Rules, *, connect=None,
            exported_at: str | None = None) -> dict:
    """One transaction for the whole file. ``connect`` is injectable for tests."""
    if connect is None:
        import psycopg
        connect = psycopg.connect
    mapping = Mapping({"vendor": rules.source_system,
                       "source_system": rules.source_system, "defaults": {}})
    conn = connect(dsn)
    try:
        if exported_at:
            conflicts = stale_snapshot_conflicts(conn, exported_at)
            if conflicts:
                raise StaleSnapshotError(_stale_message(exported_at, conflicts))
        stats = apply_drafts(conn, drafts, mapping)
        conn.commit()
        return stats
    except Exception:
        conn.rollback()
        raise
    finally:
        close = getattr(conn, "close", None)
        if close is not None:
            close()


def emit(report: dict, args: argparse.Namespace) -> None:
    payload = json.dumps(report, sort_keys=True, indent=2, default=str) + "\n"
    stdout_json = args.json_out == "-"
    if args.json_out and not stdout_json:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload, encoding="utf-8")
    if stdout_json:
        print(payload, end="")
        return

    status = ("FAIL" if report["errors"] or report.get("database_error")
              or report.get("stale_snapshot") else "OK")
    mode = "dry-run" if args.dry_run else "applied"
    stats = report.get("stats") or {}

    if args.quiet:
        print(f"{status} mode={mode} snapshot={Path(report['snapshot']).name} "
              f"rows={report['rows']} errors={report['errors']} "
              f"new={stats.get('new', '-')} updated={stats.get('updated', '-')} "
              f"prices={report['prices']} families={report['families']}")
        return

    print(f"{status}  {report['snapshot']}  ({mode})")
    print("    "
          f"{report['rows']} rows, {report['errors']} errors, "
          f"{report['warnings']} warnings, {report['archived']} archived, "
          f"{report['categories_with_rows']} categories, "
          f"{report['derived_thickness_measures']} derived measures, "
          f"{report['prices']} prices, {report['cv_identifiers']} cv codes, "
          f"{report['families']} families, {report['identifier_collisions']} collisions")
    if stats:
        print("    "
              f"new={stats['new']} updated={stats['updated']} skipped={stats['skipped']} "
              f"measures={stats['measures']} identifiers={stats['identifiers']} "
              f"families={stats['families']}")
    for problem in report["contract"]["problems"]:
        print(f"  - contract: {problem}")
    for warning in report["meta_warnings"]:
        print(f"  - warning: {warning}")
    for row in report["error_rows"]:
        print(f"  - row {row['row']}: {', '.join(row['errors'])}")
    if report.get("stale_snapshot"):
        print(f"  - stale snapshot: {report['stale_snapshot']}")
    if report.get("database_error"):
        print(f"  - database: {report['database_error']}")
    if args.json_out:
        print(f"report -> {args.json_out}")


def main(argv: list[str] | None = None, *, connect=None) -> int:
    args = make_parser().parse_args(argv)
    if args.limit < 0:
        print("error: --limit must be zero or positive", file=sys.stderr)
        return USAGE
    if not args.dry_run and not args.dsn:
        print("error: an import needs --dsn (or $ML_DSN); use --dry-run to build without a database",
              file=sys.stderr)
        return USAGE

    try:
        report, drafts, rules = build_plan(args)
    except (SnapshotError, RulesError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return FAILURE

    # All-or-nothing: a single bad row aborts the batch before touching the DB.
    if not args.dry_run and not report["errors"]:
        try:
            report["stats"] = execute(args.dsn, drafts, rules, connect=connect,
                                      exported_at=report.get("exported_at"))
        except StaleSnapshotError as exc:
            report["stale_snapshot"] = str(exc)
            print(f"stale snapshot: {exc}", file=sys.stderr)
        except Exception as exc:                     # psycopg errors, bad DSN, ...
            report["database_error"] = str(exc)
            print(f"database error: {exc}", file=sys.stderr)

    emit(report, args)
    if report["errors"] or report.get("database_error") or report.get("stale_snapshot"):
        return FAILURE
    return 0


if __name__ == "__main__":
    sys.exit(main())
