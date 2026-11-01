#!/usr/bin/env python3
"""CLI: fill the duplicate review queue.

    # find candidate duplicates against a database
    python scripts/duplicate_scan.py --dsn "$ML_DSN"

    # tighter threshold, and keep the pairs in a file for a review pass
    python scripts/duplicate_scan.py --dsn "$ML_DSN" --threshold 0.85 --json .tmp/dups.json

The scan only writes candidate rows with status 'open'; deciding what to do with a pair is a
separate, human step (`POST /v1/duplicates/{uid_a}/{uid_b}/merge`, or `scripts/merge_materials.py`).

DSN example: postgresql://postgres:postgres@127.0.0.1:55432/ml
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fill the duplicate review queue.")
    ap.add_argument("--dsn", default=os.environ.get("ML_DSN"),
                    help="PostgreSQL DSN; defaults to ML_DSN")
    ap.add_argument("--threshold", type=float, default=0.80,
                    help="name similarity floor, 0-1 (default 0.80)")
    ap.add_argument("--limit", type=int, default=500,
                    help="maximum pairs considered in this run (default 500)")
    ap.add_argument("--json", dest="json_out", help="write the report as JSON")
    ap.add_argument("--quiet", action="store_true", help="print nothing on success")
    args = ap.parse_args(argv)

    if not args.dsn:
        print("error: no DSN; pass --dsn or set ML_DSN", file=sys.stderr)
        return 2

    import psycopg

    try:
        with psycopg.connect(args.dsn, autocommit=True) as conn:
            row = conn.execute(
                "SELECT material_duplicate_scan(%s::numeric, %s::integer)",
                (args.threshold, args.limit),
            ).fetchone()
    except psycopg.Error as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    report = (row[0] if row else None) or {}
    pairs = report.get("candidates") or []

    if not args.quiet:
        print(f"scanned {report.get('pairs', 0)} pairs, "
              f"inserted {report.get('inserted', 0)}, updated {report.get('updated', 0)} "
              f"(threshold {report.get('threshold', args.threshold)})")
        for pair in pairs[:20]:
            print(f"  {pair['score']:.4f}  {pair['uid_a']}  {pair['uid_b']}")
        if len(pairs) > 20:
            print(f"  ... {len(pairs) - 20} more")

    if args.json_out:
        payload = {
            "threshold": report.get("threshold", args.threshold),
            "inserted": report.get("inserted", 0),
            "updated": report.get("updated", 0),
            "pairs": pairs,
        }
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        if not args.quiet:
            print(f"report written to {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
