#!/usr/bin/env python3
"""CLI: import supplier spreadsheets into Material Library v2.

    # check the mapping first, without connecting to the DB
    python scripts/import_file.py --file ingest/samples/blum_hinges.csv --mapping mappings/blum.yaml

    # dry run against the DB (reads the DB to decide new/update, but does not commit)
    python scripts/import_file.py --file ... --mapping ... --dsn "$env:ML_DSN"

    # actually write
    python scripts/import_file.py --file ... --mapping ... --dsn "$env:ML_DSN" --apply

DSN example: postgresql://postgres:postgres@127.0.0.1:55432/ml
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingest.importer import apply as apply_drafts, build_drafts, plan_report, read_table
from ingest.mapping import load_mapping


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="CSV / XLSX")
    ap.add_argument("--mapping", required=True, help="mappings/<vendor>.yaml")
    ap.add_argument("--dsn", default=os.environ.get("ML_DSN"))
    ap.add_argument("--apply", action="store_true", help="write to the database (dry-run by default)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", dest="json_out", help="write the report as JSON")
    args = ap.parse_args(argv)

    mapping = load_mapping(args.mapping)
    headers, rows = read_table(args.file)
    if args.limit:
        rows = rows[: args.limit]
    drafts, meta = build_drafts(headers, rows, mapping)

    report = plan_report(drafts, meta)
    print(f"vendor={mapping.vendor} source={mapping.source_system} file={Path(args.file).name}")
    print(f"rows={report['rows']} ok={report['ok']} errors={report['errors']} warnings={report['warnings']}")
    print(f"headers matched: {len(meta['resolved'])}/{len(headers)}")
    for field, header in meta["resolved"].items():
        print(f"  {field:12s} <- {header!r}")
    if meta["unmatched"]:
        print(f"  unmatched (→ attributes): {meta['unmatched']}")

    if report["error_rows"]:
        print("errors:")
        for e in report["error_rows"]:
            print(f"  row {e['row']}: {e['errors']}")

    for s in report["sample"]:
        print(f"  [{s['row']}] {s['part_no']} | {s['name']} | family={s['family_name']} | "
              f"measures={[m['key'] for m in s['measures']]}")

    if args.apply:
        if not args.dsn:
            raise SystemExit("--apply requires --dsn (or the ML_DSN environment variable)")
        import psycopg
        with psycopg.connect(args.dsn) as conn:
            stats = apply_drafts(conn, drafts, mapping)
            conn.commit()
        print("applied:", stats)
        report["stats"] = stats
    elif args.dsn:
        print("dry-run (nothing written). Add --apply to commit.")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"report -> {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
