"""Pure-Python tests for pre-import data-quality signals (no DB, no web stack).

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

from ingest.importer import Draft
from ingest.quality import assess, draft_required_present, within_batch_duplicates


def draft(row: int, code: str, *, attributes=None, measures=None, source="v") -> Draft:
    d = Draft(row_no=row, part_no=code, name=code, category_code="board")
    d.attributes = attributes or {}
    d.measures = measures or []
    d.identifiers = [{"kind": "supplier_code", "value": code, "source_system": source}]
    return d


class RequiredPresentTests(unittest.TestCase):
    def test_attribute_or_measure_both_count(self):
        d = draft(2, "A", attributes={"colour": "oak"}, measures=[{"key": "thickness_mm"}])
        self.assertEqual(draft_required_present(d, ["thickness_mm", "colour", "substrate"]), (2, 3))

    def test_empty_required_is_full(self):
        self.assertEqual(draft_required_present(draft(2, "A"), []), (0, 0))


class WithinBatchDuplicatesTests(unittest.TestCase):
    def test_repeated_code_is_reported(self):
        out = within_batch_duplicates([draft(2, "X"), draft(3, "X"), draft(4, "Y")])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["value"], "X")
        self.assertEqual(out[0]["rows"], [2, 3])

    def test_same_value_different_source_is_not_a_duplicate(self):
        out = within_batch_duplicates([draft(2, "X", source="a"), draft(3, "X", source="b")])
        self.assertEqual(out, [])

    def test_no_duplicates(self):
        self.assertEqual(within_batch_duplicates([draft(2, "A"), draft(3, "B")]), [])


class AssessTests(unittest.TestCase):
    def test_clean_batch_has_no_signals(self):
        d = draft(2, "A", attributes={"colour": "oak", "substrate": "MDF"},
                  measures=[{"key": "thickness_mm"}])
        r = assess([d], required_by_category={"board": ["thickness_mm", "colour", "substrate"]})
        self.assertFalse(r["has_signals"])
        self.assertEqual(r["completeness"]["buckets"], {"full": 1})

    def test_incomplete_row_is_a_signal(self):
        r = assess([draft(2, "A")], required_by_category={"board": ["thickness_mm", "colour"]})
        self.assertTrue(r["has_signals"])
        self.assertEqual(r["completeness"]["incomplete_count"], 1)

    def test_unknown_category_is_a_signal(self):
        r = assess([draft(2, "A")], required_by_category={})
        self.assertTrue(r["has_signals"])
        self.assertEqual(r["completeness"]["unknown_category"], 1)

    def test_no_db_context_skips_completeness_and_does_not_gate(self):
        r = assess([draft(2, "A")], required_by_category=None)
        self.assertFalse(r["completeness"]["checked"])
        self.assertFalse(r["has_signals"])

    def test_existing_collision_is_informational_not_a_signal(self):
        r = assess([draft(2, "A")], required_by_category={"board": []},
                   existing_identifiers={("supplier_code", "A", "v")})
        self.assertEqual(r["collision_count"], 1)
        self.assertFalse(r["has_signals"])

    def test_db_wide_duplicate_candidates_do_not_gate(self):
        r = assess([draft(2, "A")], required_by_category={"board": []},
                   open_duplicate_candidates=9)
        self.assertEqual(r["open_duplicate_candidates"], 9)
        self.assertFalse(r["has_signals"])

    def test_error_rows_are_excluded(self):
        bad = draft(2, "BAD")
        bad.errors.append("name is empty")
        r = assess([bad], required_by_category={"board": ["thickness_mm"]})
        self.assertEqual(r["rows_checked"], 0)
        self.assertFalse(r["has_signals"])


if __name__ == "__main__":
    unittest.main()
