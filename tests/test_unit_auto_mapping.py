"""Pure-Python unit tests for header -> mapping inference (no DB, no web stack).

Run in the CI static job with:
    python -m unittest discover -s tests -p "test_unit_*.py" -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingest.auto_mapping import infer_mapping
from ingest.importer import build_drafts
from ingest.mapping import Mapping


class InferMappingTests(unittest.TestCase):
    def test_code_name_price_and_dimension_roles(self):
        mapping, report = infer_mapping(
            ["Product Code", "Description", "Price", "Thickness (mm)", "Colour"],
            [
                {"Product Code": "A-1", "Description": "Board 16", "Price": "42.00",
                 "Thickness (mm)": "16", "Colour": "Black"},
            ],
            vendor="acme",
        )
        by_role = {f["target"]: f for f in mapping["fields"]}
        self.assertEqual(by_role["identifier"]["kind"], "supplier_code")
        self.assertTrue(by_role["identifier"]["required"])
        columns = {f.get("column"): f for f in mapping["fields"] if f["target"] == "column"}
        self.assertIn("name", columns)
        price = next(f for f in mapping["fields"] if f.get("column") == "unit_cost")
        self.assertEqual(price["transform"], "number")
        measure = next(f for f in mapping["fields"] if f["target"] == "measure")
        self.assertEqual(measure["measure_key"], "thickness_mm")
        self.assertEqual(measure["unit"], "mm")
        self.assertEqual(report["identifier"], "Product Code")
        attr = next(f for f in mapping["fields"] if f["target"] == "attribute")
        self.assertEqual(attr["key"], "colour")

    def test_generated_mapping_matches_the_real_schema(self):
        from jsonschema import Draft202012Validator

        schema = json.loads((ROOT / "schema/mapping.schema.json").read_text(encoding="utf-8"))
        mapping, _ = infer_mapping(
            ["Artikelnummer", "Bezeichnung", "Preis", "Farbe"],
            [{"Artikelnummer": "X", "Bezeichnung": "Panel", "Preis": "1,5", "Farbe": "weiss"}],
            vendor="dealer",
        )
        errors = list(Draft202012Validator(schema).iter_errors(mapping))
        self.assertEqual(errors, [], [e.message for e in errors])

    def test_generated_mapping_builds_drafts(self):
        mapping, _ = infer_mapping(
            ["SKU", "Product Name", "Unit Price", "Width"],
            [{"SKU": "S1", "Product Name": "Panel A", "Unit Price": "10.5", "Width": "1200"}],
            vendor="v",
        )
        drafts, meta = build_drafts(
            ["SKU", "Product Name", "Unit Price", "Width"],
            [{"SKU": "S1", "Product Name": "Panel A", "Unit Price": "10.5", "Width": "1200"}],
            Mapping(mapping),
        )
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].errors, [])
        self.assertEqual(drafts[0].part_no, "S1")
        self.assertEqual(drafts[0].name, "Panel A")
        self.assertEqual(drafts[0].unit_cost, 10.5)
        self.assertEqual(len(drafts[0].measures), 1)

    def test_explicit_code_column_overrides_alias(self):
        mapping, report = infer_mapping(
            ["ItemDescription", "ItemNo"],
            [{"ItemDescription": "Panel", "ItemNo": "123"}],
            vendor="v", code_column="ItemNo",
        )
        self.assertEqual(report["identifier"], "ItemNo")

    def test_first_data_column_fallback_warns(self):
        _, report = infer_mapping(
            ["Article", "Colour"],
            [{"Article": "X1", "Colour": "red"}],
            vendor="v",
        )
        self.assertEqual(report["identifier"], "Article")
        self.assertTrue(report["warnings"])

    def test_unit_parameter_is_not_polluted_by_measure(self):
        mapping, _ = infer_mapping(
            ["Code", "Thickness (mm)"],
            [{"Code": "A", "Thickness (mm)": "16"}],
            vendor="v",
        )
        self.assertNotIn("unit", mapping["defaults"])

    def test_no_headers_raises(self):
        with self.assertRaises(ValueError):
            infer_mapping([], [], vendor="v")

    def test_bad_vendor_raises(self):
        with self.assertRaises(ValueError):
            infer_mapping(["Code"], [{"Code": "A"}], vendor="bad vendor!")


if __name__ == "__main__":
    unittest.main()
