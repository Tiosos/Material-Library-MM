"""Pure tests for cover-row / header-row / worksheet reading (no DB, no web stack).

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

from ingest.auto_mapping import detect_header_row
from ingest.importer import list_sheets, matrix_to_table, read_table


class DetectHeaderRowTests(unittest.TestCase):
    def test_row_one_when_already_a_header(self):
        matrix = [
            ["Part no.", "Colour", "Price"],
            ["P1", "Oak", "10"],
            ["P2", "Ash", "12"],
        ]
        self.assertEqual(detect_header_row(matrix), 1)

    def test_skips_cover_title_and_blank_line(self):
        matrix = [
            ["Acme Pty Ltd - Price List 2026"],
            ["Effective 1 Jan 2026, all prices ex GST"],
            [],
            ["Product Code", "Description", "Thickness (mm)", "Colour", "Price per sheet"],
            ["AC-16", "Oak board 16mm", "16", "Oak", "40.00"],
            ["AC-18", "Oak board 18mm", "18", "Oak", "45.00"],
        ]
        self.assertEqual(detect_header_row(matrix), 4)

    def test_empty_matrix_raises(self):
        with self.assertRaises(ValueError):
            detect_header_row([])

    def test_scan_is_bounded(self):
        matrix = [["junk"]] * 100 + [["Code", "Name"], ["X1", "Thing"]]
        # max_scan=5 means only the first five rows are considered; row 1 still wins.
        self.assertEqual(detect_header_row(matrix, max_scan=5), 1)


class MatrixToTableTests(unittest.TestCase):
    def test_default_header_is_first_row(self):
        headers, rows = matrix_to_table([["A", "B"], ["1", "2"]])
        self.assertEqual(headers, ["A", "B"])
        self.assertEqual(rows, [{"A": "1", "B": "2"}])

    def test_header_row_is_one_based_and_keeps_position(self):
        matrix = [
            ["Cover line"],
            [],
            ["Code", "Colour"],
            ["P1", "Oak"],
            ["P2", "Ash"],
        ]
        headers, rows = matrix_to_table(matrix, 3)
        self.assertEqual(headers, ["Code", "Colour"])
        self.assertEqual(len(rows), 2)

    def test_blank_data_rows_are_dropped(self):
        matrix = [["Code", "Name"], ["P1", "A"], [], ["P2", "B"]]
        _, rows = matrix_to_table(matrix)
        self.assertEqual(len(rows), 2)

    def test_duplicate_headers_get_a_suffix(self):
        headers, _ = matrix_to_table([["Code", "Code"], ["A", "B"]])
        self.assertEqual(headers, ["Code", "Code_2"])

    def test_out_of_range_header_raises(self):
        with self.assertRaises(ValueError):
            matrix_to_table([["A"]], 3)

    def test_blank_header_row_raises(self):
        with self.assertRaises(ValueError):
            matrix_to_table([["", None], ["a", "b"]], 1)


class ReadTableIntegrationTests(unittest.TestCase):
    def _csv(self, text: str) -> Path:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", encoding="utf-8", delete=False)
        tmp.write(text)
        tmp.close()
        self.addCleanup(lambda: tmp_path.unlink(missing_ok=True))
        tmp_path = Path(tmp.name)
        return tmp_path

    def test_read_table_with_header_row(self):
        p = self._csv("Title line\n\nCode,Name\nP1,Panel\n")
        headers, rows = read_table(p, header_row=3)
        self.assertEqual(headers, ["Code", "Name"])
        self.assertEqual(rows[0]["Name"], "Panel")

    def test_csv_has_no_sheets(self):
        p = self._csv("Code,Name\nP1,Panel\n")
        self.assertEqual(list_sheets(p), [])

    def test_xlsx_sheet_selection(self):
        from openpyxl import Workbook

        tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        tmp.close()
        p = Path(tmp.name)
        self.addCleanup(p.unlink, missing_ok=True)
        wb = Workbook()
        wb.active.title = "Notes"
        wb.active.append(["ignore me"])
        ws = wb.create_sheet("Data")
        ws.append(["Code", "Name"])
        ws.append(["P1", "Panel"])
        wb.save(p)
        self.assertEqual(list_sheets(p), ["Notes", "Data"])
        headers, rows = read_table(p, sheet="Data")
        self.assertEqual(headers, ["Code", "Name"])
        self.assertEqual(rows[0]["Code"], "P1")


if __name__ == "__main__":
    unittest.main()
