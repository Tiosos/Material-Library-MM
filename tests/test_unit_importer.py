"""Pure-Python unit tests for ingest.importer (no database).

Run in the CI static job with:
    python -m unittest discover -s tests -p "test_unit_*.py" -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingest.importer import _fmt, _slug, build_drafts, gen_uid, plan_report, read_table
from ingest.mapping import Mapping

HEADERS = ["Part no.", "Description", "Thickness", "Colour", "Price", "Extra"]


def make_mapping(**overrides) -> Mapping:
    data = {
        "vendor": "unit",
        "source_system": "unit_test",
        "defaults": {
            "category_code": "board.melamine.mdf",
            "unit": "sheet",
            "currency": "AUD",
            "brand": "Acme",
        },
        "extra_headers": "attributes",
        "fields": [
            {"key": "part_no", "target": "identifier", "kind": "supplier_code",
             "required": True, "aliases": ["Part no."]},
            {"key": "title", "target": "attribute", "aliases": ["Description"]},
            {"key": "thickness", "target": "measure", "measure_key": "thickness_mm",
             "unit": "mm", "transform": "number", "aliases": ["Thickness"]},
            {"key": "colour", "target": "attribute", "aliases": ["Colour"]},
            {"key": "price", "target": "column", "column": "unit_cost",
             "transform": "number", "aliases": ["Price"]},
        ],
        "name_template": "{brand} {title} {thickness}mm {colour}",
        "family": {
            "key": ["colour"],
            "axis": ["thickness"],
            "name_template": "{brand} {colour}",
        },
    }
    data.update(overrides)
    return Mapping(data)


def sample_row(**overrides) -> dict:
    row = {"Part no.": "P1", "Description": "Oak Board", "Thickness": "18 mm",
           "Colour": "Natural", "Price": "$42.50", "Extra": "note"}
    row.update(overrides)
    return row


class UidTests(unittest.TestCase):
    def test_uid_is_stable_and_well_formed(self):
        a = gen_uid("mat", "unit_test:P1")
        self.assertEqual(a, gen_uid("mat", "unit_test:P1"))
        self.assertRegex(a, r"^mat_[0-9A-HJKMNP-TV-Z]{26}$")
        self.assertNotEqual(a, gen_uid("mat", "unit_test:P2"))
        self.assertRegex(gen_uid("fam", "unit_test:P1"), r"^fam_[0-9A-HJKMNP-TV-Z]{26}$")


class HelperTests(unittest.TestCase):
    def test_fmt_and_slug(self):
        self.assertEqual(_fmt(110.0), "110")
        self.assertEqual(_fmt(None), "")
        self.assertEqual(_fmt("abc"), "abc")
        self.assertEqual(_slug(" Laminex Black MDF "), "laminex-black-mdf")
        self.assertEqual(_slug(None), "")


class ReadTableTests(unittest.TestCase):
    def _write(self, text: str) -> Path:
        tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".csv", delete=False)
        tmp.write(text)
        tmp.close()
        path = Path(tmp.name)
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return path

    def test_csv_with_bom(self):
        path = self._write("\ufeffPart no.,Description\nP1,Oak\nP2,Ash\n")
        headers, rows = read_table(path)
        self.assertEqual(headers, ["Part no.", "Description"])
        self.assertEqual(rows, [
            {"Part no.": "P1", "Description": "Oak"},
            {"Part no.": "P2", "Description": "Ash"},
        ])

    def test_semicolon_delimited_csv(self):
        path = self._write("Part no.;Description\nP1;Oak\n")
        headers, rows = read_table(path)
        self.assertEqual(headers, ["Part no.", "Description"])
        self.assertEqual(rows[0]["Description"], "Oak")

    def test_suffix_selects_the_reader(self):
        path = self._write("Part no.,Description\nP1,Oak\n")
        self.assertEqual(read_table(path)[0], ["Part no.", "Description"])


class BuildDraftsTests(unittest.TestCase):
    def test_every_target_is_mapped(self):
        drafts, meta = build_drafts(HEADERS, [sample_row()], make_mapping())
        self.assertEqual(len(drafts), 1)
        d = drafts[0]
        self.assertEqual(d.part_no, "P1")
        self.assertEqual(d.identifiers, [
            {"kind": "supplier_code", "value": "P1", "source_system": "unit_test"}
        ])
        self.assertEqual(d.measures, [{
            "key": "thickness_mm", "value": 18.0, "unit": "mm",
            "source_system": "unit_test", "source_ref": None,
        }])
        self.assertEqual(d.attributes["title"], "Oak Board")
        self.assertEqual(d.attributes["colour"], "Natural")
        self.assertEqual(d.attributes["src.extra"], "note")
        self.assertEqual(d.unit_cost, 42.5)
        self.assertEqual(d.category_code, "board.melamine.mdf")
        self.assertEqual(d.unit, "sheet")
        self.assertEqual(d.currency, "AUD")
        self.assertEqual(d.brand, "Acme")
        self.assertEqual(d.name, "Acme Oak Board 18mm Natural")
        self.assertEqual(d.family_key, "acme|natural")
        self.assertEqual(d.family_name, "Acme Natural")
        self.assertEqual(d.family_axis, {"thickness_mm": 18.0})
        self.assertEqual(d.errors, [])
        self.assertEqual(d.warnings, [])
        self.assertEqual(meta["unmatched"], ["Extra"])
        self.assertEqual(meta["resolved"]["part_no"], "Part no.")

    def test_name_column_overrides_the_template(self):
        m = make_mapping(
            fields=[
                {"key": "part_no", "target": "identifier", "kind": "supplier_code",
                 "required": True, "aliases": ["Part no."]},
                {"key": "title", "target": "column", "column": "name",
                 "aliases": ["Description"]},
            ],
            name_template="{part_no}",
            family=None,
        )
        d = build_drafts(HEADERS, [sample_row()], m)[0][0]
        self.assertEqual(d.name, "Oak Board")

    def test_measure_key_beats_the_field_key_on_the_family_axis(self):
        d = build_drafts(HEADERS, [sample_row()], make_mapping())[0][0]
        self.assertIn("thickness_mm", d.family_axis)
        self.assertNotIn("thickness", d.family_axis)

    def test_bad_measure_warns_instead_of_writing_a_measure(self):
        # The warning branch is reachable only when the measure field does not
        # declare transform: number. With that transform the raw value already
        # becomes None in the first pass and is skipped before this branch.
        fields = [dict(f) for f in make_mapping().data["fields"]]
        next(f for f in fields if f["key"] == "thickness").pop("transform", None)
        m = make_mapping(fields=fields)
        d = build_drafts(HEADERS, [sample_row(Thickness="n/a")], m)[0][0]
        self.assertEqual(d.measures, [])
        self.assertTrue(any("cannot convert" in w for w in d.warnings))

    def test_missing_required_column_stops_the_import(self):
        with self.assertRaises(SystemExit):
            build_drafts(["Description", "Thickness", "Colour", "Price"],
                         [sample_row()], make_mapping())

    def test_empty_code_and_name_are_reported(self):
        m = make_mapping(name_template="{title}", family=None)
        row = sample_row()
        row["Part no."] = ""
        row["Description"] = ""
        d = build_drafts(HEADERS, [row], m)[0][0]
        self.assertIn("no code column", d.errors)
        self.assertIn("name is empty", d.errors)
        self.assertEqual(d.identifiers, [])

    def test_all_family_keys_empty_warns_and_skips_grouping(self):
        d = build_drafts(HEADERS, [sample_row(Colour="")], make_mapping())[0][0]
        self.assertTrue(any("all family keys are empty" in w for w in d.warnings))
        self.assertIsNone(d.family_key)

    def test_extra_headers_can_be_ignored(self):
        d = build_drafts(HEADERS, [sample_row()], make_mapping(extra_headers="ignore"))[0][0]
        self.assertNotIn("src.extra", d.attributes)


class PlanReportTests(unittest.TestCase):
    def test_counts_samples_and_errors(self):
        good = sample_row()
        bad = sample_row()
        bad["Part no."] = ""
        bad["Description"] = ""
        m = make_mapping(name_template="{title}", family=None)
        drafts, meta = build_drafts(HEADERS, [good, bad], m)
        report = plan_report(drafts, meta)
        self.assertEqual(report["rows"], 2)
        self.assertEqual(report["ok"], 1)
        self.assertEqual(report["errors"], 1)
        self.assertEqual(len(report["sample"]), 2)
        self.assertEqual(report["error_rows"][0]["errors"], ["name is empty", "no code column"])


if __name__ == "__main__":
    unittest.main()
