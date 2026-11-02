from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from review import review_pr

SINGLE = """Reviewer: pr-reviewer · coworker review · 2026-11-02

## ABC-1555 (delete step safeguard) · avetta/processservice#828 · @m0n01d · head 0123456789abcdef0123456789abcdef01234567
**TL;DR:** 🔴 1 blocking — guard the Count() lookup.

### Actions required
1. 🔴 `src/A.scala:10` — Guard misses Count() steps. Deleting one breaks a calculation. *Fix:* include Count().
2. 🔵 `src/A.scala:20` — nit: rename x. Reads poorly. *Fix:* call it stepIds.

SEVERITY: substantive
"""

STORY = """Reviewer: pr-reviewer · coworker review · 2026-11-02

Rollup: land process-domain#346 before processservice#835.

## ABC-1737 (creator auto-assign) · avetta/process-domain#346 · @aricWL · head `abcdef1`
**TL;DR:** 🔵 nits only.
1. 🔵 `a.scala:1` — nit: naming. Minor. *Fix:* rename.

## ABC-1737 (creator auto-assign) · avetta/processservice#835 · @aricWL · head 1234abc
**TL;DR:** 🟢 Ready — no changes required.

<details><summary>Verification</summary>
| 1 | x | CONFIRMED | y |
</details>

SEVERITY: nits-only
"""


class SeverityTests(unittest.TestCase):
    def test_reads_severity_line(self):
        self.assertEqual(review_pr.severity_of(SINGLE), "substantive")

    def test_derives_when_line_missing(self):
        self.assertEqual(review_pr.severity_of("1. 🔵 `a:1` — nit: x."), "nits-only")
        self.assertEqual(review_pr.severity_of("1. 🟡 `a:1` — x."), "substantive")
        self.assertEqual(review_pr.severity_of("**TL;DR:** 🟢 Ready"), "clean")

    def test_counts(self):
        counts = review_pr.count_findings(SINGLE)
        self.assertEqual(counts, {"blocking": 1, "advisory": 0, "nit": 1, "decision": 0})
        self.assertEqual(review_pr.summary_line(counts), "1 blocking · 1 nit")


class PreambleTests(unittest.TestCase):
    def test_strips_text_before_reviewer_line(self):
        self.assertEqual(review_pr.strip_preamble("I checked it all.\n\n" + SINGLE), SINGLE)

    def test_leaves_text_without_reviewer_line(self):
        self.assertEqual(review_pr.strip_preamble("no stamp"), "no stamp")


class SplitTests(unittest.TestCase):
    def test_single_pr(self):
        [block] = review_pr.split_pr_blocks(SINGLE)
        self.assertEqual((block["key"], block["repo"], block["number"]), ("ABC-1555", "processservice", 828))
        self.assertEqual(block["head"], "0123456789abcdef0123456789abcdef01234567")
        self.assertNotIn("SEVERITY:", block["body"])

    def test_story_blocks_strip_verification(self):
        blocks = review_pr.split_pr_blocks(STORY)
        self.assertEqual([(b["repo"], b["number"]) for b in blocks],
                         [("process-domain", 346), ("processservice", 835)])
        self.assertEqual(blocks[0]["head"], "abcdef1")
        self.assertNotIn("Verification", blocks[1]["body"])

    def test_no_header_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(review_pr.ReviewError):
                review_pr.write_drafts("no header here", {"target_key": None}, Path(d))


class WriteDraftTests(unittest.TestCase):
    def test_single_pr_writes_draft_sidecar_and_index(self):
        with tempfile.TemporaryDirectory() as d:
            drafts, index = Path(d) / "drafts", Path(d) / "index"
            with mock.patch.object(review_pr, "REVIEW_INDEX_DIR", index):
                [rec] = review_pr.write_drafts(SINGLE, {"target_key": None}, drafts)
            self.assertEqual(rec["severity"], "substantive")
            self.assertEqual((drafts / "ABC-1555-processservice.md").read_text(), SINGLE)
            sidecar = json.loads((drafts / "ABC-1555-processservice.json").read_text())
            self.assertEqual(sidecar["head"], "0123456789abcdef0123456789abcdef01234567")
            self.assertTrue((index / "processservice#828.json").exists())

    def test_story_writes_story_file_and_per_pr_drafts(self):
        with tempfile.TemporaryDirectory() as d:
            drafts, index = Path(d) / "drafts", Path(d) / "index"
            with mock.patch.object(review_pr, "REVIEW_INDEX_DIR", index):
                recs = review_pr.write_drafts(STORY, {"target_key": "ABC-1737"}, drafts)
            self.assertTrue((drafts / "ABC-1737-story.md").exists())
            self.assertEqual([r["severity"] for r in recs], ["nits-only", "clean"])
            self.assertIn("Part of the story review", (drafts / "ABC-1737-processservice.md").read_text())


if __name__ == "__main__":
    unittest.main()
