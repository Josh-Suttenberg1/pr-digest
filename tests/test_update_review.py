from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import settings
from review import record
from review import review_pr
from review import update_review as U

OLD, NEW = "1" * 40, "2" * 40

DRAFT = """Reviewer: pr-reviewer · coworker review · 2026-11-02

## ABC-9 (thing) · avetta/api#7 · @dev · head 1111111111111111111111111111111111111111
**TL;DR:** 🔴 1 blocking.

### Actions required
1. 🔴 `svc/Service.scala:10` — Guard misses Count(). Breaks a calc. *Fix:* include it.
2. 🟡 `other/Thing.scala:5`: advisory, a name. *Fix:* rename.
3. 🔵 `svc/Service.scala:30` — nit: spacing.
4. ⚠️ — Should this ship behind a flag?

SEVERITY: substantive
"""

DELTA = {"files": {"src/main/svc/Service.scala": "@@ -8,4 +8,6 @@"},
         "ranges": {"src/main/svc/Service.scala": [(8, 13)]}, "lines": 6, "pr_lines": 120,
         "commits": 2, "method": "replay", "diff": "diff --git a/x b/x\n+fixed\n",
         "truncated": False, "new_files": []}


def thread(tid, author, path, line, body, outdated=False, resolved=False, first_id=None):
    return {"id": f"T{tid}", "resolved": resolved, "outdated": outdated, "path": path,
            "line": line, "original_line": line,
            "comments": [{"id": first_id or tid, "author": author, "is_bot": False, "body": body,
                          "url": f"https://x/{tid}"}]}


class RecordTest(unittest.TestCase):
    def test_parse_findings_reads_both_separators_and_shortened_paths(self):
        found = record.parse_findings(DRAFT)
        self.assertEqual([(f["id"], f["severity"], f["path"], f["line"]) for f in found], [
            ("f1", "blocking", "svc/Service.scala", 10),
            ("f2", "advisory", "other/Thing.scala", 5),
            ("f3", "nit", "svc/Service.scala", 30),
            ("f4", "decision", None, None)])

    def test_update_sections_are_not_findings_of_the_review(self):
        text = (f"{record.UPDATE_START}\n## Update\n1. 🔴 `a/b.py:1` — new\n{record.UPDATE_END}\n\n"
                + DRAFT)
        self.assertEqual(len(record.parse_findings(text)), 4)

    def test_counts_and_severity_follow_open_findings(self):
        fs = record.parse_findings(DRAFT)
        fs[0]["status"] = "addressed"
        fs[1]["status"] = "partly"
        c = record.counts(fs)
        self.assertEqual(c, {"blocking": 0, "advisory": 1, "nit": 1, "decision": 1})
        self.assertEqual(record.severity({"blocking": 0, "advisory": 0, "nit": 1, "decision": 0}),
                         "nits-only")

    def test_write_drafts_keeps_findings_and_history(self):
        with tempfile.TemporaryDirectory() as d:
            idx = Path(d) / "index"
            with mock.patch.object(review_pr, "REVIEW_INDEX_DIR", idx):
                review_pr.write_drafts(DRAFT, {"target_key": None, "cost_usd": 3.0}, Path(d))
                [rec] = review_pr.write_drafts(DRAFT, {"target_key": None, "cost_usd": 2.0}, Path(d))
            self.assertEqual(len(rec["findings"]), 4)
            self.assertEqual([h["cost_usd"] for h in rec["history"]], [3.0, 2.0])


class SelectionTest(unittest.TestCase):
    def test_findings_in_touched_files_or_without_a_file_are_judged(self):
        judge, untouched = U.to_judge(record.parse_findings(DRAFT), DELTA)
        self.assertEqual([f["id"] for f in judge], ["f1", "f3", "f4"])
        self.assertEqual([f["id"] for f in untouched], ["f2"])

    def test_comments_on_changed_lines_by_any_reviewer(self):
        threads = [
            thread(1, "mykal", "src/main/svc/Service.scala", 12, "use a set"),       # in a hunk
            thread(2, "woods", "src/main/svc/Service.scala", 15, "near it"),          # within NEAR
            thread(3, "woods", "src/main/svc/Service.scala", 90, "far away"),
            thread(4, "woods", "src/main/svc/Service.scala", None, "gone", outdated=True),
            thread(5, "dev", "src/main/svc/Service.scala", 12, "author note"),
            thread(6, "woods", "src/main/svc/Service.scala", 12, "done", resolved=True),
            thread(7, "woods", "other/Thing.scala", 5, "other file"),
        ]
        got = U.comments_on_changes(threads, DELTA, "dev")
        self.assertEqual([c["id"] for c in got], [1, 2, 4])


class ParseTest(unittest.TestCase):
    def test_keeps_known_entries_only(self):
        text = json.dumps({
            "findings": [{"id": "f1", "status": "addressed", "sha": "abc", "note": "fixed"},
                         {"id": "f9", "status": "addressed"},
                         {"id": "f3", "status": "maybe"}],
            "comments": [{"id": 1, "verdict": "addressed", "mechanical": True, "evidence": []},
                         {"id": 2, "verdict": "kinda"}],
            "new_findings": [{"severity": "advisory", "path": "a.py", "line": "4", "text": "x. *Fix:* y."},
                             {"severity": "huge", "text": "z"}]})
        r = U.parse_result("noise " + text, ["f1", "f3"], [1, 2])
        self.assertEqual(list(r["findings"]), ["f1"])
        self.assertEqual(list(r["comments"]), [1])
        self.assertEqual(r["new_findings"][0]["line"], 4)
        self.assertEqual(len(r["new_findings"]), 1)

    def test_not_json(self):
        with self.assertRaises(ValueError):
            U.parse_result("no", [], [])


class SummaryTest(unittest.TestCase):
    def test_reads_like_the_design(self):
        comments = [{"id": 1, "author": "mykal"}]
        line = U.summary_line(OLD, 3, {"f1": {"status": "addressed"}, "f2": {"status": "addressed"}},
                              1, comments, {1: {"verdict": "addressed"}}, 1, {"mykal": "Mykal"})
        self.assertEqual(line, "Since 1111111, 3 commits: 2 findings addressed, 1 still open; "
                               "Mykal's comment addressed; 1 new issue.")

    def test_one_reviewers_mixed_verdicts_read_as_one_phrase(self):
        comments = [{"id": 1, "author": "dk"}, {"id": 2, "author": "dk"}]
        line = U.summary_line(OLD, 1, {}, 0, comments,
                              {1: {"verdict": "partly"}, 2: {"verdict": "not_addressed"}}, 0,
                              {"dk": "Daniel Knight"})
        self.assertEqual(line, "Since 1111111, 1 commit: Daniel Knight's 2 comments: "
                               "1 partly addressed, 1 not addressed; no new issues.")

    def test_cut_closes_what_it_leaves_open(self):
        self.assertEqual(U._cut("word `abc def ghi` jkl", 12), "word `abc`…")
        self.assertEqual(U._cut("**bold words here** tail", 12), "**bold**…")
        self.assertEqual(U._cut("short"), "short")

    def test_force_push_and_nothing_new(self):
        line = U.summary_line(OLD, None, {}, 0, [], {}, 0, {})
        self.assertEqual(line, "Since 1111111, a force push: no new issues.")

    def test_surviving_takes_the_verified_actions(self):
        verified = ("Reviewer: x\n\n## ABC-9 · avetta/api#7 · head 2222\n### Actions required\n"
                    "1. 🟡 `a.py:4` — x.\n   - detail\n\n<details><summary>Evidence</summary>e</details>\n"
                    "<details><summary>Verification</summary>\n| 1 | x | CONFIRMED | y |\n</details>\n")
        block, details = U.surviving(verified)
        self.assertEqual(block, "1. 🟡 `a.py:4` — x.\n   - detail")
        self.assertTrue(details.startswith("<details><summary>Verification"))


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.draft = root / "ABC-9-api.md"
        self.draft.write_text(DRAFT)
        self.rec = {"key": "ABC-9", "repo": "api", "number": 7, "head": OLD, "draft": str(self.draft),
                    "findings": record.parse_findings(DRAFT), "history": [{"kind": "full", "cost_usd": 3}]}
        self.cfg = settings._merge(settings.DEFAULTS, {"me": "me", "workspace": str(root)})
        self.index = root / "index"
        self.runs = mock.patch.object(review_pr, "RUNS_DIR", root / "runs")
        self.runs.start()

    def tearDown(self):
        self.runs.stop()
        self.tmp.cleanup()

    def run_update(self, answer: dict, verified: str | None = None):
        prompts, verified_paths = [], []

        def claude(prompt):
            prompts.append(prompt)
            return {"result": json.dumps(answer), "total_cost_usd": 0.5}

        def verify(path):
            verified_paths.append(Path(path).read_text())
            return {"result": verified, "total_cost_usd": 0.25}
        threads = [thread(1, "mykal", "src/main/svc/Service.scala", 12, "use a set")]
        pr = {"repo": "api", "number": 7, "title": "ABC-9: thing", "author": "dev",
              "url": "https://github.com/acme/api/pull/7"}
        out = U.run(self.rec, pr, DELTA, OLD, NEW, Path("/clone"), self.cfg, threads,
                    {"mykal": "Mykal"}, claude=claude, verify=verify, index_dir=self.index)
        return out, prompts, verified_paths

    def test_an_update_goes_on_top_and_moves_the_record(self):
        answer = {"findings": [{"id": "f1", "status": "addressed", "sha": "abcdef1", "note": "guards Count()"},
                               {"id": "f3", "status": "not", "note": ""},
                               {"id": "f4", "status": "partly", "note": "flag added, default on"}],
                  "comments": [{"id": 1, "verdict": "addressed", "mechanical": True, "evidence": [],
                                "note": "now a set"}],
                  "new_findings": [{"severity": "advisory", "path": "src/main/svc/Service.scala",
                                    "line": 12, "text": "Set loses order. Output reorders. *Fix:* use a ListSet.",
                                    "evidence": "Service.scala:12"}]}
        verified = ("Reviewer: pr-reviewer\n\n## ABC-9 () · avetta/api#7 · @dev · head " + NEW +
                    "\n### Actions required\n1. 🟡 `src/main/svc/Service.scala:12` — Set loses order. "
                    "*Fix:* use a ListSet.\n\n<details><summary>Verification</summary>\n"
                    "| 1 | order | CONFIRMED | ok |\n</details>\n")
        out, prompts, verified_paths = self.run_update(answer, verified)
        [prompt] = prompts
        self.assertIn("id f1:", prompt)
        self.assertNotIn("id f2:", prompt)                 # other/Thing.scala is untouched
        self.assertIn("id 1: @mykal", prompt)
        self.assertIn("Set loses order", verified_paths[0])
        text = self.draft.read_text()
        self.assertTrue(text.startswith(record.UPDATE_START))
        self.assertIn("Since 1111111, 2 commits: 1 finding addressed, 3 still open; "
                      "Mykal's comment addressed; 1 new issue.", text)
        self.assertIn("**still open** (the changes don't touch `other/Thing.scala`)", text)
        self.assertIn("Verification of the new issues", text)
        self.assertTrue(text.rstrip().endswith("SEVERITY: substantive"))
        self.assertEqual(out["head"], NEW)
        self.assertEqual({f["id"]: f["status"] for f in out["findings"]},
                         {"f1": "addressed", "f2": "open", "f3": "not", "f4": "partly",
                          "u2222222-1": "open"})
        self.assertEqual(out["counts"], {"blocking": 0, "advisory": 2, "nit": 1, "decision": 1})
        self.assertEqual(out["history"][-1]["kind"], "update")
        self.assertEqual(out["history"][-1]["cost_usd"], 0.75)
        self.assertEqual(out["comment_verdicts"][0]["verdict"], "addressed")
        self.assertEqual(record.load("api", 7, self.index)["head"], NEW)
        self.assertEqual(len(record.parse_findings(text)), 4)

    def test_nit_only_new_findings_skip_the_verifier(self):
        answer = {"findings": [], "comments": [],
                  "new_findings": [{"severity": "nit", "path": "a.py", "line": 1, "text": "spacing."}]}
        out, _, verified_paths = self.run_update(answer)
        self.assertEqual(verified_paths, [])
        self.assertIn("1. 🔵 `a.py:1` — spacing.", self.draft.read_text())
        self.assertEqual(out["history"][-1]["cost_usd"], 0.5)

    def test_an_unusable_answer_fails_without_touching_the_draft(self):
        with self.assertRaises(U.UpdateError):
            U.run(self.rec, {"repo": "api", "number": 7}, DELTA, OLD, NEW, Path("/c"), self.cfg, [],
                  {}, claude=lambda p: {"result": "sorry"}, index_dir=self.index)
        self.assertEqual(self.draft.read_text(), DRAFT)


if __name__ == "__main__":
    unittest.main()
