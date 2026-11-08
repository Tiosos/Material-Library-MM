#!/usr/bin/env python3
"""CLI: export the joineryflow catalogue into a committed JSONL snapshot.

    python scripts/export_joineryflow.py \
      --dsn postgresql://jf:jf@127.0.0.1:5432/joineryflow \
      --workspace hartwood-joinery \
      --out fixtures/joineryflow-hartwood.jsonl

Design: docs/etl-joineryflow.md sections 5 and 6. The source database is opened
with BEGIN READ ONLY and is never written. The importer reads only this file, so
CI never needs a joineryflow database. Re-exporting unchanged source rows is
byte-identical apart from the header's exported_at.

Defaults: --dsn from JOINERYFLOW_DSN, --workspace from JOINERYFLOW_WORKSPACE,
--out fixtures/joineryflow-hartwood.jsonl. --out outside fixtures/ needs
--force, so a stray run cannot dump the catalogue into a tracked source file.
"""
from __future__ import annotations

import argparse
import datetime as dt
import decimal
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA_VERSION = "1"
SOURCE_SYSTEM = "joineryflow"
FIXTURES_DIR = ROOT / "fixtures"
DEFAULT_OUT = FIXTURES_DIR / "joineryflow-hartwood.jsonl"

# Table order is part of the snapshot contract: rows are emitted in this order,
# then by primary key ascending. equipment_hire is the one table whose primary
# key is hire_id rather than material_id (docs/etl-joineryflow.md section 7).
TABLES: tuple[tuple[str, str], ...] = (
    ("board_materials", "material_id"),
    ("edging_materials", "material_id"),
    ("glass_materials", "material_id"),
    ("timber_materials", "material_id"),
    ("benchtop_materials", "material_id"),
    ("hardware_materials", "material_id"),
    ("appliances", "material_id"),
    ("custom_made", "material_id"),
    ("equipment_hire", "hire_id"),
)
CV_TABLE = "cv_material_mapping"
CV_PK = "cv_material_mapping_id"

# image_blob is a bigint pointer into joineryflow's attachment store, not data.
EXCLUDED_COLUMNS = frozenset({"image_blob"})


class UsageError(Exception):
    """A bad invocation: unknown workspace, missing dsn, no rows, bad --out."""


class ShapeError(Exception):
    """The source does not fit the snapshot contract; names table and column."""


def shape_value(value, *, table: str, column: str):
    """Return the JSON representation of one database value (pure function).

    Numerics become strings so "45.00" never becomes 45.0. Timestamps become
    ISO-8601 UTC with Z, dates stay YYYY-MM-DD, text[] becomes a JSON array.
    Anything else is a shape error rather than a silent coercion.
    """
    if value is None:
        return None
    if isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [shape_value(v, table=table, column=column) for v in value]
    raise ShapeError(f"{table}.{column}: unsupported type {type(value).__name__}")


def shape_row(columns: list[str], values, *, table: str) -> dict:
    """Build one snapshot row from parallel column and value sequences."""
    return {
        column: shape_value(value, table=table, column=column)
        for column, value in zip(columns, values)
        if column not in EXCLUDED_COLUMNS
    }


def encode_line(obj) -> str:
    """One JSONL line: sorted keys, UTF-8 friendly, no trailing whitespace."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def build_header(*, workspace_slug: str, workspace_name: str,
                 include_archived: bool, exported_at: str) -> dict:
    """The first line of the snapshot (pure function)."""
    return {
        "kind": "header",
        "schema_version": SCHEMA_VERSION,
        "exported_at": exported_at,
        "source_system": SOURCE_SYSTEM,
        "workspace_slug": workspace_slug,
        "workspace_name": workspace_name,
        "include_archived": include_archived,
        "tables": [name for name, _ in TABLES],
    }


def resolve_workspace(conn, workspace: str | None) -> tuple[int, str, str]:
    """Resolve a slug or numeric id to (id, slug, name)."""
    if not workspace:
        raise UsageError("no workspace: pass --workspace or set JOINERYFLOW_WORKSPACE")
    if workspace.isdigit():
        rows = conn.execute(
            "SELECT id, slug, name FROM workspace WHERE id = %s", (int(workspace),)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, slug, name FROM workspace WHERE slug = %s", (workspace,)
        ).fetchall()
    if not rows:
        raise UsageError(f"unknown workspace {workspace!r}")
    return rows[0]


def table_columns(conn, table: str) -> list[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s "
        "ORDER BY ordinal_position", (table,)
    ).fetchall()
    return [r[0] for r in rows]


def export_tables(conn, workspace_id: int, include_archived: bool):
    """Return (lines, counts, archived_skipped) for the nine catalogue tables."""
    lines: list[str] = []
    counts: dict[str, int] = {}
    archived_skipped = 0

    for table, pk in TABLES:
        cols = table_columns(conn, table)
        if pk not in cols:
            raise ShapeError(f"{table}.{pk}: primary key column not found")
        if "workspace_id" not in cols:
            raise ShapeError(f"{table}.workspace_id: workspace column not found")

        where = "workspace_id = %s"
        has_archived = "archived_at" in cols
        if has_archived and not include_archived:
            where += " AND archived_at IS NULL"

        cur = conn.execute(
            f"SELECT * FROM {table} WHERE {where} ORDER BY {pk}", (workspace_id,)
        )
        columns = [d.name for d in cur.description]
        rows = cur.fetchall()
        counts[table] = len(rows)

        seen: set = set()
        for values in rows:
            record = dict(zip(columns, values))
            source_id = record[pk]
            if source_id is None or source_id in seen:
                raise ShapeError(
                    f"{table}.{pk}: null or duplicate primary key {source_id!r}"
                )
            seen.add(source_id)
            lines.append(encode_line({
                "kind": "row",
                "table": table,
                "source_id": source_id,
                "archived": bool(record.get("archived_at") is not None),
                "row": shape_row(columns, values, table=table),
            }))

        if has_archived and not include_archived:
            archived_skipped += conn.execute(
                f"SELECT count(*) FROM {table} "
                "WHERE workspace_id = %s AND archived_at IS NOT NULL",
                (workspace_id,),
            ).fetchone()[0]

    return lines, counts, archived_skipped


def export_cv(conn, workspace_id: int) -> list[str]:
    """Return the cv_material_mapping lines (docs/etl-joineryflow.md E11)."""
    cur = conn.execute(
        f"SELECT * FROM {CV_TABLE} WHERE workspace_id = %s ORDER BY {CV_PK}",
        (workspace_id,),
    )
    columns = [d.name for d in cur.description]
    lines = []
    for values in cur.fetchall():
        record = dict(zip(columns, values))
        lines.append(encode_line({
            "kind": "cv_mapping",
            "table": CV_TABLE,
            "source_id": record[CV_PK],
            "row": shape_row(columns, values, table=CV_TABLE),
        }))
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsn", default=os.environ.get("JOINERYFLOW_DSN"),
                    help="joineryflow DSN (default $JOINERYFLOW_DSN)")
    ap.add_argument("--workspace", default=os.environ.get("JOINERYFLOW_WORKSPACE"),
                    help="workspace slug or numeric id (default $JOINERYFLOW_WORKSPACE)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="output JSONL path")
    ap.add_argument("--include-archived", action="store_true",
                    help="include archived rows and flip the header flag")
    ap.add_argument("--quiet", action="store_true", help="print nothing on success")
    ap.add_argument("--json", dest="json_out", help="write a JSON report here")
    ap.add_argument("--force", action="store_true",
                    help="allow --out outside fixtures/")
    args = ap.parse_args(argv)

    out = Path(args.out)
    if not args.force:
        try:
            out.resolve().relative_to(FIXTURES_DIR.resolve())
        except ValueError:
            print(f"error: --out must be inside {FIXTURES_DIR} "
                  "(pass --force to override)", file=sys.stderr)
            return 2
    if not args.dsn:
        print("error: pass --dsn or set JOINERYFLOW_DSN", file=sys.stderr)
        return 2

    try:
        import psycopg
    except ImportError:
        print("error: psycopg is required (pip install 'psycopg[binary]')",
              file=sys.stderr)
        return 1

    try:
        with psycopg.connect(args.dsn, autocommit=True) as conn:
            conn.execute("BEGIN READ ONLY")
            try:
                workspace_id, slug, name = resolve_workspace(conn, args.workspace)
                lines, counts, skipped = export_tables(
                    conn, workspace_id, args.include_archived)
                cv_lines = export_cv(conn, workspace_id)
            finally:
                conn.execute("ROLLBACK")
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ShapeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    total = sum(counts.values())
    if total == 0:
        print("error: no catalogue rows found for this workspace", file=sys.stderr)
        return 2

    exported_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) \
        .isoformat().replace("+00:00", "Z")
    header = build_header(workspace_slug=slug, workspace_name=name,
                          include_archived=args.include_archived,
                          exported_at=exported_at)
    text = "\n".join([encode_line(header)] + lines + cv_lines) + "\n"
    data = text.encode("utf-8")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)  # bytes, so line endings stay LF on every platform

    report = {
        "tables": counts,
        "archived_skipped": skipped,
        "cv_rows": len(cv_lines),
        "bytes": len(data),
    }
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    if not args.quiet:
        print(f"wrote {out}: {total} rows + {len(cv_lines)} cv lines, "
              f"{len(data)} bytes")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
