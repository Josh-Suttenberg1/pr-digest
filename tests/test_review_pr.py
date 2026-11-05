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


VERIFIED = SINGLE.replace("SEVERITY: substantive", "") + """
<details><summary>Verification</summary>

| # | Claim (short) | Verdict | Evidence |
|---|---|---|---|
| 1 | Guard misses Count() steps | CONFIRMED | traced |
| 2 | Deleting breaks a calculation via X | REFUTED — corrected to Y | X passes isActive=true |
| 3 | 11 rows affected | **UNVERIFIABLE** | no VPN |
</details>

SEVERITY: substantive
"""


class VerifierStatsTests(unittest.TestCase):
    def test_not_run(self):
        self.assertEqual(review_pr.verifier_stats(SINGLE, SINGLE, None), {"ran": False})

    def test_counts_verdicts_and_findings(self):
        after = VERIFIED.replace("2. 🔵 `src/A.scala:20` — nit: rename x. Reads poorly. *Fix:* call it stepIds.\n", "")
        stats = review_pr.verifier_stats(SINGLE, after, {"total_cost_usd": 1.234, "duration_ms": 61000})
        self.assertEqual((stats["confirmed"], stats["refuted"], stats["unverifiable"]), (1, 1, 1))
        self.assertEqual((stats["findings_before"]["nit"], stats["findings_after"]["nit"]), (1, 0))
        self.assertTrue(stats["changed_review"])
        self.assertEqual((stats["cost_usd"], stats["duration_s"]), (1.23, 61))

    def test_all_confirmed_and_unchanged_is_not_a_change(self):
        text = SINGLE + "\n| 1 | claim | CONFIRMED | e |\n"
        stats = review_pr.verifier_stats(SINGLE, text, {"total_cost_usd": 0.5})
        self.assertFalse(stats["changed_review"])


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


class InSessionReviewerTests(unittest.TestCase):
    """--reviewer-output: the session wrote the report, so only the verifier runs."""

    def _run(self, report: str, verifier_result: str | None = None, verifier: str = "pr-review-verifier"):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            report_path = d / "report.md"
            report_path.write_text("Some preamble.\n\n" + report)
            cfg = {
                "workspace": str(d),
                "review": {
                    "reviewer_agent": "pr-reviewer", "verifier_agent": verifier,
                    "timeout": 60, "max_concurrent": 1, "drafts_dir": str(d / "drafts"),
                },
            }
            envelope = {"result": verifier_result, "total_cost_usd": 1.5, "duration_ms": 1000}
            with mock.patch.object(review_pr, "RUNS_DIR", d / "runs"), \
                    mock.patch.object(review_pr, "REVIEW_INDEX_DIR", d / "index"), \
                    mock.patch.object(review_pr, "acquire_slot", return_value=None), \
                    mock.patch.object(review_pr, "claude_logged_in", return_value=True) as login, \
                    mock.patch.object(review_pr, "run_agent", return_value=envelope) as run_agent:
                [rec] = review_pr.review("ABC-1555", None, None, False, cfg, reviewer_output=report_path)
                draft = Path(rec["draft"]).read_text()
            return rec, draft, run_agent, login

    def test_substantive_report_runs_only_the_verifier(self):
        rec, draft, run_agent, _ = self._run(SINGLE, verifier_result=VERIFIED)
        [call] = run_agent.call_args_list
        self.assertEqual(call.args[0], "pr-review-verifier")
        self.assertEqual(rec["reviewer"], "in-session")
        self.assertIsNone(rec["reviewer_cost_usd"])
        self.assertTrue(rec["verified"])
        self.assertIn("Verification", draft)

    def test_empty_verifier_skips_it(self):
        rec, _, run_agent, _ = self._run(SINGLE, verifier="")
        run_agent.assert_not_called()
        self.assertFalse(rec["verified"])

    def test_empty_reviewer_turns_reviews_off(self):
        cfg = {"workspace": "/tmp", "review": {"reviewer_agent": "", "verifier_agent": ""}}
        with self.assertRaises(review_pr.ReviewError) as e:
            review_pr.review("ABC-1", None, None, False, cfg)
        self.assertIn("reviews are off", str(e.exception))

    def test_nits_only_report_runs_no_agent(self):
        nits = SINGLE.replace("1. 🔴", "1. 🔵").replace("SEVERITY: substantive", "SEVERITY: nits-only")
        rec, draft, run_agent, login = self._run(nits)
        run_agent.assert_not_called()
        login.assert_not_called()
        self.assertFalse(rec["verified"])
        self.assertTrue(draft.startswith("Reviewer:"))

    def test_report_without_pr_header_is_rejected(self):
        with self.assertRaises(review_pr.ReviewError) as ctx:
            self._run("Reviewer: pr-reviewer\n\nno header here\n")
        self.assertEqual(ctx.exception.code, review_pr.EXIT_OUTPUT)


if __name__ == "__main__":
    unittest.main()
