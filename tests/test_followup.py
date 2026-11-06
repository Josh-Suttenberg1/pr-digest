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


ME, AUTHOR, HEAD = "me", "dev", "h2"


def comment(cid, author, body="Rename this", review_state="COMMENTED", bot=False):
    return {"id": cid, "author": author, "body": body, "review_state": review_state,
            "is_bot": bot}


def thread(tid, *comments, resolved=False):
    return {"id": tid, "resolved": resolved, "comments": list(comments)}


def review(author, state, at, commit="h1"):
    return {"author": author, "state": state, "submitted_at": at, "commit": commit,
            "is_bot": False}


def snap(threads=(), reviews=(), requests=(), head=HEAD, state="OPEN"):
    return {"id": "PR1", "author": AUTHOR, "head": head, "state": state,
            "threads": list(threads),
            "reviews": [review(ME, "APPROVED", "2026-01-01T00:00:00Z"), *reviews],
            "review_requests": list(requests)}


def job(jid, tid, arm, status="ready", verdict="addressed", mechanical=True):
    return {"id": jid, "thread_id": tid, "arm_id": arm, "trigger": "reply", "head": HEAD,
            "status": status, "verdict": verdict, "mechanical": mechanical,
            "pr": {"id": "PR1"}}


class ReapprovalBlockersTests(unittest.TestCase):
    def blockers(self, s, jobs, current=None):
        current = current or jobs[0]
        return followup.reapproval_blockers(s, current, jobs, ME)

    def test_clean_when_every_comment_of_yours_is_addressed(self):
        s = snap([thread("T1", comment(1, ME), comment(2, AUTHOR)),
                  thread("T2", comment(3, ME))])
        self.assertEqual(self.blockers(s, [job("a", "T1", 1), job("b", "T2", 3)]), [])

    def test_one_fixed_comment_is_not_enough(self):
        s = snap([thread("T1", comment(1, ME), comment(2, AUTHOR)),
                  thread("T2", comment(3, ME))])
        self.assertEqual(self.blockers(s, [job("a", "T1", 1)]),
                         ["1 of your comments isn't addressed yet"])

    def test_a_comment_still_being_checked_or_not_done_as_asked_holds(self):
        s = snap([thread("T1", comment(1, ME)), thread("T2", comment(3, ME)),
                  thread("T3", comment(5, ME))])
        jobs = [job("a", "T1", 1), job("b", "T2", 3, status="running"),
                job("c", "T3", 5, mechanical=False)]
        self.assertEqual(self.blockers(s, jobs), ["2 of your comments aren't addressed yet"])

    def test_a_newer_comment_of_yours_needs_its_own_check(self):
        s = snap([thread("T1", comment(1, ME), comment(2, AUTHOR), comment(4, ME))])
        self.assertEqual(self.blockers(s, [job("a", "T1", 1)]),
                         ["1 of your comments isn't addressed yet"])

    def test_resolved_threads_of_yours_dont_count(self):
        s = snap([thread("T1", comment(1, ME)), thread("T2", comment(3, ME), resolved=True)])
        self.assertEqual(self.blockers(s, [job("a", "T1", 1)]), [])

    def test_another_reviewers_unanswered_change_request_holds(self):
        s = snap([thread("T1", comment(1, ME))],
                 reviews=[review("rev", "CHANGES_REQUESTED", "2026-01-02T00:00:00Z")])
        self.assertEqual(self.blockers(s, [job("a", "T1", 1)]),
                         ["rev's change request is still unanswered"])

    def test_a_change_request_rerequested_since_is_answered(self):
        s = snap([thread("T1", comment(1, ME))],
                 reviews=[review("rev", "CHANGES_REQUESTED", "2026-01-02T00:00:00Z")],
                 requests=[{"reviewer": "rev", "at": "2026-01-03T00:00:00Z"}])
        self.assertEqual(self.blockers(s, [job("a", "T1", 1)]), [])

    def test_a_change_request_later_replaced_by_an_approval_is_answered(self):
        s = snap([thread("T1", comment(1, ME))],
                 reviews=[review("rev", "CHANGES_REQUESTED", "2026-01-02T00:00:00Z"),
                          review("rev", "APPROVED", "2026-01-03T00:00:00Z")])
        self.assertEqual(self.blockers(s, [job("a", "T1", 1)]), [])

    def test_another_reviewers_unanswered_blocking_comment_holds(self):
        mine = thread("T1", comment(1, ME))
        tagged = thread("T2", comment(3, "rev", body="**Blocking:** this drops rows"))
        in_change_request = thread("T3", comment(5, "rev2", review_state="CHANGES_REQUESTED"))
        s = snap([mine, tagged, in_change_request])
        self.assertEqual(self.blockers(s, [job("a", "T1", 1)]),
                         ["rev and rev2's blocking comments are still unanswered"])

    def test_answered_or_non_blocking_comments_from_others_dont_hold(self):
        s = snap([thread("T1", comment(1, ME)),
                  thread("T2", comment(3, "rev", body="**Blocking:** x"), comment(4, AUTHOR)),
                  thread("T3", comment(5, "rev", body="Nit: spacing")),
                  thread("T4", comment(6, "rev", body="Nit: y", review_state="CHANGES_REQUESTED")),
                  thread("T5", comment(7, "bot", body="**Blocking:** z", bot=True))])
        self.assertEqual(self.blockers(s, [job("a", "T1", 1)]), [])

    def test_your_latest_decision_must_be_an_approval_on_an_older_commit(self):
        s = snap([thread("T1", comment(1, ME))],
                 reviews=[review(ME, "CHANGES_REQUESTED", "2026-01-02T00:00:00Z")])
        self.assertEqual(self.blockers(s, [job("a", "T1", 1)]),
                         ["your latest decision on the PR isn't an approval"])
        s = snap([thread("T1", comment(1, ME))],
                 reviews=[review(ME, "APPROVED", "2026-01-02T00:00:00Z", commit=HEAD)])
        self.assertEqual(self.blockers(s, [job("a", "T1", 1)]),
                         ["you already approved the current head"])

    def test_a_moved_head_holds(self):
        s = snap([thread("T1", comment(1, ME))], head="h3")
        self.assertEqual(self.blockers(s, [job("a", "T1", 1)]),
                         ["the PR moved or closed after the follow-up ran"])

    def test_off_unless_turned_on(self):
        cfg = {"watcher": {}, "me": ME}
        shas, why = followup.reapproval_plan(job("a", "T1", 1) | {"actor": AUTHOR,
                                             "pr": {"id": "PR1", "author": AUTHOR}},
                                             {"verdict": "addressed", "mechanical": True,
                                              "evidence": []}, cfg, None)
        self.assertEqual((shas, why), (None, "auto re-approval is off"))


class RecheckTests(unittest.TestCase):
    def held(self, **kw):
        j = job("a", "T1", 1) | {"actor": AUTHOR, "arm_commit": "h0", "finished_at": 5,
                                 "reapproval": "rev's change request is still unanswered",
                                 "pr": {"id": "PR1", "author": AUTHOR, "repo": "ui"}}
        return j | kw

    def test_picks_the_latest_held_job_on_the_current_head(self):
        old = self.held(id="old", finished_at=1)
        new = self.held(id="new", finished_at=9)
        moved = self.held(id="moved", finished_at=20, head="h1")
        self.assertEqual(followup.held_reapproval(snap(), [old, new, moved])["id"], "new")

    def test_nothing_to_recheck_when_reapproval_was_never_in_play(self):
        for why in ("auto re-approval is off", "your latest decision on the PR isn't an approval"):
            self.assertIsNone(followup.held_reapproval(snap(), [self.held(reapproval=why)]))
        self.assertIsNone(followup.held_reapproval(snap(), [self.held(mechanical=False)]))

    def test_goes_through_once_the_other_reviewer_is_answered(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            report = Path(d) / "r.md"
            report.with_suffix(".json").write_text(json.dumps(
                {"verdict": "addressed", "mechanical": True, "evidence": [{"sha": "f1"}]}))
            j = self.held(report=str(report))
            cfg = {"me": ME, "repos_root": d, "watcher": {"auto_reapprove": True}}
            standing = snap([thread("T1", comment(1, ME))],
                            reviews=[review("rev", "CHANGES_REQUESTED", "2026-01-02T00:00:00Z")])
            answered = snap([thread("T1", comment(1, ME))],
                            reviews=[review("rev", "CHANGES_REQUESTED", "2026-01-02T00:00:00Z"),
                                     review("rev", "APPROVED", "2026-01-03T00:00:00Z")])
            git = mock.Mock(return_value=mock.Mock(returncode=0, stdout="f" * 40 + "\n"))
            with mock.patch.object(followup, "_git", git):
                self.assertEqual(followup.recheck_reapprovals([standing], [j], cfg, dry_run=True),
                                 [("a", "rev's change request is still unanswered")])
                self.assertEqual(followup.recheck_reapprovals([answered], [j], cfg, dry_run=True),
                                 [("a", "would re-approve")])

    def test_off_means_no_recheck(self):
        cfg = {"me": ME, "watcher": {"auto_reapprove": False}}
        self.assertEqual(followup.recheck_reapprovals([snap()], [self.held()], cfg, True), [])


if __name__ == "__main__":
    unittest.main()


class ClosedPrTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        self.tmp = tempfile.TemporaryDirectory()
        patch = mock.patch.object(followup, "JOBS_DIR", Path(self.tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def job(self, jid, status, pr="PR1"):
        followup.save_job({"id": jid, "status": status, "pr": {"id": pr}})

    def test_a_merged_pr_clears_every_open_job_not_just_queued_ones(self):
        for jid, status in (("q", "queued"), ("r", "ready"), ("f", "failed"),
                            ("p", "posted"), ("run", "running")):
            self.job(jid, status)
        self.job("other", "ready", pr="PR2")
        self.assertEqual(sorted(followup.cancel_for_pr("PR1", "MERGED")), ["f", "q", "r"])
        jobs = {j["id"]: j for j in followup.all_jobs()}
        self.assertEqual((jobs["r"]["status"], jobs["r"]["cleared"]), ("cleared", "the PR merged"))
        self.assertEqual([jobs[k]["status"] for k in ("p", "run", "other")],
                         ["posted", "running", "ready"])

    def test_open_jobs_on_prs_the_search_no_longer_returns_are_orphans(self):
        jobs = [{"id": "a", "status": "ready", "pr": {"id": "PR1"}},
                {"id": "b", "status": "posted", "pr": {"id": "PR2"}},
                {"id": "c", "status": "failed", "pr": {"id": "PR3"}},
                {"id": "d", "status": "running", "pr": {"id": "PR4"}}]
        self.assertEqual(followup.orphaned_prs(jobs, {"PR3": {}}), {"PR1"})
