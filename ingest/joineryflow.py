#!/usr/bin/env python3
"""Pure snapshot reader and draft builder for the joineryflow ETL (step 2).

Design: docs/etl-joineryflow.md sections 5-9. Step log and decisions:
docs/etl-joineryflow-progress.md. This module never opens a database:

* ``read_snapshot`` validates and parses the committed JSONL snapshot.
* ``load_rules`` reads mapping/joineryflow.yaml v2 plus the unit seed codes.
* ``build_drafts`` turns snapshot rows into the existing ``Draft`` objects.
* ``build_report`` / ``check_contract`` produce the dry-run numbers.

``python -m ingest.joineryflow --dry-run --json`` is the step 2 verification
harness: it reports the build numbers on the committed fixture and never opens
a database. The real import CLI (``scripts/import_joineryflow.py --dry-run
--json``, with --dsn, --limit and --include-archived) belongs to step 3 and is
deliberately not here.

Implementation constraints (the design stays final, D1):
* D2: equipment_hire.rate stays in attributes; no price row and no "day" unit.
  The expected price count is 13, not the 14 printed in design sections 10/13.
* D3: cv_material_mapping rows become cv_code identifiers with source_system=cv.
* D5: archived acceptance is exercised with synthetic rows in tests only.
* common_rules.attributes (unclaimed columns) is realised through the
  per-table ``attributes`` lists in mapping v2: those are the feature columns
  that keep their source names so write-back stays mechanical. Structural and
  bookkeeping columns (pk, workspace_id, timestamps, flags) remain available
  in source_payload only.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import yaml

from .importer import Draft, gen_uid

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MAPPING = ROOT / "mapping" / "joineryflow.yaml"
DEFAULT_SEED = ROOT / "schema" / "seed.sql"
DEFAULT_SNAPSHOT = ROOT / "fixtures" / "joineryflow-hartwood.jsonl"
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
SIZE_TOKEN_FALLBACK = re.compile(r"^\d+(?:\.\d+)?\s*mm\b", re.I)


class SnapshotError(ValueError):
    """The JSONL file does not satisfy the committed snapshot contract."""


class RulesError(ValueError):
    """mapping/joineryflow.yaml is not an executable version 2 mapping."""


@dataclass(frozen=True)
class SourceRow:
    line_no: int
    table: str
    source_id: Any
    archived: bool
    row: dict


@dataclass(frozen=True)
class Snapshot:
    header: dict
    rows: tuple[SourceRow, ...]
    cv_rows: tuple[dict, ...]
    path: Path


@dataclass(frozen=True)
class Rules:
    data: dict
    units: frozenset[str]
    size_re: re.Pattern[str]
    snapshot_path: Path

    @property
    def source_system(self) -> str:
        return str((self.data.get("source") or {}).get("system") or "joineryflow")

    @property
    def workspace(self) -> str:
        return str((self.data.get("source") or {}).get("workspace") or "")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def read_snapshot(path: str | Path = None, *, tables: Iterable[str] | None = None) -> Snapshot:
    """Parse a joineryflow JSONL snapshot.

    The first object must be the header. ``row`` lines need a table, a non-null
    source_id (the exported primary key) and a row object. Duplicate primary
    keys, unknown kinds, unknown tables, blank lines and image_blob are all
    exporter failures, so they raise instead of being skipped.
    """
    p = Path(path) if path is not None else DEFAULT_SNAPSHOT
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise SnapshotError(f"cannot read snapshot {p}: {exc}") from exc

    objects: list[tuple[int, dict]] = []
    lines = raw.split("\n")
    for i, line in enumerate(lines, start=1):
        if not line.strip():
            if i == len(lines):          # one trailing newline is fine
                continue
            raise SnapshotError(f"line {i}: blank lines are not part of the JSONL contract")
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SnapshotError(f"line {i}: invalid JSON ({exc.msg})") from exc
        if not isinstance(obj, dict):
            raise SnapshotError(f"line {i}: expected a JSON object")
        objects.append((i, obj))

    if not objects or objects[0][1].get("kind") != "header":
        raise SnapshotError("first line must be the header object")
    header = objects[0][1]
    for key in ("schema_version", "source_system", "workspace_slug", "tables"):
        if key not in header:
            raise SnapshotError(f"header is missing {key!r}")
    if str(header["schema_version"]) != "1":
        raise SnapshotError(f"unsupported schema_version {header['schema_version']!r}")
    header_tables = [str(t) for t in header["tables"]]
    allowed = set(str(t) for t in tables) if tables is not None else set(header_tables)

    rows: list[SourceRow] = []
    cv_rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for line_no, obj in objects[1:]:
        kind = obj.get("kind")
        if kind == "row":
            table = str(obj.get("table") or "")
            if table not in allowed:
                raise SnapshotError(f"line {line_no}: table {table!r} is not in the header")
            sid = obj.get("source_id")
            if sid is None:
                raise SnapshotError(f"line {line_no}: row has no source_id")
            key = (table, str(sid))
            if key in seen:
                raise SnapshotError(f"line {line_no}: duplicate primary key {key[0]}:{key[1]}")
            seen.add(key)
            row = obj.get("row")
            if not isinstance(row, dict):
                raise SnapshotError(f"line {line_no}: row value must be an object")
            if "image_blob" in row:
                raise SnapshotError(f"line {line_no}: image_blob must not be exported")
            archived = bool(obj.get("archived")) or bool(row.get("archived_at"))
            rows.append(SourceRow(line_no, table, sid, archived, row))
        elif kind == "cv_mapping":
            row = obj.get("row")
            if not isinstance(row, dict):
                raise SnapshotError(f"line {line_no}: cv_mapping row must be an object")
            cv_rows.append({"line_no": line_no, "table": obj.get("table"), "row": row})
        else:
            raise SnapshotError(f"line {line_no}: unknown kind {kind!r}")

    return Snapshot(header=header, rows=tuple(rows), cv_rows=tuple(cv_rows), path=p)


def seed_codes(text: str, table: str) -> set[str]:
    """Extract the codes from an ``INSERT INTO <table> ... ON CONFLICT`` block."""
    match = re.search(rf"INSERT INTO {table} .*?VALUES\s*(.*?)ON CONFLICT", text, re.S)
    if not match:
        return set()
    return set(re.findall(r"\('([A-Za-z0-9.]+)'", match.group(1)))


def rules_from_mapping(data: dict, *, units: Iterable[str] = ()) -> Rules:
    """Build rules from an already parsed mapping (used by tests and step 3)."""
    if not isinstance(data, dict) or data.get("version") != 2:
        raise RulesError("mapping/joineryflow.yaml must be version 2")
    derived = (data.get("measures") or {}).get("derived_thickness") or {}
    regex = str(derived.get("regex") or "(?!)")
    snapshot = ROOT / str((data.get("source") or {}).get("snapshot") or "")
    return Rules(
        data=data,
        units=frozenset(str(u) for u in units),
        size_re=re.compile(regex, re.I),
        snapshot_path=snapshot,
    )


def load_rules(mapping_path: str | Path = None, seed_path: str | Path = None,
               *, units: Iterable[str] | None = None) -> Rules:
    """Load mapping v2 and the unit codes from schema/seed.sql."""
    mp = Path(mapping_path) if mapping_path is not None else DEFAULT_MAPPING
    try:
        data = yaml.safe_load(mp.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise RulesError(f"cannot read mapping {mp}: {exc}") from exc
    if units is None:
        sp = Path(seed_path) if seed_path is not None else DEFAULT_SEED
        try:
            units = seed_codes(sp.read_text(encoding="utf-8"), "unit")
        except OSError:
            units = set()
    return rules_from_mapping(data, units=units)


# ---------------------------------------------------------------------------
# Small value helpers
# ---------------------------------------------------------------------------
def _first(row: dict, columns: Iterable[str]) -> Any:
    """First non-null, non-empty candidate in a mapping fallback chain."""
    for col in columns:
        value = row.get(col)
        if value is None or str(value).strip() == "":
            continue
        return value
    return None


def _number(value: Any) -> float | None:
    """Numerics arrive as strings in the snapshot; keep them exact until here."""
    if value is None or value == "":
        return None
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None


def _dedupe(values: Any) -> list[str]:
    """Case-insensitive dedupe, source order kept (common_rules.synonyms)."""
    if isinstance(values, (list, tuple)):
        items = values
    elif values in (None, ""):
        return []
    else:
        items = [values]
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item)
        key = text.casefold()
        if key not in seen:
            seen.add(key)
            out.append(text)
    return out


def _normalise_name(name: str, size_re: re.Pattern[str]) -> str:
    """Design section 8: strip the leading size token, lowercase, collapse."""
    return re.sub(r"\s+", " ", size_re.sub("", name or "")).strip().lower()


def _blank_format(template: str, row: dict) -> str:
    values = {k: str(row.get(k) or "") for k in set(PLACEHOLDER.findall(template))}
    return re.sub(r"\s+", " ", template.format(**values)).strip()


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------
def _product_type(row: dict, table: str, spec: dict, data: dict) -> str:
    common = (data.get("common_rules") or {}).get("product_type") or {}
    value = row.get(common.get("column") or "product_type")
    if value not in (None, ""):
        entry = (data.get("tab_only_mapping") or {}).get(str(value))
        if isinstance(entry, dict) and entry.get("table") == table:
            return str(entry.get("force_product_type") or value)
        return str(value)
    if "category_display_name" in (common.get("fallback") or []):
        return str(spec.get("category_display_name") or "")
    return ""


def _status(obj: SourceRow, common: dict) -> str:
    cfg = common.get("status") or {}
    if obj.archived or obj.row.get("archived_at"):
        return "archived"
    active = obj.row.get("is_active")
    if active is False and obj.table in (cfg.get("is_active_tables") or []):
        return "draft"
    return str(cfg.get("otherwise") or "active")


def _measures(obj: SourceRow, spec: dict, rules: Rules) -> tuple[list[dict], dict, int, list[str]]:
    data = rules.data
    cfg = data.get("measures") or {}
    source_system = str(cfg.get("source_system") or "joineryflow")
    preferred = bool(cfg.get("is_preferred", True))
    items: list[dict] = []
    by_key: dict[str, dict] = {}
    warnings: list[str] = []

    for key, measure in (spec.get("measures") or {}).items():
        if not isinstance(measure, dict):
            continue
        column = measure.get("column")
        raw = obj.row.get(column) if column else None
        if raw in (None, ""):
            continue
        value = _number(raw)
        if value is None:
            warnings.append(f"{key}: cannot convert {raw!r} to a number")
            continue
        item = {
            "key": key,
            "value": value,
            "unit": measure.get("unit"),
            "source_system": source_system,
            "source_ref": f"{obj.table}.{column}",
            "is_preferred": preferred,
        }
        items.append(item)
        by_key[str(key)] = item

    derived = 0
    dcfg = cfg.get("derived_thickness") or {}
    column = dcfg.get("column")
    applies = dcfg.get("applies_to") or []
    if column and obj.table in applies and column not in by_key:
        real = obj.row.get(column) if dcfg.get("only_when_column_is_null", True) else None
        if real in (None, ""):
            match = rules.size_re.search(obj.row.get("description") or "")
            if match:
                item = {
                    "key": column,
                    "value": float(match.group(1)),
                    "unit": dcfg.get("unit") or "mm",
                    "source_system": source_system,
                    "source_ref": f"{obj.table}.description#size",
                    "is_preferred": preferred,
                }
                items.append(item)
                by_key[str(column)] = item
                derived += 1
                if dcfg.get("warning", True):
                    warnings.append(f"{column} derived from description")
    return items, by_key, derived, warnings


def _family(obj: SourceRow, spec: dict, rules: Rules,
            by_key: dict[str, dict]) -> tuple[str | None, str | None, dict]:
    family = spec.get("family")
    if not family:
        return None, None, {}
    parts = [str(obj.row.get(c)).strip() for c in (family.get("key") or [])
             if obj.row.get(c) not in (None, "")]
    fallback = not parts
    if fallback:
        key = _normalise_name(str(obj.row.get("description") or ""), rules.size_re)
    else:
        key = " ".join(parts).lower()
    if not key:
        return None, None, {}

    prefix = str((rules.data.get("families") or {}).get("key_prefix")
                 or "joineryflow:{category_code}:{key}")
    family_key = prefix.format(category_code=spec.get("category_code"), key=key)

    template = str(family.get("name") or "")
    family_name = (key if fallback else _blank_format(template, obj.row)) or key

    axes = list(family.get("axis") or [])
    if fallback:
        axes.append(str((rules.data.get("families") or {}).get("fallback_axis") or "thickness_mm"))
    family_axis: dict[str, Any] = {}
    for axis in dict.fromkeys(axes):
        measure = by_key.get(str(axis))
        if measure:
            family_axis[str(axis)] = measure["value"]
    return family_key, family_name, family_axis


def _build_draft(obj: SourceRow, spec: dict, rules: Rules, common: dict) -> Draft:
    row = obj.row
    source_id = f"{obj.table}:{obj.source_id}"
    name = re.sub(r"\s+", " ", str(row.get("description") or "")).strip()
    legacy = {
        "kind": "legacy_id",
        "value": source_id,
        "source_system": str(((rules.data.get("identity") or {})
                              .get("legacy_identifier") or {}).get("source_system") or "joineryflow"),
        "is_primary": True,
    }

    d = Draft(
        row_no=obj.line_no,
        part_no=source_id,
        name=name,
        category_code=str(spec.get("category_code") or ""),
        unit=str(_first(row, [str((common.get("unit") or {}).get("column") or "um")])
                 or spec.get("unit") or ""),
        currency=str(common.get("currency") or "AUD"),
        material_uid=gen_uid("mat", f"joineryflow:{source_id}"),
        source_system=str(common.get("source_system") or "joineryflow"),
        source_id=source_id,
        source_payload={k: v for k, v in row.items() if k != "image_blob"},
        cost_basis=str(common.get("cost_basis") or "ex_gst"),
        confidence=float(common.get("confidence") or 1.00),
        status=_status(obj, common),
        product_type=_product_type(row, obj.table, spec, rules.data),
        description=row.get((common.get("description") or {}).get("column") or "notes"),
        product_url=row.get((common.get("product_url") or {}).get("column") or "product_website"),
        synonyms=_dedupe(row.get((common.get("synonyms") or {}).get("column") or "synonyms")),
        model_no=row.get("model_number"),
        default_supplier_ref=(str(row["default_supplier_id"])
                              if row.get("default_supplier_id") not in (None, "") else None),
    )

    if not name:
        d.errors.append("name is empty")
    if rules.units and d.unit not in rules.units:
        d.errors.append(f"unknown unit {d.unit!r}")

    brand = _first(row, (common.get("brand_id") or {}).get("columns") or [])
    supplier = _first(row, (common.get("default_supplier_id") or {}).get("columns") or [])
    d.brand = str(brand) if brand is not None else None
    d.supplier = str(supplier) if supplier is not None else None

    lead = _first(row, (common.get("lead_time_days") or {}).get("columns") or [])
    if lead is not None:
        try:
            d.lead_time_days = int(lead)
        except (TypeError, ValueError):
            d.warnings.append(f"lead_time_days: cannot convert {lead!r} to an integer")

    updated = row.get("updated_at")
    if isinstance(updated, str) and re.match(r"^\d{4}-\d{2}-\d{2}", updated):
        d.price_valid_from = updated[:10]

    # Identifiers: the legacy one is primary, so apply() derives the same
    # material_uid as identity.material_uid in mapping v2.
    d.identifiers = [legacy]
    for ident in spec.get("identifiers") or []:
        if not isinstance(ident, dict):
            continue
        value = row.get(ident.get("column"))
        if value in (None, ""):
            continue
        d.identifiers.append({
            "kind": str(ident.get("kind")),
            "value": str(value),
            "source_system": str(ident.get("source_system") or "joineryflow"),
            "is_primary": False,
        })

    # Attributes keep their source column names for mechanical write-back.
    for column in spec.get("attributes") or []:
        value = row.get(column)
        if value not in (None, ""):
            d.attributes[str(column)] = value

    # Cost: COALESCE(unit_cost, own cost column), except hire (D2).
    if spec.get("price_policy") != "attributes_only":
        own = spec.get("own_cost_column")
        raw = row.get("unit_cost")
        if raw in (None, "") and own:
            raw = row.get(own)
        cost = _number(raw)
        if cost is not None:
            d.unit_cost = cost
        elif raw not in (None, ""):
            d.warnings.append(f"unit_cost: cannot convert {raw!r} to a number")

    d.measures, by_key, _derived, warnings = _measures(obj, spec, rules)
    d.warnings.extend(warnings)
    d.family_key, d.family_name, d.family_axis = _family(obj, spec, rules, by_key)
    return d


def _attach_cv_identifiers(drafts: list[Draft], cv_rows: Iterable[dict],
                           data: dict, warnings: list[str]) -> None:
    cfg = (data.get("identity") or {}).get("cv_identifiers") or {}
    if not cfg.get("enabled"):
        return
    by_source = {d.source_id: d for d in drafts}
    for cv in cv_rows:
        row = cv["row"]
        code = row.get(cfg.get("code_column") or "cv_code")
        if code in (None, ""):
            continue
        target = (f"{row.get(cfg.get('target_table_column') or 'target_material_table')}:"
                  f"{row.get(cfg.get('target_id_column') or 'target_material_id')}")
        draft = by_source.get(target)
        if draft is None:
            warnings.append(f"cv_code {code!r} targets a row that is not imported: {target}")
            continue
        draft.identifiers.append({
            "kind": str(cfg.get("kind") or "cv_code"),
            "value": str(code),
            "source_system": str(cfg.get("source_system") or "cv"),
            "is_primary": False,
        })


def _check_identifier_uniqueness(drafts: list[Draft], data: dict) -> int:
    """E5: reject rows whose identifier value is shared across the snapshot."""
    kinds = set(((data.get("identity") or {}).get("uniqueness_check") or {})
                .get("applies_to") or [])
    owners: dict[tuple[str, str], set[str]] = defaultdict(set)
    for d in drafts:
        for ident in d.identifiers:
            if ident["kind"] in kinds:
                owners[(ident["kind"], ident["value"])].add(str(d.source_id))
    collisions = 0
    for (kind, value), sources in owners.items():
        if len(sources) > 1:
            collisions += 1
            for d in drafts:
                if str(d.source_id) in sources:
                    d.errors.append(f"identifier collision: {kind}={value!r}")
    return collisions


def build_drafts(snapshot: Snapshot, rules: Rules, *,
                 include_archived: bool = False) -> tuple[list[Draft], dict]:
    """Pure: snapshot + rules -> Draft objects. No database, no file writes."""
    data = rules.data
    header = snapshot.header
    if rules.source_system != str(header.get("source_system")):
        raise SnapshotError("mapping source.system does not match the snapshot header")
    if rules.workspace != str(header.get("workspace_slug")):
        raise SnapshotError("mapping source.workspace does not match the snapshot header")
    tables = data.get("tables") or {}
    if list(tables) != list(header.get("tables") or []):
        raise RulesError("mapping tables do not match the snapshot header order")

    common = data.get("common_rules") or {}
    warnings: list[str] = []
    drafts: list[Draft] = []
    skipped = 0
    for obj in snapshot.rows:
        if obj.archived and not include_archived:
            skipped += 1
            continue
        spec = tables.get(obj.table)
        if spec is None:
            raise RulesError(f"snapshot table {obj.table!r} has no mapping rules")
        drafts.append(_build_draft(obj, spec, rules, common))

    _attach_cv_identifiers(drafts, snapshot.cv_rows, data, warnings)
    collisions = _check_identifier_uniqueness(drafts, data)
    if include_archived and not any(d.status == "archived" for d in drafts):
        warnings.append("include_archived requested but the snapshot has no archived rows")

    meta = {
        "snapshot": str(snapshot.path),
        "workspace": str(header.get("workspace_slug")),
        "include_archived": include_archived,
        "skipped_archived": skipped,
        "identifier_collisions": collisions,
        "warnings": warnings,
    }
    return drafts, meta


# ---------------------------------------------------------------------------
# Reporting (the numbers that --dry-run will print in step 3)
# ---------------------------------------------------------------------------
def build_report(drafts: list[Draft], meta: dict, rules: Rules) -> dict:
    # Keep the three zero-row tables visible in the report as explicit zeros.
    table_names = list(rules.data.get("tables") or {})
    tables: dict[str, int] = dict.fromkeys(table_names, 0)
    attributes_empty: dict[str, int] = dict.fromkeys(table_names, 0)
    families: dict[str, int] = defaultdict(int)
    categories: set[str] = set()
    derived = column_measures = prices = cv_identifiers = archived = warnings = 0
    identifier_values: set[tuple[str, str]] = set()

    for d in drafts:
        table = str(d.source_id or "").split(":", 1)[0]
        tables[table] = tables.get(table, 0) + 1
        if not d.attributes:
            attributes_empty[table] = attributes_empty.get(table, 0) + 1
        categories.add(d.category_code)
        if d.status == "archived":
            archived += 1
        if d.unit_cost is not None:
            prices += 1
        if d.family_key:
            families[d.family_key] += 1
        warnings += len(d.warnings)
        for ident in d.identifiers:
            identifier_values.add((ident["kind"], ident["value"]))
            if ident["kind"] == "cv_code":
                cv_identifiers += 1
        for measure in d.measures:
            if str(measure.get("source_ref") or "").endswith("#size"):
                derived += 1
            else:
                column_measures += 1

    report = {
        "rows": len(drafts),
        "ok": sum(1 for d in drafts if not d.errors),
        "errors": sum(1 for d in drafts if d.errors),
        "warnings": warnings + len(meta.get("warnings") or []),
        "tables": dict(sorted(tables.items())),
        "archived": archived,
        "skipped_archived": meta.get("skipped_archived", 0),
        "categories_with_rows": len(categories),
        "measures_from_columns": column_measures,
        "derived_thickness_measures": derived,
        "prices": prices,
        "cv_identifiers": cv_identifiers,
        "families": len(families),
        "single_member_families": sum(1 for n in families.values() if n == 1),
        "family_none": sum(1 for d in drafts if not d.family_key),
        "identifier_values": len(identifier_values),
        "identifier_collisions": meta.get("identifier_collisions", 0),
        "source_payload_on_all_rows": bool(drafts) and all(d.source_payload for d in drafts),
        "attributes_empty": dict(sorted(attributes_empty.items())),
        "error_rows": [{"row": d.row_no, "errors": d.errors} for d in drafts if d.errors][:20],
        "warning_rows": [{"row": d.row_no, "warnings": d.warnings}
                         for d in drafts if d.warnings][:20],
        "meta_warnings": list(meta.get("warnings") or []),
    }
    return report


def check_contract(report: dict, rules: Rules) -> list[str]:
    """Compare the dry-run numbers with import_contract.expected."""
    expected = (rules.data.get("import_contract") or {}).get("expected") or {}
    measured = {
        "materials": report["rows"],
        "archived": report["archived"],
        "categories_with_rows": report["categories_with_rows"],
        "derived_thickness_measures": report["derived_thickness_measures"],
        "prices": report["prices"],
        "cv_identifiers": report["cv_identifiers"],
        "source_payload_on_all_rows": report["source_payload_on_all_rows"],
        "families": report["families"],
        "single_member_families": report["single_member_families"],
    }
    problems = []
    for key, want in expected.items():
        if key == "second_import_new":       # step 3 verifies idempotency
            continue
        if key in measured and measured[key] != want:
            problems.append(f"import_contract.expected.{key}: expected {want}, got {measured[key]}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--snapshot", help="override mapping source.snapshot")
    ap.add_argument("--mapping", help="override mapping/joineryflow.yaml")
    ap.add_argument("--dry-run", action="store_true",
                    help="explicit no-op: step 2 reads and builds but never applies")
    ap.add_argument("--json", dest="json_out", nargs="?", const="-", metavar="FILE",
                    help="print the full report as JSON to stdout, or write it to FILE")
    args = ap.parse_args(argv)

    rules = load_rules(args.mapping)
    snapshot_path = Path(args.snapshot) if args.snapshot else rules.snapshot_path
    snapshot = read_snapshot(snapshot_path)
    drafts, meta = build_drafts(snapshot, rules)
    report = build_report(drafts, meta, rules)
    problems = check_contract(report, rules)
    exit_code = 1 if problems or report["errors"] else 0

    # stdout must stay machine-readable when --json has no FILE argument.
    if args.json_out == "-":
        print(json.dumps(report, sort_keys=True, indent=2))
        return exit_code

    print(f"{'FAIL' if problems or report['errors'] else 'OK'}  {snapshot_path}")
    print(
        "    "
        f"{report['rows']} rows, {report['errors']} errors, "
        f"{report['archived']} archived, {report['categories_with_rows']} categories, "
        f"{report['derived_thickness_measures']} derived measures, "
        f"{report['prices']} prices, {report['cv_identifiers']} cv codes, "
        f"{report['families']} families, "
        f"{report['identifier_collisions']} collisions"
    )
    for problem in problems:
        print(f"  - {problem}")
    for line in report["error_rows"]:
        print(f"  - row {line['row']}: {', '.join(line['errors'])}")
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
