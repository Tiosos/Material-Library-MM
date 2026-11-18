"""Pure tests for the acceptance runner's result aggregation (no DB / HTTP needed)."""
import unittest

from scripts.verify_e2e import Results


class ResultsTests(unittest.TestCase):
    def test_all_pass(self):
        r = Results()
        r.check("a", lambda: "fine")
        r.check("b", lambda: "also fine")
        self.assertTrue(r.ok)
        self.assertEqual(len(r.rows), 2)
        self.assertTrue(all(p for _, p, _ in r.rows))
        self.assertIn("2/2 checks passed", r.report())

    def test_failure_captured_and_all_checks_still_run(self):
        r = Results()

        def boom():
            raise ValueError("bad")

        r.check("first", boom)
        r.check("second", lambda: "ok")
        self.assertFalse(r.ok)
        self.assertEqual(len(r.rows), 2)  # second still ran
        name, passed, detail = r.rows[0]
        self.assertEqual(name, "first")
        self.assertFalse(passed)
        self.assertIn("ValueError: bad", detail)
        self.assertIn("1/2 checks passed", r.report())

    def test_check_returning_none_is_ok(self):
        r = Results()
        r.check("no detail", lambda: None)
        self.assertTrue(r.ok)
        self.assertEqual(r.rows[0][2], "ok")

    def test_report_lists_every_check(self):
        r = Results()
        r.check("alpha", lambda: "x")
        r.check("beta", lambda: "y")
        report = r.report()
        self.assertIn("[PASS] alpha", report)
        self.assertIn("[PASS] beta", report)


if __name__ == "__main__":
    unittest.main()
