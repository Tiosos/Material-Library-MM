"""Pure-logic tests for the Antaro catalogue extractor (no PDF / DB needed)."""
import unittest

from ingest.pdf_antaro import (
    is_part,
    _spec_on_row,
    _drawer_type,
    _colour,
    _component_name,
    extract_page,
)


class IsPartTests(unittest.TestCase):
    def test_real_part_forms(self):
        for p in ["378N4002SA", "578.4001M", "Z30N000S.04", "ZRG.207RSIC",
                  "ZIF.80M5", "609.1500", "661.1000.HG", "M01.ZZ03.01", "Z31L1036A"]:
            with self.subTest(p=p):
                self.assertTrue(is_part(p), f"{p} should be a part number")

    def test_noise_rejected(self):
        for p in ["DQC21A", "DQC2QM", "R9006", "2-dimensional", "3-dimensional",
                  "Z30NxxxS.6", "Z30MxxxS.6", "Blum", "Order"]:
            with self.subTest(p=p):
                self.assertFalse(is_part(p), f"{p} should be rejected")


class RowHelpersTests(unittest.TestCase):
    def test_spec_on_row_picks_nearest_left_number(self):
        # (x0, y0, x1, y1, word)
        words = [
            (40, 100, 60, 110, "400"),
            (100, 100, 130, 110, "\u2000"),
            (128, 100, 170, 110, "578.4001M"),
            (40, 120, 60, 130, "450"),
            (128, 120, 170, 130, "578.4501M"),
        ]
        self.assertEqual(_spec_on_row(words, words[2]), "400")
        self.assertEqual(_spec_on_row(words, words[4]), "450")

    def test_spec_on_row_ignores_other_rows(self):
        words = [
            (40, 200, 60, 210, "400"),
            (128, 100, 170, 110, "ZIF.80M5"),
        ]
        self.assertEqual(_spec_on_row(words, words[1]), "")

    def test_drawer_type_standard_and_inner(self):
        self.assertEqual(_drawer_type("Drawer \u2013 N\nOrder information"), "Standard N")
        self.assertEqual(_drawer_type("Inner drawer \u2013 M\nOrder information"), "Inner M")
        self.assertEqual(_drawer_type("High fronted pull-out \u2013 gallery \u2013 C"), "High fronted C")

    def test_drawer_type_none_on_accessory_page(self):
        self.assertEqual(_drawer_type("Steel back\nOrder information"), "")

    def test_colour_detection(self):
        self.assertIn("SW", _colour("Colour: silk white | RAL 9006 grey"))
        self.assertIn("R9006", _colour("Colour: silk white | RAL 9006 grey"))
        self.assertEqual(_colour("Material: steel"), "")

    def test_component_name_fallback(self):
        self.assertEqual(_component_name("1 Cabinet profiles left/right", "1"), "Cabinet profiles")
        self.assertEqual(_component_name("Steel back order table", ""), "Steel back")


class ExtractPageTests(unittest.TestCase):
    class FakePage:
        def __init__(self, text, words, number=0):
            self._text = text
            self._words = words
            self.number = number

        def get_text(self, *a, **k):
            return self._text

        def get_text_words(self):
            return self._words

    def test_extract_page_with_global_seen(self):
        # patch get_text("words") via a page whose get_text returns words for that arg
        class P:
            number = 4
            def get_text(self, kind=None):
                if kind == "words":
                    return [
                        (45, 100, 50, 108, "1"),
                        (99, 100, 160, 108, "Cabinet"),
                        (40, 130, 60, 138, "400"),
                        (128, 130, 170, 138, "578.4001M"),
                    ]
                return "Drawer \u2013 N\nOrder information\nCabinet profiles"
        seen = set()
        rows = extract_page(P(), seen)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Part no."], "578.4001M")
        self.assertEqual(rows[0]["Specification"], "400")
        self.assertEqual(rows[0]["Drawer type"], "Standard N")
        self.assertEqual(rows[0]["Page"], 5)
        # second extraction with same seen set yields nothing (global dedupe)
        self.assertEqual(extract_page(P(), seen), [])

    def test_non_order_page_returns_empty(self):
        class P:
            number = 0
            def get_text(self, kind=None):
                return "Cover page" if kind != "words" else []
        self.assertEqual(extract_page(P(), set()), [])


if __name__ == "__main__":
    unittest.main()
