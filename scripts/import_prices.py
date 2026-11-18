#!/usr/bin/env python3
"""CLI: import a supplier price list into material_price, joined by (supplier, code).

    # review: which rows match an existing material, which are unmatched, what would be written
    python scripts/import_prices.py --file prices.csv --source-system blum_catalogue \
           --valid-from 2026-01-01 --supplier Blum

    # write (idempotent on the price unique key)
    python scripts/import_prices.py --file prices.xlsx --sheet "Prices 2026" --header-row 0 \
           --source-system blum_catalogue --valid-from 2026-01-01 --dsn "$ML_DSN" --apply

No material rows are created: an unmatched code is reported, never inserted. `--valid-from` is
required unless the file has a per-row date column, so a price always has an explicit effective
date and different dates are kept as separate history rows.

DSN example: postgresql://postgres:postgres@127.0.0.1:5432/ml
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingest.auto_mapping import detect_header_row
from ingest.importer import list_sheets, matrix_to_table, read_matrix
from ingest.price_import import apply_prices, build_price_rows, price_report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", required=True, help="CSV / XLSX price list")
    ap.add_argument("--dsn", default=os.environ.get("ML_DSN"))
    ap.add_argument("--apply", action="store_true", help="write prices (dry-run by default)")
    ap.add_argument("--source-system", required=True,
                    help="source_system used by the catalogue's material_identifier")
    ap.add_argument("--valid-from", help="effective date YYYY-MM-DD (or a per-row date column)")
    ap.add_argument("--supplier", help="default supplier name when the file has no supplier column")
    ap.add_argument("--currency", default="AUD")
    ap.add_argument("--cost-basis", default="ex_gst", choices=["ex_gst", "inc_gst", "ex_tax", "inc_tax"])
    ap.add_argument("--code-column", help="force this header as the product code")
    ap.add_argument("--header-row", type=int, default=1,
                    help="1-based physical header row (0 = auto-detect)")
    ap.add_argument("--sheet", help="XLSX worksheet name")
    ap.add_argument("--list-sheets", action="store_true", help="print sheet names and exit")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", dest="json_out", help="write the report as JSON")
    args = ap.parse_args(argv)

    if args.list_sheets:
        print(f"sheets in {Path(args.file).name}: {list_sheets(args.file) or 'none (not an XLSX)'}")
        return 0
    if args.header_row < 0:
        raise SystemExit("--header-row must be >= 1 (or 0 for auto-detect)")
    if not args.valid_from:
        raise SystemExit("--valid-from YYYY-MM-DD is required (or include a date column in the file)")

    matrix = read_matrix(args.file, args.sheet)
    header_row = detect_header_row(matrix) if args.header_row == 0 else args.header_row
    if args.header_row == 0:
        print(f"[auto] detected header row {header_row} of {len(matrix)}")
    headers, rows = matrix_to_table(matrix, header_row)
    if args.limit:
        rows = rows[: args.limit]

    drafts, meta = build_price_rows(
        headers, rows,
        supplier_default=args.supplier,
        valid_from_default=args.valid_from,
        currency_default=args.currency,
        cost_basis_default=args.cost_basis,
        code_column=args.code_column,
    )
    report = price_report(drafts, meta)
    report["source_system"] = args.source_system
    report["header_row"] = header_row
    print(f"file={Path(args.file).name} source={args.source_system} valid_from={args.valid_from}")
    print(f"rows={report['rows']} ok={report['ok']} errors={report['errors']}")
    for role, header in meta["resolved"].items():
        print(f"  {role:12s} <- {header!r}" if header else f"  {role:12s} <- (default/CLI)")
    for e in report["error_rows"]:
        print(f"  row {e['row']}: {e['errors']}")

    if args.apply:
        if not args.dsn:
            raise SystemExit("--apply requires --dsn (or the ML_DSN environment variable)")
        import psycopg
        with psycopg.connect(args.dsn) as conn:
            stats = apply_prices(conn, drafts, source_system=args.source_system)
            conn.commit()
        report["stats"] = stats
        print("applied:", stats)
        for d in drafts:
            if d.material_uid and not d.errors:
                print(f"  row {d.row_no}: {d.code} -> {d.material_uid} "
                      f"({d.valid_from}, {d.currency} {d.unit_cost})")
            elif d.warnings:
                print(f"  row {d.row_no}: {d.code} - {'; '.join(d.warnings)}")
    else:
        print("dry-run (nothing written). Add --apply to commit.")
        for d in drafts[:20]:
            if not d.errors:
                print(f"  [{d.row_no}] {d.supplier} / {d.code} = {d.unit_cost} "
                      f"{d.currency} @ {d.valid_from} ({d.cost_basis})")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"report -> {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
