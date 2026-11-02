from __future__ import annotations

import unittest

from watcher import followup


class ParseResultTests(unittest.TestCase):
    def test_mechanical_must_be_literally_true(self):
        r = followup.parse_result('{"verdict": "addressed", "mechanical": "yes", "reply": "ok"}')
        self.assertFalse(r["mechanical"])
        r = followup.parse_result('```json\n{"verdict": "addressed", "mechanical": true, "reply": "ok"}\n```')
        self.assertTrue(r["mechanical"])

    def test_rejects_bad_verdict_and_empty_reply(self):
        with self.assertRaises(ValueError):
            followup.parse_result('{"verdict": "maybe", "reply": "x"}')
        with self.assertRaises(ValueError):
            followup.parse_result('{"verdict": "addressed", "reply": "  "}')
        with self.assertRaises(ValueError):
            followup.parse_result("no json")


class ReapprovalBodyTests(unittest.TestCase):
    def test_one_commit(self):
        sha = "1ff4241ef1d618e8daaa6d973e5937eaa936a36c"
        self.assertEqual(
            followup.reapproval_body("feedservice", [sha]),
            "Reapproving, the author's commit "
            f"[1ff4241](https://github.com/avetta/feedservice/commit/{sha}) addresses my comment."
            "\n\nDrafted with Claude 🤖",
        )

    def test_several_commits(self):
        body = followup.reapproval_body("ui", ["a" * 40, "b" * 40, "c" * 40])
        self.assertIn("commits [aaaaaaa]", body)
        self.assertIn(", [bbbbbbb]", body)
        self.assertIn(" and [ccccccc]", body)
        self.assertIn("address my comment.", body)


class FooterTests(unittest.TestCase):
    def test_footer_added_once(self):
        once = followup.with_footer("Done.")
        self.assertEqual(followup.with_footer(once), once)


if __name__ == "__main__":
    unittest.main()
