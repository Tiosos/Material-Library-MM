"""Pure tests for price-list parsing (no DB, no web stack).

Run in the CI static job with:
    python -m unittest discover -s tests -p "test_unit_*.py" -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingest.price_import import build_price_rows, price_report, resolve_price_columns


HEADERS = ["Supplier", "Product Code", "Price", "Currency"]


class ResolveColumnsTests(unittest.TestCase):
    def test_canonical_headers(self):
        cols = resolve_price_columns(HEADERS)
        self.assertEqual(cols["supplier"], "Supplier")
        self.assertEqual(cols["code"], "Product Code")
        self.assertEqual(cols["price"], "Price")
        self.assertEqual(cols["currency"], "Currency")

    def test_supplier_german_alias(self):
        cols = resolve_price_columns(["Lieferant", "Artikelnummer", "Preis"])
        self.assertEqual(cols["supplier"], "Lieferant")
        self.assertEqual(cols["code"], "Artikelnummer")
        self.assertEqual(cols["price"], "Preis")

    def test_no_price_column(self):
        self.assertIsNone(resolve_price_columns(["Code", "Name"])["price"])


class BuildPriceRowsTests(unittest.TestCase):
    def test_defaults_and_parsing(self):
        rows = [{"Supplier": "Acme", "Product Code": "A-1", "Price": "$12.50", "Currency": "aud"}]
        drafts, meta = build_price_rows(
            HEADERS, rows, valid_from_default="2026-01-01", cost_basis_default="ex_gst",
        )
        self.assertEqual(len(drafts), 1)
        d = drafts[0]
        self.assertEqual(d.errors, [])
        self.assertEqual(d.code, "A-1")
        self.assertEqual(d.supplier, "Acme")
        self.assertEqual(d.unit_cost, 12.5)
        self.assertEqual(d.currency, "AUD")
        self.assertEqual(d.valid_from, "2026-01-01")
        self.assertEqual(d.qty_break, 0.0)

    def test_supplier_default_when_no_column(self):
        rows = [{"Product Code": "A-1", "Price": "10"}]
        drafts, _ = build_price_rows(
            ["Product Code", "Price"], rows,
            supplier_default="Acme", valid_from_default="2026-02-01",
        )
        self.assertEqual(drafts[0].supplier, "Acme")
        self.assertEqual(drafts[0].errors, [])

    def test_per_row_date_overrides_default(self):
        headers = HEADERS + ["Valid from"]
        rows = [{"Supplier": "A", "Product Code": "X", "Price": "5",
                 "Currency": "AUD", "Valid from": "2026-03-15"}]
        drafts, _ = build_price_rows(headers, rows, valid_from_default="2026-01-01")
        self.assertEqual(drafts[0].valid_from, "2026-03-15")

    def test_missing_code_errors(self):
        rows = [{"Supplier": "A", "Product Code": "", "Price": "5"}]
        drafts, _ = build_price_rows(HEADERS, rows, valid_from_default="2026-01-01")
        self.assertIn("no product code", drafts[0].errors)

    def test_missing_supplier_errors_without_default(self):
        rows = [{"Product Code": "A-1", "Price": "5"}]
        drafts, _ = build_price_rows(["Product Code", "Price"], rows,
                                     valid_from_default="2026-01-01")
        self.assertIn("no supplier (add a column or pass --supplier)", drafts[0].errors)

    def test_missing_valid_from_errors(self):
        rows = [{"Supplier": "A", "Product Code": "X", "Price": "5"}]
        drafts, _ = build_price_rows(HEADERS, rows)
        self.assertIn("no valid_from (add a column or pass --valid-from YYYY-MM-DD)",
                      drafts[0].errors)

    def test_invalid_date_errors(self):
        rows = [{"Supplier": "A", "Product Code": "X", "Price": "5", "Currency": "AUD"}]
        drafts, _ = build_price_rows(HEADERS, rows, valid_from_default="not-a-date")
        self.assertTrue(any("invalid valid_from" in e for e in drafts[0].errors))

    def test_negative_price_errors(self):
        rows = [{"Supplier": "A", "Product Code": "X", "Price": "-5"}]
        drafts, _ = build_price_rows(HEADERS, rows, valid_from_default="2026-01-01")
        self.assertIn("price is negative", drafts[0].errors)

    def test_forced_code_column(self):
        headers = ["PCodeRaw", "Product Code", "Price"]
        rows = [{"PCodeRaw": "X-RAW", "Product Code": "X", "Price": "7"}]
        drafts, _ = build_price_rows(headers, rows, valid_from_default="2026-01-01",
                                     supplier_default="A", code_column="PCodeRaw")
        self.assertEqual(drafts[0].code, "X-RAW")

    def test_no_price_column_raises(self):
        with self.assertRaises(ValueError):
            build_price_rows(["Code", "Name"], [{"Code": "X", "Name": "Thing"}])

    def test_report_counts(self):
        rows = [
            {"Supplier": "A", "Product Code": "X", "Price": "5"},
            {"Supplier": "A", "Product Code": "", "Price": "5"},
        ]
        drafts, meta = build_price_rows(HEADERS, rows, valid_from_default="2026-01-01")
        report = price_report(drafts, meta)
        self.assertEqual(report["rows"], 2)
        self.assertEqual(report["ok"], 1)
        self.assertEqual(report["errors"], 1)
        self.assertEqual(report["error_rows"][0]["row"], 3)


if __name__ == "__main__":
    unittest.main()
