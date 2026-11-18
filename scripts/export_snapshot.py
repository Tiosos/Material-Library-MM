#!/usr/bin/env python3
"""Export a real database snapshot for the self-contained preview page.

Companion to scripts/build_preview.py: this writes preview_snapshot.json from a
live database via the existing read API plus a few direct queries. Together the
two make refreshing the published static page reproducible:

    python scripts/export_snapshot.py --dsn postgresql://.../ml -o preview_snapshot.json
    python scripts/build_preview.py

The snapshot is always a frozen, timestamped export - the preview page states
this in its banner. No price or supply date is invented.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# cost_basis is a closed vocabulary in the UMR document, not a DB table.
COST_BASES = ["ex_gst", "inc_gst"]
RESULTS_LIMIT = 12  # the preview page exports one screenful of rows


def build(dsn: str) -> dict:
    import psycopg
    from fastapi.testclient import TestClient
    from api.main import app

    os.environ["ML_DSN"] = dsn
    with TestClient(app) as client:
        meta = client.get("/v1/meta").json()
        listing = client.get("/v1/materials", params={"limit": RESULTS_LIMIT}).json()
        facets = client.get("/v1/facets").json()["facets"]
        wb = client.get("/v1/writeback/schema").json()

    with psycopg.connect(dsn) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("SELECT category_code FROM material_writeback_catalog "
                        "WHERE category_code IS NOT NULL ORDER BY 1")
            categories = [r["category_code"] for r in cur.fetchall()]
            cur.execute("SELECT id FROM material_create_request ORDER BY id DESC")
            create_ids = [r["id"] for r in cur.fetchall()]
            cur.execute("SELECT id FROM material_change_request ORDER BY id DESC")
            change_ids = [r["id"] for r in cur.fetchall()]
            cur.execute("SELECT * FROM material_create_request ORDER BY id DESC")
            create_rows = [json.loads(json.dumps(r, default=str)) for r in cur.fetchall()]
            cur.execute("SELECT * FROM material_change_request ORDER BY id DESC")
            cr_rows = [json.loads(json.dumps(r, default=str)) for r in cur.fetchall()]

    units = [{"code": u["code"], "name": u["name"], "dimension": u["dimension"]}
             for u in wb["units"]]
    catalogs = [{"category_code": c["category_code"], "attributes": c["attributes"]}
                for c in wb["catalogs"]]

    return {
        "exported_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "meta": meta,
        "results": listing["results"],
        "search_total": listing["total"],
        "facets": facets,
        "create_rows": create_rows,
        "cr_rows": cr_rows,
        "schema": {
            "categories": sorted(set(categories)),
            "units": units,
            "cost_bases": COST_BASES,
            "catalogs": catalogs,
        },
        "seeded_ids": {"creates": create_ids, "edits": change_ids},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Export a preview snapshot from a live DB")
    ap.add_argument("--dsn", required=True, help="DSN of the database to snapshot")
    ap.add_argument("-o", "--out", default="preview_snapshot.json")
    args = ap.parse_args(argv)

    snap = build(args.dsn)
    out = Path(args.out)
    out.write_text(json.dumps(snap, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}: {len(snap['results'])} results of {snap['search_total']}, "
          f"{snap['meta']['materials']} materials, "
          f"{len(snap['create_rows'])} creates, {len(snap['cr_rows'])} edits, "
          f"exported {snap['exported_at']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
