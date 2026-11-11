#!/usr/bin/env python3
"""Validate mapping/joineryflow.yaml v2 against the committed snapshot.

    python scripts/check_joineryflow_mapping.py
    python scripts/check_joineryflow_mapping.py --json report.json

Checks the executable half of the ETL mapping: the YAML parses, version is 2,
the snapshot header and workspace match, all nine tables are present in contract
order, referenced columns exist (or are declared as known_columns for the three
tables with no rows), category/unit/identifier codes exist in the schema, and
identifier values do not collide. It also asserts the decisions recorded in
docs/etl-joineryflow-progress.md, recomputes the expected import counts from
the fixture, and checks that the write-back projection in schema/writeback.sql
(the catalog endpoints and the field allowlist inverse) agrees with the
write_back section of the mapping, so the SQL side cannot drift from the YAML.
Exits 1 and lists every problem; 0 means the mapping resolves.

Step 4 wires this into CI. Nothing here connects to a database.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MAPPING_PATH = ROOT / "mapping" / "joineryflow.yaml"
SEED_PATH = ROOT / "schema" / "seed.sql"
SCHEMA_PATH = ROOT / "schema" / "material.sql"
WRITEBACK_PATH = ROOT / "schema" / "writeback.sql"
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
REQUIRED_TABLE_KEYS = (
    "category_code", "pk", "unit", "own_cost_column", "price_policy",
    "family", "measures", "attributes", "identifiers",
)


def seed_codes(text: str, table: str) -> set[str]:
    match = re.search(
        rf"INSERT INTO {table} .*?VALUES\s*(.*?)ON CONFLICT", text, re.S
    )
    if not match:
        return set()
    return set(re.findall(r"\('([A-Za-z0-9.]+)'", match.group(1)))


def identifier_kinds(text: str) -> set[str]:
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS material_identifier.*?"
        r"CHECK \(kind IN\s*\((.*?)\)\)", text, re.S
    )
    if not match:
        return set()
    return set(re.findall(r"'([a-z_]+)'", match.group(1)))


def read_snapshot(path: Path):
    lines = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln]
    header = lines[0]
    rows = [o for o in lines[1:] if o.get("kind") == "row"]
    cv = [o for o in lines[1:] if o.get("kind") == "cv_mapping"]
    columns: dict[str, set[str]] = defaultdict(set)
    counts: dict[str, int] = defaultdict(int)
    for obj in rows:
        columns[obj["table"]].update(obj["row"].keys())
        counts[obj["table"]] += 1
    return header, rows, cv, columns, counts


def columns_for_table(table: str, spec: dict, actual: dict[str, set[str]],
                      problems: list[str]) -> tuple[set[str], str]:
    if actual.get(table):
        return set(actual[table]), "verified"
    known = spec.get("known_columns")
    if not known:
        problems.append(
            f"{table}: no rows in the snapshot and no known_columns declared; "
            "column references cannot be checked"
        )
        return set(), "unverified"
    return set(known), "known_columns (unverified)"


def referenced_columns(spec: dict) -> set[str]:
    refs: set[str] = set()
    for value in (spec.get("pk"), spec.get("own_cost_column")):
        if isinstance(value, str):
            refs.add(value)
    family = spec.get("family") or {}
    refs.update(str(c) for c in (family.get("key") or []))
    refs.update(PLACEHOLDER.findall(family.get("name") or ""))
    for measure in (spec.get("measures") or {}).values():
        if isinstance(measure, dict) and measure.get("column"):
            refs.add(str(measure["column"]))
    refs.update(str(c) for c in (spec.get("attributes") or []))
    for ident in spec.get("identifiers") or []:
        if isinstance(ident, dict) and ident.get("column"):
            refs.add(str(ident["column"]))
    return refs


def family_key(table: str, spec: dict, row: dict, size_re) -> str | None:
    family = spec.get("family")
    if not family:
        return None
    parts = [row.get(c) for c in (family.get("key") or [])]
    usable = [str(p).strip() for p in parts if p is not None and str(p).strip()]
    if usable:
        key = " ".join(usable).lower()
    else:
        name = str(row.get("description") or "")
        key = re.sub(r"\s+", " ", size_re.sub("", name)).strip().lower()
    if not key:
        return None
    return f"joineryflow:{spec['category_code']}:{key}"


CATALOG_ROW = re.compile(
    r"^\s*\('([a-z_]+)',\s*'([a-z_]+)',\s*'(/[a-z/-]+)',\s*'([a-z_]+)',\s*ARRAY\[(.*?)\]\),?$"
)
FIELD_ROW = re.compile(r"^\s*\('([a-z_.]+)',\s*'([a-z_]+)',\s*'([a-z_]+)'\),?$")


def writeback_rows() -> tuple[dict[str, dict], dict[str, dict], list[str]]:
    """Parse the mapping-projection INSERTs in schema/writeback.sql (no database)."""
    errors: list[str] = []
    catalog: dict[str, dict] = {}
    fields: dict[str, dict] = {}
    try:
        text = WRITEBACK_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        return catalog, fields, [f"cannot read schema/writeback.sql: {exc}"]
    section = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("INSERT INTO material_writeback_catalog"):
            section = "catalog"
            continue
        if stripped.startswith("INSERT INTO material_writeback_field"):
            section = "fields"
            continue
        if stripped.startswith("ON CONFLICT"):
            section = None
            continue
        if section is None or not stripped.startswith("('"):
            continue
        if section == "catalog":
            m = CATALOG_ROW.match(line)
            if not m:
                errors.append(f"schema/writeback.sql: unparsed catalog row: {stripped[:70]}")
                continue
            catalog[m.group(1)] = {
                "category_code": m.group(2),
                "endpoint": m.group(3),
                "pk_column": m.group(4),
                "attribute_keys": [k.strip().strip("'") for k in m.group(5).split(",") if k.strip()],
            }
        else:
            m = FIELD_ROW.match(line)
            if not m:
                errors.append(f"schema/writeback.sql: unparsed field row: {stripped[:70]}")
                continue
            fields[m.group(1)] = {
                "material_column": m.group(2),
                "upstream_column": m.group(3),
            }
    return catalog, fields, errors


def writeback_agreement(mapping: dict) -> tuple[list[str], dict]:
    """schema/writeback.sql must project the mapping's write_back section exactly."""
    problems: list[str] = []
    report: dict = {}
    tables = mapping.get("tables") or {}
    catalog, fields, errors = writeback_rows()
    problems.extend(errors)
    report["writeback_tables"] = len(catalog)
    report["writeback_fields"] = len(fields)

    if sorted(catalog) != sorted(tables):
        problems.append(
            "writeback.sql catalog tables do not match the mapping tables: "
            f"{sorted(catalog)} vs {sorted(tables)}"
        )
    for table, row in catalog.items():
        spec = tables.get(table) or {}
        if spec and row["category_code"] != str(spec.get("category_code")):
            problems.append(f"writeback.sql {table}: category_code disagrees with the mapping")
        if spec and row["endpoint"] != str(spec.get("endpoint")):
            problems.append(f"writeback.sql {table}: endpoint disagrees with the mapping")
        if spec and row["pk_column"] != str(spec.get("pk")):
            problems.append(f"writeback.sql {table}: pk_column disagrees with the mapping")
        if spec and sorted(row["attribute_keys"]) != sorted(str(a) for a in (spec.get("attributes") or [])):
            problems.append(f"writeback.sql {table}: attribute_keys disagree with the mapping attributes list")

    want: dict[str, dict] = {}
    yaml_fields = (mapping.get("write_back") or {}).get("fields") or {}
    for upstream, umr in yaml_fields.items():
        want[str(umr)] = {"material_column": str(umr), "upstream_column": str(upstream)}
    missing = sorted(set(want) - set(fields))
    extra = sorted(set(fields) - set(want))
    if missing:
        problems.append(f"writeback.sql field rows missing: {', '.join(missing)}")
    if extra:
        problems.append(f"writeback.sql field rows not in write_back.fields: {', '.join(extra)}")
    for umr in sorted(set(want) & set(fields)):
        if fields[umr] != want[umr]:
            problems.append(f"writeback.sql field row {umr}: disagrees with write_back.fields")
    return problems, report


def validate() -> tuple[list[str], dict, list[str]]:
    problems: list[str] = []
    notes: list[str] = []
    try:
        mapping = yaml.safe_load(MAPPING_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        return [f"cannot read mapping: {exc}"], {}, []

    if not isinstance(mapping, dict):
        return ["top level must be a mapping object"], {}, []
    if mapping.get("version") != 2:
        problems.append(f"version must be 2, got {mapping.get('version')!r}")

    source = mapping.get("source") or {}
    snapshot_path = ROOT / str(source.get("snapshot") or "")
    if not snapshot_path.is_file():
        return problems + [f"source.snapshot not found: {snapshot_path}"], {}, []
    header, rows, cv, actual_columns, counts = read_snapshot(snapshot_path)
    header_tables = list(header.get("tables") or [])
    if source.get("system") != header.get("source_system"):
        problems.append("source.system does not match the snapshot header")
    if source.get("workspace") != header.get("workspace_slug"):
        problems.append("source.workspace does not match the snapshot header")

    tables = mapping.get("tables") or {}
    if list(tables.keys()) != header_tables:
        problems.append(
            f"tables must match the snapshot header order: {header_tables}; got {list(tables.keys())}"
        )

    seed = SEED_PATH.read_text(encoding="utf-8")
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    categories = seed_codes(seed, "material_category")
    units = seed_codes(seed, "unit")
    kinds = identifier_kinds(schema)
    if not categories or not units or not kinds:
        problems.append("could not parse category/unit/identifier codes from schema/*.sql")

    common = mapping.get("common_rules") or {}
    size_re = re.compile(
        str((mapping.get("measures") or {}).get("derived_thickness", {}).get("regex") or "(?!)" ),
        re.I,
    )

    identifiers: dict[tuple[str, str], set[str]] = defaultdict(set)
    family_members: dict[str, int] = defaultdict(int)
    derived = 0
    prices = 0
    archived = 0
    category_codes: set[str] = set()
    per_table: list[str] = []

    for table in header_tables:
        spec = tables.get(table) or {}
        for key in REQUIRED_TABLE_KEYS:
            if key not in spec:
                problems.append(f"{table}: missing required key {key!r}")
        if not spec:
            continue
        if spec.get("category_code") not in categories:
            problems.append(f"{table}: unknown category_code {spec.get('category_code')!r}")
        if spec.get("unit") not in units:
            problems.append(f"{table}: unknown unit {spec.get('unit')!r}")

        cols, status = columns_for_table(table, spec, actual_columns, problems)
        declared_rows = spec.get("snapshot_rows")
        if declared_rows != counts.get(table, 0):
            problems.append(
                f"{table}: snapshot_rows={declared_rows} but the fixture has {counts.get(table, 0)} rows"
            )
        missing = sorted(referenced_columns(spec) - cols)
        if missing:
            problems.append(f"{table}: columns not found in snapshot: {', '.join(missing)}")
        per_table.append(
            f"    {table:<22} rows={counts.get(table, 0):>3}  cols={len(cols):>3}  {status}"
        )

        for ident in spec.get("identifiers") or []:
            if not isinstance(ident, dict):
                problems.append(f"{table}: identifier must be a mapping")
                continue
            if ident.get("kind") not in kinds:
                problems.append(f"{table}: identifier kind {ident.get('kind')!r} not in schema CHECK")

        if spec.get("price_policy") not in ("unit_cost", "attributes_only"):
            problems.append(f"{table}: unknown price_policy {spec.get('price_policy')!r}")

        for obj in [o for o in rows if o["table"] == table]:
            row = obj["row"]
            category_codes.add(str(spec.get("category_code")))
            if obj.get("archived"):
                archived += 1
            for ident in spec.get("identifiers") or []:
                if not isinstance(ident, dict):
                    continue
                value = row.get(ident.get("column"))
                if value not in (None, ""):
                    identifiers[(str(ident.get("kind")), str(value))].add(
                        f"{table}:{row.get(spec.get('pk'))}"
                    )
            fkey = family_key(table, spec, row, size_re)
            if fkey:
                family_members[fkey] += 1
            own_cost = spec.get("own_cost_column")
            cost = row.get("unit_cost") or (row.get(own_cost) if own_cost else None)
            if spec.get("price_policy") == "unit_cost" and cost not in (None, ""):
                prices += 1
            if table in ("board_materials",) and row.get("thickness_mm") in (None, "") \
                    and size_re.search(str(row.get("description") or "")):
                derived += 1

    for (kind, value), owners in sorted(identifiers.items()):
        if len(owners) > 1:
            problems.append(
                f"identifier collision: {kind}={value!r} on {', '.join(sorted(owners))}"
            )
    for obj in cv:
        row = obj["row"]
        code = row.get("cv_code")
        if code:
            identifiers[("cv_code", str(code))].add(
                f"{row.get('target_material_table')}:{row.get('target_material_id')}"
            )

    supplier_chain = [str(c) for c in ((common.get("default_supplier_id") or {}).get("columns") or [])]
    if "vendor" not in supplier_chain:
        problems.append("common_rules.default_supplier_id.columns must include vendor (custom_made)")
    if any("vendors.name" in str(c) for c in supplier_chain):
        problems.append("supplier chain must not contain the unresolvable vendors.name branch")
    hire = tables.get("equipment_hire") or {}
    if hire.get("price_policy") != "attributes_only" or hire.get("own_cost_column") is not None:
        problems.append("D2: equipment_hire must use price_policy attributes_only and no own cost column")
    if (mapping.get("prices") or {}).get("expected_rows") != 13:
        problems.append("D2: prices.expected_rows must be 13, not 14")
    if not (mapping.get("identity") or {}).get("cv_identifiers", {}).get("enabled"):
        problems.append("D3: cv_identifiers.enabled must be true")
    if (mapping.get("archived") or {}).get("acceptance") != "synthetic_rows":
        problems.append("D5: archived.acceptance must be synthetic_rows")
    if "category_display_name" not in ((common.get("product_type") or {}).get("fallback") or []):
        problems.append("product_type fallback must include category_display_name")
    if (common.get("status") or {}).get("is_active_tables") and \
            len(common["status"]["is_active_tables"]) != 5:
        problems.append("E8: is_active applies to exactly five tables")

    wb_problems, wb_report = writeback_agreement(mapping)
    problems.extend(wb_problems)

    report = {
        "tables": {t: counts.get(t, 0) for t in header_tables},
        "rows": len(rows),
        "identifier_values": len(identifiers),
        "identifier_collisions": sum(1 for owners in identifiers.values() if len(owners) > 1),
        "cv_identifiers": len(cv),
        "archived": archived,
        "categories_with_rows": len(category_codes),
        "derived_thickness": derived,
        "prices": prices,
        "families": len(family_members),
        "single_member_families": sum(1 for n in family_members.values() if n == 1),
        "writeback_tables": wb_report.get("writeback_tables", 0),
        "writeback_fields": wb_report.get("writeback_fields", 0),
    }
    expected = (mapping.get("import_contract") or {}).get("expected") or {}
    measured = {
        "materials": report["rows"],
        "archived": report["archived"],
        "categories_with_rows": report["categories_with_rows"],
        "derived_thickness_measures": report["derived_thickness"],
        "prices": report["prices"],
        "cv_identifiers": report["cv_identifiers"],
        "families": report["families"],
        "single_member_families": report["single_member_families"],
    }
    for key, want in expected.items():
        if key in measured and measured[key] != want:
            problems.append(f"import_contract.expected.{key}: expected {want}, fixture gives {measured[key]}")
    notes = per_table
    return problems, report, notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", dest="json_out", help="write the measured report as JSON")
    args = ap.parse_args(argv)

    problems, report, notes = validate()
    print(f"{'FAIL' if problems else 'OK'}  mapping/joineryflow.yaml v2")
    for line in notes:
        print(line)
    if report:
        print(
            "    measured: "
            f"{report['rows']} rows, {report['archived']} archived, "
            f"{report['categories_with_rows']} categories, "
            f"{report['derived_thickness']} derived measures, "
            f"{report['prices']} prices, {report['cv_identifiers']} cv codes, "
            f"{report['families']} families, "
            f"{report['identifier_values']} identifier values, "
            f"{report['identifier_collisions']} collisions, "
            f"writeback {report['writeback_tables']} tables / {report['writeback_fields']} fields"
        )
    for problem in problems:
        print(f"  - {problem}")
    if args.json_out and report:
        Path(args.json_out).write_text(
            json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
