#!/usr/bin/env python3
"""CLI: prune terminal write-back requests after the retention window (T3, 6.3).

    python scripts/prune_change_requests.py --dsn "$ML_WRITEBACK_DSN" --json -
    python scripts/prune_change_requests.py --dsn "$ML_WRITEBACK_DSN" --older-than 90 --apply

Dry-run is the default and makes zero writes, matching the executor's rule.
Only `applied` and `rejected` rows are candidates: `pending`, `approved` and
`failed` are still actionable. The audit trail (`material_event`) and the
upstream-write watermark (`material_writeback_state`) are never touched, so a
stale snapshot stays refused even after the requests that caused the write have
been pruned.

Exit codes: 0 success, 1 database failure, 2 usage or configuration error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

USAGE = 2
FAILURE = 1
DEFAULT_DAYS = 90

# Terminal states only. The watermark, not these rows, is what protects the
# import path from an old snapshot (docs/t3-writeback.md 6.3).
CANDIDATES_SQL = """
SELECT id, status, coalesce(applied_at, decided_at, requested_at) AS closed_at
  FROM material_change_request
 WHERE status IN ('applied', 'rejected')
   AND coalesce(applied_at, decided_at, requested_at)
         < now() - make_interval(days => %s)
 ORDER BY id
"""


def make_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsn", default=None,
                    help="PostgreSQL DSN (default: $ML_WRITEBACK_DSN, then $ML_DSN)")
    ap.add_argument("--older-than", type=int, default=DEFAULT_DAYS, metavar="DAYS",
                    help="prune terminal requests closed more than DAYS ago (default 90)")
    ap.add_argument("--apply", action="store_true",
                    help="delete the candidates; without it this is a read-only dry run")
    ap.add_argument("--json", dest="json_out", nargs="?", const="-", metavar="FILE",
                    help="write the full report to FILE, or print it as JSON to stdout")
    return ap


def _new_connection(dsn: str):
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(dsn, autocommit=True, row_factory=dict_row)


def run(args: argparse.Namespace, dsn: str, *, connect=None) -> dict:
    connect = connect or _new_connection
    conn = connect(dsn)
    report = {
        "ok": True,
        "mode": "apply" if args.apply else "dry-run",
        "older_than_days": args.older_than,
        "candidates": 0,
        "deleted": 0,
        "rows": [],
        "watermark_untouched": True,
        "audit_untouched": True,
    }
    try:
        with conn.cursor() as cur:
            cur.execute(CANDIDATES_SQL, (args.older_than,))
            rows = cur.fetchall()
            ids = [row["id"] for row in rows]
            report["candidates"] = len(ids)
            report["rows"] = [
                {"id": row["id"], "status": row["status"],
                 "closed_at": str(row["closed_at"])}
                for row in rows[:10]
            ]
            if args.apply and ids:
                cur.execute(
                    "DELETE FROM material_change_request WHERE id = ANY(%s::bigint[])",
                    (ids,),
                )
                report["deleted"] = cur.rowcount
    finally:
        close = getattr(conn, "close", None)
        if close is not None:
            close()
    return report


def emit(report: dict, args: argparse.Namespace) -> None:
    if args.json_out:
        payload = json.dumps(report, sort_keys=True, indent=2, default=str) + "\n"
        if args.json_out == "-":
            print(payload, end="")
            return
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload, encoding="utf-8")
    print(f"OK mode={report['mode']} older_than={report['older_than_days']}d "
          f"candidates={report['candidates']} deleted={report['deleted']}")


def main(argv: list[str] | None = None, *, connect=None) -> int:
    args = make_parser().parse_args(argv)
    if args.older_than < 0:
        print("error: --older-than must be zero or positive", file=sys.stderr)
        return USAGE
    dsn = args.dsn or os.environ.get("ML_WRITEBACK_DSN") or os.environ.get("ML_DSN")
    if not dsn:
        print("error: pruning needs --dsn (or $ML_WRITEBACK_DSN / $ML_DSN)",
              file=sys.stderr)
        return USAGE
    try:
        report = run(args, dsn, connect=connect)
    except Exception as exc:                       # psycopg errors, bad DSN, ...
        print(f"error: {exc}", file=sys.stderr)
        return FAILURE
    emit(report, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
