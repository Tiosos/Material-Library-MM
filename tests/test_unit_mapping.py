"""Pure-Python unit tests for ingest.mapping (no database, no web stack).

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

from ingest.mapping import TRANSFORMS, Mapping, load_mapping, norm, to_bool, to_number


def two_field_mapping() -> Mapping:
    return Mapping({
        "vendor": "unit",
        "fields": [
            {"key": "part_no", "target": "identifier", "aliases": ["Part no.", "Artikelnummer"]},
            {"key": "price", "target": "column", "column": "unit_cost", "aliases": ["Unit price"]},
        ],
    })


class NormalizeTests(unittest.TestCase):
    def test_case_space_and_punctuation_are_flattened(self):
        self.assertEqual(norm(" Part No. "), "partno")
        self.assertEqual(norm("Unit price ($)"), "unitprice")
        self.assertEqual(norm("Sheet-Len MM"), "sheetlenmm")

    def test_bom_is_stripped(self):
        self.assertEqual(norm("\ufeffPCode"), "pcode")

    def test_none_and_empty_are_safe(self):
        self.assertEqual(norm(None), "")
        self.assertEqual(norm(""), "")


class NumberTransformTests(unittest.TestCase):
    def test_first_number_is_taken(self):
        self.assertEqual(to_number("110\u00b0"), 110.0)
        self.assertEqual(to_number("16 mm"), 16.0)
        self.assertEqual(to_number("$42.00"), 42.0)
        self.assertEqual(to_number("approx 3 pcs"), 3.0)

    def test_decimal_comma(self):
        self.assertEqual(to_number("1,8 mm"), 1.8)

    def test_negative(self):
        self.assertEqual(to_number("-3.5 mm"), -3.5)

    def test_no_number(self):
        for value in (None, "", "n/a", "abc"):
            with self.subTest(value=value):
                self.assertIsNone(to_number(value))


class BoolTransformTests(unittest.TestCase):
    def test_true_words(self):
        for value in ("1", "true", "YES", "y", "ja", "x"):
            with self.subTest(value=value):
                self.assertIs(to_bool(value), True)

    def test_false_words(self):
        for value in ("0", "false", "NO", "n", "nein"):
            with self.subTest(value=value):
                self.assertIs(to_bool(value), False)

    def test_unknown_and_empty(self):
        for value in (None, "", "maybe"):
            with self.subTest(value=value):
                self.assertIsNone(to_bool(value))


class TransformRegistryTests(unittest.TestCase):
    def test_all_five_transforms(self):
        self.assertEqual(TRANSFORMS["string"]("  a  "), "a")
        self.assertIsNone(TRANSFORMS["string"](None))
        self.assertEqual(TRANSFORMS["number"]("18 mm"), 18.0)
        self.assertIs(TRANSFORMS["bool"]("yes"), True)
        self.assertEqual(TRANSFORMS["upper"](" abc "), "ABC")
        self.assertEqual(TRANSFORMS["lower"](" ABC "), "abc")

    def test_unknown_transform_falls_back_to_string(self):
        m = Mapping({"fields": []})
        self.assertEqual(m.value({"X": " hi "}, "X", {"transform": "nope"}), "hi")


class MappingConstructionTests(unittest.TestCase):
    def test_field_key_and_aliases_are_indexed(self):
        m = two_field_mapping()
        self.assertEqual(m.alias_index["partno"], "part_no")
        self.assertEqual(m.alias_index["artikelnummer"], "part_no")
        self.assertEqual(m.alias_index["unitprice"], "price")
        self.assertEqual(m.vendor, "unit")
        self.assertEqual(m.source_system, "unit")

    def test_missing_key_or_target_is_rejected(self):
        with self.assertRaises(ValueError):
            Mapping({"fields": [{"target": "identifier"}]})
        with self.assertRaises(ValueError):
            Mapping({"fields": [{"key": "x"}]})

    def test_unknown_target_is_rejected(self):
        with self.assertRaises(ValueError):
            Mapping({"fields": [{"key": "x", "target": "nonsense"}]})

    def test_empty_mapping_has_safe_defaults(self):
        m = Mapping({"fields": []})
        self.assertEqual(m.vendor, "unknown")
        self.assertEqual(m.source_system, "unknown")
        self.assertEqual(m.defaults, {})


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.m = two_field_mapping()

    def test_exact_match_and_unmatched_headers(self):
        resolved, unmatched = self.m.resolve(["Part no.", "Unit price", "Mystery"])
        self.assertEqual(resolved, {"part_no": "Part no.", "price": "Unit price"})
        self.assertEqual(unmatched, ["Mystery"])

    def test_normalized_field_key_is_an_implicit_alias(self):
        resolved, unmatched = self.m.resolve(["PARTNO"])
        self.assertEqual(resolved, {"part_no": "PARTNO"})
        self.assertEqual(unmatched, [])

    def test_containment_matches_a_decorated_header(self):
        resolved, _ = self.m.resolve(["Unit price ($)"])
        self.assertEqual(resolved, {"price": "Unit price ($)"})

    def test_fuzzy_header_is_resolved(self):
        resolved, _ = self.m.resolve(["Artikelnummr"])
        self.assertEqual(resolved, {"part_no": "Artikelnummr"})

    def test_second_column_for_the_same_field_is_unmatched(self):
        resolved, unmatched = self.m.resolve(["Part no.", "PART NO"])
        self.assertEqual(resolved, {"part_no": "Part no."})
        self.assertEqual(unmatched, ["PART NO"])


class ValueTests(unittest.TestCase):
    def setUp(self):
        self.m = Mapping({"fields": []})

    def test_default_is_used_when_no_header_matches(self):
        self.assertEqual(self.m.value({}, None, {"default": "fallback"}), "fallback")

    def test_transform_is_applied_to_the_cell(self):
        self.assertEqual(self.m.value({"X": "18 mm"}, "X", {"transform": "number"}), 18.0)

    def test_string_transform_is_the_default(self):
        self.assertEqual(self.m.value({"X": " 7 "}, "X", {}), "7")

    def test_missing_cell_is_none(self):
        self.assertIsNone(self.m.value({}, "X", {}))


class RepositoryMappingTests(unittest.TestCase):
    def test_every_committed_mapping_loads(self):
        paths = sorted((ROOT / "mappings").glob("*.yaml"))
        self.assertTrue(paths, "no mappings/*.yaml found")
        for path in paths:
            with self.subTest(mapping=path.name):
                m = load_mapping(path)
                self.assertTrue(m.vendor)
                self.assertTrue(m.fields)
                self.assertEqual(m.path, path)
                self.assertTrue(
                    any(f["target"] == "identifier" for f in m.fields),
                    "every mapping needs at least one identifier field",
                )


if __name__ == "__main__":
    unittest.main()
