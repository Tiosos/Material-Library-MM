#!/usr/bin/env python3
"""End-to-end acceptance: real data in a clean DB, then exercise the read paths.

This closes the loop the import chain left open: the fast importer, quality gate,
cover-row handling, price split and Antaro extractor are all verified on their own,
but nobody checked that data actually comes back out through search, facets, the
material list/detail, the completeness view and export once it is loaded.

The script builds a fresh database in CI schema order, imports the real sample
catalogues plus the 83 Antaro parts (catalogue only, --no-price), then asserts the
read paths against that live data. Every check runs even if an earlier one fails,
so one invocation reports all problems. Exit code 0 only when all checks pass.

Usage:
    python scripts/verify_e2e.py \
        --admin-dsn postgresql://postgres:postgres@127.0.0.1:5432/postgres \
        --db ml_e2e

    # or point at an already-prepared database and skip schema/import:
    python scripts/verify_e2e.py --dsn postgresql://postgres:postgres@127.0.0.1:5432/ml_e2e \
        --skip-prepare

Requires psycopg (api/requirements.txt) and the FastAPI stack for TestClient.
The Antaro CSV is committed at ingest/samples/antaro_products.csv.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCHEMA_ORDER = [
    "material.sql", "search.sql", "api.sql", "merge.sql",
    "seed.sql", "writeback.sql", "create-material.sql",
]
IMPORTS = [
    ("ingest/samples/blum_hinges.csv", "mappings/blum.yaml"),
    ("ingest/samples/laminex_boards.csv", "mappings/laminex.yaml"),
    ("ingest/samples/antaro_products.csv", "mappings/antaro.yaml", "--no-price"),
]


class Results:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, fn) -> None:
        try:
            detail = fn()
            self.rows.append((name, True, detail or "ok"))
        except Exception as exc:  # noqa: BLE001 - report every failure in one pass
            self.rows.append((name, False, f"{type(exc).__name__}: {exc}"))

    @property
    def ok(self) -> bool:
        return all(passed for _, passed, _ in self.rows)

    def report(self) -> str:
        lines = ["", "=== acceptance results ==="]
        for name, passed, detail in self.rows:
            lines.append(f"  [{'PASS' if passed else 'FAIL'}] {name}: {detail}")
        passed = sum(1 for _, p, _ in self.rows if p)
        lines.append(f"  {passed}/{len(self.rows)} checks passed")
        return "\n".join(lines)


def _connect(dsn: str):
    import psycopg
    return psycopg.connect(dsn, autocommit=True)


def prepare(dsn: str) -> None:
    """Create a fresh DB in CI order: schemas, fixture, real imports."""
    import psycopg
    with _connect(dsn) as conn:
        for name in SCHEMA_ORDER:
            conn.execute((ROOT / "schema" / name).read_text(encoding="utf-8"))
        conn.execute((ROOT / "eval" / "fixture.sql").read_text(encoding="utf-8"))

    for spec in IMPORTS:
        csv_path, mapping_path = spec[0], spec[1]
        cmd = [sys.executable, str(ROOT / "scripts/import_file.py"),
               "--file", str(ROOT / csv_path), "--mapping", str(ROOT / mapping_path),
               "--dsn", dsn, "--apply"]
        if len(spec) > 2:
            cmd.append(spec[2])
        subprocess.run(cmd, check=True, cwd=str(ROOT),
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    print(f"[prepare] schemas + fixture + {len(IMPORTS)} imports loaded into {dsn}")


def build_checks(dsn: str, client) -> Results:
    r = Results()

    # 1. Search: an Antaro part number with dots/mixed case must hit the exact tier.
    def search_antaro():
        body = client.get("/v1/materials", params={"q": "ZRG.207RSIC", "X-Actor": "e2e"}).json()
        assert body["total"] >= 1, body
        assert body.get("level") == "exact", f"expected exact tier, got {body.get('level')}"
        return f"total={body['total']} level={body.get('level')}"
    r.check("search: Antaro part 'ZRG.207RSIC' exact hit", search_antaro)

    def search_screw():
        body = client.get("/v1/materials", params={"q": "661.1000.HG"}).json()
        assert body["total"] >= 1, body
        assert body.get("level") == "exact", f"expected exact tier, got {body.get('level')}"
        return f"total={body['total']} level={body.get('level')}"
    r.check("search: screw '661.1000.HG' exact hit", search_screw)

    def search_hinge():
        body = client.get("/v1/materials", params={"q": "71B3590"}).json()
        assert body["total"] >= 1, body
        return f"total={body['total']} level={body.get('level')}"
    r.check("search: known hinge '71B3590' still hits", search_hinge)

    # 2. Facets: category facet must include hardware (from Antaro) and hardware.hinge.
    def facets():
        body = client.get("/v1/facets").json()
        blob = json.dumps(body)
        assert "hardware" in blob, "hardware category missing from facets"
        assert "hardware.hinge" in blob, "hardware.hinge missing from facets"
        return "facets include hardware + hardware.hinge"
    r.check("facets: hardware categories present", facets)

    # 3. Material list filter + detail round trip.
    def list_filter():
        body = client.get("/v1/materials", params={"category": "hardware", "limit": 100}).json()
        assert body["total"] >= 83, body["total"]
        return f"hardware rows={body['total']}"
    r.check("list: category=hardware returns Antaro rows", list_filter)

    def detail():
        listing = client.get("/v1/materials", params={"q": "ZRG.207RSIC"}).json()
        assert listing["results"], "no item to open"
        uid = listing["results"][0]["material_uid"]
        doc = client.get(f"/v1/materials/{uid}").json()
        assert doc.get("material_uid") == uid, doc
        return f"uid={uid} category={doc.get('category_code')}"
    r.check("detail: open one material by uid", detail)

    # 4. Quality view: Antaro's own hardware rows are complete and carry no prices.
    # The fixture also has hardware rows with missing required fields; those are test
    # data and not part of the catalogue import, so scope this check to the Antaro rows.
    def quality():
        with _connect(dsn) as conn:
            total, complete = conn.execute(
                "SELECT count(*), count(*) FILTER (WHERE present_count >= required_count) "
                "FROM v_material_completeness v JOIN material m USING(material_uid) "
                "WHERE m.source_system='blum_antaro_catalogue'"
            ).fetchone()
            antaro_prices = conn.execute(
                "SELECT count(*) FROM material_price p JOIN material m USING(material_uid) "
                "WHERE m.source_system='blum_antaro_catalogue'"
            ).fetchone()[0]
        assert total == 83, f"expected 83 Antaro rows, got {total}"
        assert complete == total, f"{total - complete} incomplete Antaro rows"
        assert antaro_prices == 0, f"Antaro wrote {antaro_prices} price rows"
        return f"Antaro {complete}/{total} complete, prices=0"
    r.check("quality: Antaro completeness + no prices", quality)

    # 5. Export: JSONL / CSV / JSON envelopes, counts consistent, JSON schema-valid.
    def export_jsonl():
        resp = client.get("/v1/export", params={"format": "jsonl"})
        lines = [l for l in resp.text.splitlines() if l.strip()]
        assert len(lines) == int(resp.headers["x-record-count"]), (len(lines), resp.headers)
        json.loads(lines[0])
        return f"lines={len(lines)} schema={resp.headers.get('x-schema-version')}"
    r.check("export: jsonl", export_jsonl)

    def export_csv():
        resp = client.get("/v1/export", params={"format": "csv"})
        rows = list(csv.reader(io.StringIO(resp.text)))
        assert len(rows) - 1 == int(resp.headers["x-record-count"]), (len(rows), resp.headers)
        return f"rows={len(rows) - 1}"
    r.check("export: csv", export_csv)

    def export_json():
        resp = client.get("/v1/export", params={"format": "json"})
        env = resp.json()
        assert set(["schema_version", "exported_at", "count", "records"]) <= set(env)
        assert env["count"] == len(env["records"])
        assert int(resp.headers["x-record-count"]) == env["count"]
        # schema validation when the dependency is present
        try:
            import jsonschema
            from referencing import Registry, Resource
            from referencing.jsonschema import DRAFT202012
            export_schema = json.loads((ROOT / "schema/export.schema.json").read_text(encoding="utf-8"))
            material_schema = json.loads((ROOT / "schema/material.schema.json").read_text(encoding="utf-8"))
            registry = Registry().with_resources([
                (material_schema["$id"], Resource.from_contents(material_schema, default_specification=DRAFT202012)),
                (export_schema["$id"], Resource.from_contents(export_schema, default_specification=DRAFT202012)),
            ])
            jsonschema.Draft202012Validator(export_schema, registry=registry).validate(env)
        except ImportError:
            pass
        return f"count={env['count']} schema_version={env['schema_version']}"
    r.check("export: json envelope", export_json)

    # 6. UI shell: the editable screen is served without a token.
    def ui_shell():
        resp = client.get("/ui")
        assert resp.status_code == 200, resp.status_code
        assert "/ui/assets/" in resp.text and ".js" in resp.text, resp.text[:200]
        return "GET /ui 200 with asset refs"
    r.check("ui: shell served", ui_shell)

    return r


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="End-to-end acceptance on real imported data")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--admin-dsn", help="admin DSN (postgres db); used with --db to create a fresh DB")
    src.add_argument("--dsn", help="DSN of an existing DB (use --skip-prepare)")
    ap.add_argument("--db", default="ml_e2e", help="database name to create from --admin-dsn")
    ap.add_argument("--skip-prepare", action="store_true",
                    help="skip schema/fixture/import; use the DB as-is")
    args = ap.parse_args(argv)

    if args.dsn:
        dsn = args.dsn
        if not args.skip_prepare:
            ap.error("--dsn requires --skip-prepare (it will not rebuild an existing DB)")
    else:
        import psycopg
        admin = args.admin_dsn
        dsn = admin.rsplit("/", 1)[0] + "/" + args.db
        with _connect(admin) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{args.db}"')
            conn.execute(f'CREATE DATABASE "{args.db}"')
        print(f"[prepare] fresh database {args.db}")

    if not args.skip_prepare:
        prepare(dsn)

    os.environ["ML_DSN"] = dsn
    from fastapi.testclient import TestClient
    from api.main import app
    with TestClient(app) as client:
        results = build_checks(dsn, client)
    print(results.report())
    return 0 if results.ok else 1


if __name__ == "__main__":
    sys.exit(main())
