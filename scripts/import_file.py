#!/usr/bin/env python3
"""CLI: import supplier spreadsheets into Material Library v2.

    # check the mapping first, without connecting to the DB
    python scripts/import_file.py --file ingest/samples/blum_hinges.csv --mapping mappings/blum.yaml

    # NEW: no YAML needed - infer the mapping from the headers, then review the dry run
    python scripts/import_file.py --file new_supplier.csv --auto
    python scripts/import_file.py --file new_supplier.csv --auto --save-mapping mappings/new.yaml

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

import yaml

from ingest.auto_mapping import detect_header_row, infer_mapping
from ingest.importer import (
    apply as apply_drafts, build_drafts, list_sheets, matrix_to_table,
    plan_report, read_matrix, read_table,
)
from ingest.mapping import Mapping, load_mapping
from ingest.quality import assess, load_quality_context, print_quality


def _auto_options(args) -> dict:
    return {
        "vendor": args.vendor or Path(args.file).stem.lower(),
        "source_system": args.source_system,
        "category_code": args.category,
        "unit": args.unit,
        "currency": args.currency,
        "brand": args.brand,
        "supplier": args.supplier,
        "code_column": args.code_column,
    }


def _print_inference(report: dict) -> None:
    print(f"[auto] vendor={report['vendor']} identifier={report['identifier']!r}")
    print(f"[auto] name template: {report['name_template']}")
    for row in report["resolved"]:
        print(f"  {row['header']!r:26s} -> {row['role']:14s} ({row['target']}, {row['field']})")
    for row in report["unresolved"]:
        print(f"  {row['header']!r:26s} -> skipped ({row['reason']})")
    for warning in report["warnings"]:
        print(f"  WARNING: {warning}")
    if report["warnings"]:
        print("[auto] review the guesses above; fix with --code-column/--category/--brand or edit the saved YAML")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="CSV / XLSX")
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--mapping", help="mappings/<vendor>.yaml (explicit)")
    source.add_argument("--auto", action="store_true", help="infer the mapping from the headers")
    ap.add_argument("--dsn", default=os.environ.get("ML_DSN"))
    ap.add_argument("--apply", action="store_true", help="write to the database (dry-run by default)")
    ap.add_argument("--strict", action="store_true",
                    help="refuse --apply when the quality report shows collisions, duplicates or incomplete rows")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", dest="json_out", help="write the report as JSON")
    # auto-mode overrides (ignored with --mapping)
    ap.add_argument("--vendor", help="auto: vendor id (default: file stem)")
    ap.add_argument("--source-system", help="auto: source_system (default: vendor)")
    ap.add_argument("--category", help="auto: default category_code (default: generic)")
    ap.add_argument("--unit", help="auto: default UCUM unit")
    ap.add_argument("--currency", default="AUD", help="auto: default currency (default: AUD)")
    ap.add_argument("--brand", help="auto: default brand")
    ap.add_argument("--supplier", help="auto: default supplier")
    ap.add_argument("--code-column", help="auto: force this header as the identifier")
    ap.add_argument("--save-mapping", help="auto: save the inferred mapping as YAML for later reuse")
    ap.add_argument("--header-row", type=int, default=1,
                    help="1-based physical row holding the headers (0 = auto-detect, with --auto)")
    ap.add_argument("--sheet", help="XLSX worksheet name (default: active sheet)")
    ap.add_argument("--list-sheets", action="store_true", help="print XLSX sheet names and exit")
    args = ap.parse_args(argv)

    if args.list_sheets:
        names = list_sheets(args.file)
        print(f"sheets in {Path(args.file).name}: {names or 'none (not an XLSX)'}")
        return 0
    if args.header_row == 0 and not args.auto:
        raise SystemExit("--header-row 0 (auto-detect) requires --auto")
    if args.header_row < 0:
        raise SystemExit("--header-row must be >= 1 (or 0 for auto-detect)")

    matrix = read_matrix(args.file, args.sheet)
    header_row = args.header_row
    if header_row == 0:
        header_row = detect_header_row(matrix)
        print(f"[auto] detected header row {header_row} of {len(matrix)}")
    headers, rows = matrix_to_table(matrix, header_row)
    if args.limit:
        rows = rows[: args.limit]

    if args.auto:
        mapping_dict, inference = infer_mapping(headers, rows, **_auto_options(args))
        _print_inference(inference)
        mapping = Mapping(mapping_dict)
        if args.save_mapping:
            target = Path(args.save_mapping)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "# Auto-generated mapping - review and commit if this supplier is permanent.\n"
                + yaml.safe_dump(mapping_dict, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            print(f"[auto] mapping saved -> {target}")
    else:
        mapping = load_mapping(args.mapping)

    drafts, meta = build_drafts(headers, rows, mapping)
    report = plan_report(drafts, meta)
    report["auto"] = bool(args.auto)
    if args.auto:
        report["inference"] = inference
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

    # --- data-quality gate (SELECT only; never writes) -----------------------
    quality_ctx = {}
    if args.dsn:
        import psycopg
        with psycopg.connect(args.dsn) as conn:
            quality_ctx = load_quality_context(conn, drafts)
    quality = assess(
        drafts,
        required_by_category=quality_ctx.get("required_by_category") if args.dsn else None,
        existing_identifiers=quality_ctx.get("existing_identifiers"),
        open_duplicate_candidates=quality_ctx.get("open_duplicate_candidates"),
    )
    report["quality"] = quality
    print_quality(quality)
    if not args.dsn:
        print("  (batch-only checks: pass --dsn to include DB collisions and profile completeness)")

    if args.apply:
        if not args.dsn:
            raise SystemExit("--apply requires --dsn (or the ML_DSN environment variable)")
        if args.strict and quality["has_signals"]:
            raise SystemExit("--strict: quality signals found; fix the mapping/data or drop --strict to apply")
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
