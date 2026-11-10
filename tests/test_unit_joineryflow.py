"""Pure-Python unit tests for ingest.joineryflow (ETL step 2, no database).

Run in the CI static job with:
    python -m unittest discover -s tests -p "test_unit_*.py" -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingest.importer import (
    Draft,
    apply,
    gen_uid,
    material_insert_columns,
    material_insert_sql,
    material_update_sql,
    optional_column_values,
    valid_from_for,
)
from ingest.joineryflow import (
    DEFAULT_SNAPSHOT,
    RulesError,
    Snapshot,
    SnapshotError,
    SourceRow,
    build_drafts,
    build_report,
    check_contract,
    load_rules,
    main as joineryflow_main,
    read_snapshot,
    rules_from_mapping,
    seed_codes,
)

RULES = load_rules()
FIXTURE = read_snapshot(DEFAULT_SNAPSHOT)
DRAFTS, META = build_drafts(FIXTURE, RULES)
REPORT = build_report(DRAFTS, META, RULES)


def make_snapshot(rows, cv=(), *, include_archived=False) -> Snapshot:
    """rows: (table, source_id, archived, row dict); cv: cv_mapping objects."""
    header = {
        "kind": "header",
        "schema_version": "1",
        "source_system": "joineryflow",
        "workspace_slug": "hartwood-joinery",
        "tables": list(RULES.data["tables"]),
        "include_archived": include_archived,
    }
    source_rows = tuple(
        SourceRow(i + 2, table, sid, archived, row)
        for i, (table, sid, archived, row) in enumerate(rows)
    )
    cv_rows = tuple({"line_no": 90 + i, "table": "cv_material_mapping", "row": row}
                    for i, row in enumerate(cv))
    return Snapshot(header=header, rows=source_rows, cv_rows=cv_rows,
                    path=Path("synthetic.jsonl"))


def board(sid=1, **overrides):
    row = {
        "material_id": sid,
        "description": "18mm White MDF",
        "sku": f"SKU-{sid}",
        "code": f"CODE-{sid}",
        "unit_cost": "45.0000",
        "default_supplier": "Acme",
        "updated_at": "2026-11-08T09:58:47Z",
        "is_active": True,
        "synonyms": ["BM-1", "bm-1", "White MDF"],
    }
    row.update(overrides)
    return ("board_materials", sid, False, row)


class FixtureTests(unittest.TestCase):
    def test_snapshot_shape(self):
        self.assertEqual(FIXTURE.header["source_system"], "joineryflow")
        self.assertEqual(FIXTURE.header["workspace_slug"], "hartwood-joinery")
        self.assertEqual(len(FIXTURE.header["tables"]), 9)
        self.assertEqual(len(FIXTURE.rows), 18)
        self.assertEqual(len(FIXTURE.cv_rows), 2)
        self.assertFalse(any(r.archived for r in FIXTURE.rows))
        self.assertTrue(all("image_blob" not in r.row for r in FIXTURE.rows))

    def test_contract_numbers(self):
        self.assertEqual(check_contract(REPORT, RULES), [])
        self.assertEqual(REPORT["rows"], 18)
        self.assertEqual(REPORT["errors"], 0)
        self.assertEqual(REPORT["archived"], 0)
        self.assertEqual(REPORT["categories_with_rows"], 6)
        self.assertEqual(REPORT["derived_thickness_measures"], 10)
        self.assertEqual(REPORT["measures_from_columns"], 1)
        self.assertEqual(REPORT["prices"], 13)
        self.assertEqual(REPORT["cv_identifiers"], 2)
        self.assertEqual(REPORT["families"], 17)
        self.assertEqual(REPORT["single_member_families"], 17)
        self.assertEqual(REPORT["family_none"], 1)
        self.assertEqual(REPORT["identifier_collisions"], 0)
        self.assertTrue(REPORT["source_payload_on_all_rows"])

    def test_table_counts_and_attributes_empty(self):
        self.assertEqual(REPORT["tables"]["board_materials"], 10)
        self.assertEqual(REPORT["tables"]["hardware_materials"], 4)
        self.assertEqual(REPORT["tables"]["equipment_hire"], 1)
        self.assertEqual(REPORT["tables"]["edging_materials"], 0)
        self.assertEqual(len(REPORT["tables"]), 9)
        # Every live row has at least one declared attribute (including boolean
        # flags), so no table is entirely empty; the zeros stay explicit.
        self.assertEqual(len(REPORT["attributes_empty"]), 9)
        self.assertEqual(sum(REPORT["attributes_empty"].values()), 0)

    def test_primary_identifier_and_uid(self):
        d = DRAFTS[0]
        self.assertEqual(d.identifiers[0], {
            "kind": "legacy_id", "value": "board_materials:1",
            "source_system": "joineryflow", "is_primary": True,
        })
        self.assertEqual(d.source_id, "board_materials:1")
        self.assertEqual(d.material_uid, gen_uid("mat", "joineryflow:board_materials:1"))
        self.assertEqual(d.source_system, "joineryflow")
        self.assertEqual(d.status, "active")
        self.assertEqual(d.currency, "AUD")
        self.assertEqual(d.cost_basis, "ex_gst")

    def test_derived_thickness_and_family_fallback(self):
        d = DRAFTS[0]
        sizes = [m for m in d.measures if m["source_ref"].endswith("#size")]
        self.assertEqual(len(sizes), 1)
        self.assertEqual(sizes[0]["value"], 18.0)
        self.assertEqual(sizes[0]["unit"], "mm")
        self.assertTrue(sizes[0]["is_preferred"])
        self.assertTrue(any("derived from description" in w for w in d.warnings))
        self.assertEqual(d.family_key, "joineryflow:board:white mdf")
        self.assertEqual(d.family_axis, {"thickness_mm": 18.0})
        self.assertEqual(d.product_type, "Board")

    def test_real_measure_column_beats_derived(self):
        d = next(d for d in DRAFTS if d.source_id == "benchtop_materials:1")
        sizes = [m for m in d.measures if m["key"] == "thickness_mm"]
        self.assertEqual(len(sizes), 1)
        self.assertEqual(sizes[0]["value"], 20.0)
        self.assertEqual(sizes[0]["source_ref"], "benchtop_materials.thickness_mm")
        self.assertFalse(any("derived from description" in w for w in d.warnings))

    def test_hire_policy_d2(self):
        d = next(d for d in DRAFTS if d.source_id == "equipment_hire:1")
        self.assertIsNone(d.unit_cost)
        self.assertIsNone(d.family_key)
        for key in ("rate", "rate_unit", "total_cost", "contract_ref", "project_id"):
            self.assertIn(key, d.attributes)
        self.assertEqual(d.unit, "ea")
        self.assertEqual(REPORT["prices"], 13)

    def test_supplier_chain_and_cv_identifiers(self):
        custom = next(d for d in DRAFTS if d.source_id == "custom_made:1")
        self.assertEqual(custom.supplier, "Metalform")
        self.assertEqual(custom.product_type, "Custom Made")
        by_source = {d.source_id: d for d in DRAFTS}
        cv = [i for d in DRAFTS for i in d.identifiers if i["kind"] == "cv_code"]
        self.assertEqual(len(cv), 2)
        self.assertTrue(all(i["source_system"] == "cv" for i in cv))
        self.assertIn(cv[0], by_source["board_materials:1"].identifiers)
        self.assertIn(cv[1], by_source["hardware_materials:2"].identifiers)

    def test_synonyms_dedupe_and_source_payload(self):
        d = DRAFTS[0]
        self.assertEqual(d.synonyms, ["BM-001", "18-WHITE-MDF", "18mm white mdf"])
        self.assertEqual(d.source_payload["description"], "18mm White MDF")
        self.assertNotIn("image_blob", d.source_payload)
        self.assertEqual(d.price_valid_from, "2026-11-08")
        self.assertEqual(valid_from_for(d), date(2026, 11, 8))

    def test_seed_codes_and_rules_guard(self):
        units = seed_codes((ROOT / "schema" / "seed.sql").read_text(encoding="utf-8"), "unit")
        self.assertIn("sheet", units)
        self.assertNotIn("day", units)
        with self.assertRaises(RulesError):
            rules_from_mapping({"version": 1}, units=units)


class ReadSnapshotTests(unittest.TestCase):
    def _write(self, lines: list[str]) -> Path:
        tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".jsonl", delete=False)
        tmp.write("\n".join(lines) + "\n")
        tmp.close()
        path = Path(tmp.name)
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return path

    def header(self, **overrides):
        h = {"kind": "header", "schema_version": "1", "source_system": "joineryflow",
             "workspace_slug": "hartwood-joinery", "include_archived": False,
             "tables": ["board_materials"]}
        h.update(overrides)
        return h

    def line(self, obj) -> str:
        return json.dumps(obj, sort_keys=True)

    def test_header_must_come_first(self):
        path = self._write([self.line({"kind": "row", "table": "board_materials",
                                       "source_id": 1, "row": {}})])
        with self.assertRaisesRegex(SnapshotError, "first line"):
            read_snapshot(path)

    def test_header_fields_are_checked(self):
        h = self.header()
        del h["workspace_slug"]
        path = self._write([self.line(h)])
        with self.assertRaisesRegex(SnapshotError, "workspace_slug"):
            read_snapshot(path)

    def test_unknown_kind_is_rejected(self):
        path = self._write([self.line(self.header()),
                            self.line({"kind": "mystery", "row": {}})])
        with self.assertRaisesRegex(SnapshotError, "unknown kind"):
            read_snapshot(path)

    def test_duplicate_primary_key_is_rejected(self):
        row = {"kind": "row", "table": "board_materials", "source_id": 7,
               "archived": False, "row": {"material_id": 7}}
        path = self._write([self.line(self.header()), self.line(row), self.line(row)])
        with self.assertRaisesRegex(SnapshotError, "duplicate primary key"):
            read_snapshot(path)

    def test_same_id_in_two_tables_is_allowed(self):
        a = {"kind": "row", "table": "board_materials", "source_id": 1,
             "row": {"material_id": 1}}
        b = {"kind": "row", "table": "hardware_materials", "source_id": 1,
             "row": {"material_id": 1}}
        path = self._write([self.line(self.header(tables=["board_materials",
                                                           "hardware_materials"])),
                            self.line(a), self.line(b)])
        self.assertEqual(len(read_snapshot(path).rows), 2)

    def test_missing_source_id_and_unknown_table(self):
        for obj, message in (
            ({"kind": "row", "table": "board_materials", "row": {}}, "no source_id"),
            ({"kind": "row", "table": "other", "source_id": 1, "row": {}}, "not in the header"),
        ):
            with self.subTest(message=message):
                path = self._write([self.line(self.header()), self.line(obj)])
                with self.assertRaisesRegex(SnapshotError, message):
                    read_snapshot(path)

    def test_image_blob_and_blank_line_are_rejected(self):
        row = {"kind": "row", "table": "board_materials", "source_id": 1,
               "row": {"image_blob": 123}}
        path = self._write([self.line(self.header()), self.line(row)])
        with self.assertRaisesRegex(SnapshotError, "image_blob"):
            read_snapshot(path)
        path2 = self._write([self.line(self.header()), "", self.line(row)])
        with self.assertRaisesRegex(SnapshotError, "blank lines"):
            read_snapshot(path2)

    def test_invalid_json_reports_the_line(self):
        path = self._write([self.line(self.header()), "{not json"])
        with self.assertRaisesRegex(SnapshotError, "line 2"):
            read_snapshot(path)

    def test_archived_flag_and_cv_mapping(self):
        row = {"kind": "row", "table": "board_materials", "source_id": 1,
               "archived": False, "row": {"archived_at": "2026-11-01T00:00:00Z"}}
        cv = {"kind": "cv_mapping", "table": "cv_material_mapping", "source_id": 1,
              "row": {"cv_code": "18-PB"}}
        path = self._write([self.line(self.header(include_archived=True)),
                            self.line(row), self.line(cv)])
        snap = read_snapshot(path)
        self.assertTrue(snap.rows[0].archived)
        self.assertEqual(snap.cv_rows[0]["row"]["cv_code"], "18-PB")


class BuildDraftsSyntheticTests(unittest.TestCase):
    def build(self, rows, cv=(), *, include_archived=False, rules=None):
        snap = make_snapshot(rows, cv, include_archived=include_archived)
        return build_drafts(snap, rules or RULES, include_archived=include_archived)

    def test_name_from_description_and_empty_name_error(self):
        drafts, _ = self.build([board()])
        self.assertEqual(drafts[0].name, "18mm White MDF")
        drafts, _ = self.build([board(description="   ")])
        self.assertIn("name is empty", drafts[0].errors)

    def test_derived_thickness_and_real_column_precedence(self):
        drafts, _ = self.build([board()])
        self.assertEqual(drafts[0].measures[0]["value"], 18.0)
        self.assertTrue(drafts[0].measures[0]["source_ref"].endswith("#size"))
        drafts, _ = self.build([board(thickness_mm="20.00")])
        self.assertEqual(len(drafts[0].measures), 1)
        self.assertEqual(drafts[0].measures[0]["value"], 20.0)
        self.assertEqual(drafts[0].measures[0]["source_ref"], "board_materials.thickness_mm")
        self.assertFalse(any("derived" in w for w in drafts[0].warnings))

    def test_family_key_from_real_columns_and_fallback(self):
        drafts, _ = self.build([board(colour="White", substrate="MDF", finish="Matt")])
        self.assertEqual(drafts[0].family_key, "joineryflow:board:white mdf matt")
        self.assertEqual(drafts[0].family_name, "White MDF Matt")
        drafts, _ = self.build([board(description="18mm White MDF")])
        self.assertEqual(drafts[0].family_key, "joineryflow:board:white mdf")

    def test_identifier_collision_rejects_both_rows(self):
        drafts, meta = self.build([board(1, sku="DUP"), board(2, sku="DUP")])
        self.assertEqual(meta["identifier_collisions"], 1)
        self.assertTrue(all(any("identifier collision" in e for e in d.errors) for d in drafts))

    def test_status_from_archived_at_and_is_active(self):
        drafts, _ = self.build([board(1, archived_at="2026-11-01T00:00:00Z")])
        self.assertEqual(drafts[0].status, "archived")
        drafts, _ = self.build([board(1, is_active=False)])
        self.assertEqual(drafts[0].status, "draft")
        appliance = ("appliances", 1, False, {"material_id": 1, "description": "Oven",
                                              "is_active": False, "um": "ea"})
        drafts, _ = self.build([appliance])
        self.assertEqual(drafts[0].status, "active")

    def test_unknown_unit_is_a_row_error(self):
        drafts, _ = self.build([board(1, um="furlong")])
        self.assertTrue(any("unknown unit" in e for e in drafts[0].errors))
        drafts, _ = self.build([board(1, um="sheet")])
        self.assertEqual(drafts[0].errors, [])

    def test_include_archived_and_synthetic_archived_rows(self):
        archived = ("board_materials", 9, True,
                    {"material_id": 9, "description": "E2E-archived board",
                     "archived_at": "2026-11-01T00:00:00Z", "sku": "E2E-9"})
        drafts, meta = self.build([board(), archived])
        self.assertEqual(len(drafts), 1)
        self.assertEqual(meta["skipped_archived"], 1)
        drafts, meta = self.build([board(), archived], include_archived=True)
        self.assertEqual(len(drafts), 2)
        self.assertEqual(drafts[1].status, "archived")
        self.assertEqual(build_report(drafts, meta, RULES)["archived"], 1)
        drafts, meta = self.build([board()], include_archived=True)
        self.assertTrue(any("no archived rows" in w for w in meta["warnings"]))

    def test_product_type_tab_only_and_category_fallback(self):
        drafts, _ = self.build([board(1, product_type="Laminate")])
        self.assertEqual(drafts[0].product_type, "Laminate")
        drafts, _ = self.build([board(1, product_type=None)])
        self.assertEqual(drafts[0].product_type, "Board")

    def test_cv_target_missing_warns_and_can_be_disabled(self):
        cv = [{"cv_code": "18-PB", "target_material_table": "board_materials",
               "target_material_id": 99}]
        drafts, meta = self.build([board()], cv=cv)
        self.assertTrue(any("not imported" in w for w in meta["warnings"]))
        self.assertFalse(any(i["kind"] == "cv_code" for i in drafts[0].identifiers))
        data = json.loads(json.dumps(RULES.data))
        data["identity"]["cv_identifiers"]["enabled"] = False
        disabled = rules_from_mapping(data, units=RULES.units)
        cv_ok = [{"cv_code": "18-PB", "target_material_table": "board_materials",
                  "target_material_id": 1}]
        drafts, _ = self.build([board()], cv=cv_ok, rules=disabled)
        self.assertFalse(any(i["kind"] == "cv_code" for i in drafts[0].identifiers))

    def test_attributes_keep_source_names_and_structural_columns_do_not_leak(self):
        drafts, _ = self.build([board(1, colour="White", workspace_id=1,
                                      created_at="2026-11-08T09:58:47Z")])
        self.assertEqual(drafts[0].attributes["colour"], "White")
        self.assertNotIn("workspace_id", drafts[0].attributes)
        self.assertIn("workspace_id", drafts[0].source_payload)

    def test_header_workspace_mismatch_is_refused(self):
        snap = make_snapshot([board()])
        header = dict(snap.header, workspace_slug="another-workspace")
        bad = Snapshot(header=header, rows=snap.rows, cv_rows=snap.cv_rows, path=snap.path)
        with self.assertRaisesRegex(SnapshotError, "workspace"):
            build_drafts(bad, RULES)


class ImporterExtensionTests(unittest.TestCase):
    def test_supplier_draft_has_no_optional_columns(self):
        d = Draft(row_no=2, name="Oak", part_no="P1")
        self.assertIsNone(d.source_id)
        self.assertEqual(d.synonyms, [])
        self.assertEqual(optional_column_values(d), {})
        self.assertNotIn("source_payload", material_update_sql({}))
        self.assertTrue(material_update_sql({}).rstrip().endswith("WHERE material_uid = %s"))

    def test_joineryflow_draft_extends_both_statements(self):
        d = DRAFTS[0]
        extra = optional_column_values(d)
        for name in ("source_system", "source_id", "source_payload", "status",
                     "synonyms", "product_type", "lead_time_days", "confidence"):
            self.assertIn(name, extra)
        columns = material_insert_columns(extra)
        self.assertIn("source_payload", columns)
        self.assertEqual(len(columns), len(set(columns)))
        insert = material_insert_sql(columns)
        self.assertIn("source_payload", insert)
        self.assertIn("%s::jsonb", insert)
        self.assertIn("%s::text[]", insert)
        self.assertIn("status = %s", material_update_sql(extra))

    def test_valid_from_defaults_to_today(self):
        self.assertEqual(valid_from_for(Draft(row_no=2)), date.today())


class ApplySqlTests(unittest.TestCase):
    """A recording cursor stands in for PostgreSQL (no psql/docker on this box).

    It verifies the optional-column SQL is well formed: every %s has a bound
    parameter, supplier drafts keep the historical statement, and the price
    row carries the snapshot's updated_at date.
    """

    class Cursor:
        def __init__(self, categories):
            self.categories = categories
            self.calls: list[tuple[str, tuple]] = []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            self.calls.append((sql, tuple(params or ())))

        def fetchone(self):
            sql = self.calls[-1][0]
            if "FROM material_identifier" in sql:
                return None            # every row is a new material
            return (1,)                # party / family already exist

        def fetchall(self):
            return [(c,) for c in self.categories]

    class Connection:
        def __init__(self, cursor):
            self.cursor_obj = cursor

        def cursor(self):
            return self.cursor_obj

    def setUp(self):
        cats = {spec["category_code"] for spec in RULES.data["tables"].values()}
        self.cursor = self.Cursor(sorted(cats))
        self.stats = apply(self.Connection(self.cursor), DRAFTS,
                           SimpleNamespace(vendor="joineryflow",
                                           source_system="joineryflow", defaults={}))

    def test_all_rows_are_new_and_every_placeholder_is_bound(self):
        self.assertEqual(self.stats["new"], 18)
        self.assertEqual(self.stats["skipped"], 0)
        for sql, params in self.cursor.calls:
            with self.subTest(sql=sql[:60]):
                self.assertEqual(sql.count("%s"), len(params))

    def test_material_insert_carries_optional_columns(self):
        inserts = [sql for sql, _ in self.cursor.calls if sql.startswith("INSERT INTO material (")]
        self.assertEqual(len(inserts), 18)
        self.assertTrue(all("source_payload" in sql and "status" in sql for sql in inserts))
        # source_payload is on every row, so every insert binds a jsonb.
        # synonyms is optional: only the 14 rows with a non-empty synonym list
        # get a text[] column. apply() emits inserts in draft order, so each
        # statement can be checked against the draft that produced it.
        self.assertEqual(sum(1 for d in DRAFTS if d.synonyms), 14)
        for d, sql in zip(DRAFTS, inserts):
            with self.subTest(part_no=d.part_no):
                self.assertIn("%s::jsonb", sql)
                self.assertEqual("%s::text[]" in sql, bool(d.synonyms))
        self.assertEqual(sum(1 for sql in inserts if "%s::text[]" in sql), 14)

    def test_price_rows_use_the_snapshot_date(self):
        prices = [params for sql, params in self.cursor.calls
                  if sql.startswith("INSERT INTO material_price")]
        self.assertEqual(len(prices), 13)
        self.assertTrue(all(p[4] == date(2026, 11, 8) for p in prices))
        self.assertTrue(all(p[5] == "ex_gst" for p in prices))
        self.assertTrue(all(p[6] == "joineryflow" for p in prices))

    def test_supplier_draft_keeps_the_historical_statement(self):
        supplier = Draft(row_no=2, name="Oak", part_no="P1", category_code="board",
                         identifiers=[{"kind": "supplier_code", "value": "P1",
                                       "source_system": "unit_test"}],
                         unit_cost=10.0)
        cursor = self.Cursor(["board"])
        apply(self.Connection(cursor), [supplier],
              SimpleNamespace(vendor="unit", source_system="unit_test", defaults={}))
        insert = [sql for sql, _ in cursor.calls if sql.startswith("INSERT INTO material (")][0]
        self.assertNotIn("source_payload", insert)
        self.assertNotIn("status =", material_update_sql({}))


class HarnessCliTests(unittest.TestCase):
    """The step 2 harness answers design section 13's --dry-run --json line.

    It is still a pure read-and-build verification: --dry-run is an explicit
    no-op flag, and no database connection is opened by any of these paths.
    """

    def test_dry_run_json_stdout_is_machine_readable(self):
        buf = StringIO()
        with redirect_stdout(buf):
            code = joineryflow_main(["--dry-run", "--json"])
        self.assertEqual(code, 0)
        report = json.loads(buf.getvalue())
        self.assertEqual(report["rows"], 18)
        self.assertEqual(report["errors"], 0)
        self.assertEqual(report["derived_thickness_measures"], 10)
        self.assertEqual(report["families"], 17)
        self.assertEqual(report["prices"], 13)   # D2: 13, not the design's stale 14
        self.assertEqual(check_contract(report, RULES), [])

    def test_json_file_branch_writes_the_same_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "nested" / "report.json"
            buf = StringIO()
            with redirect_stdout(buf):
                code = joineryflow_main(["--dry-run", "--json", str(out)])
            self.assertEqual(code, 0)
            self.assertIn("OK", buf.getvalue())
            self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["rows"], 18)

    def test_contract_mismatch_exits_nonzero(self):
        data = json.loads(json.dumps(RULES.data))   # cheap deep copy
        data["import_contract"]["expected"]["prices"] = 99
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mapping.yaml"
            path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
            buf = StringIO()
            with redirect_stdout(buf):
                code = joineryflow_main(["--dry-run", "--mapping", str(path)])
        self.assertEqual(code, 1)
        self.assertIn("import_contract.expected.prices", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
