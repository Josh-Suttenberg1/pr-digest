from __future__ import annotations

import unittest

import swiftbar_plugin as menu
from triage import mine

ME = "me-login"


def st(num, **kw):
    base = {"pr_url": f"https://github.com/acme/api/pull/{num}", "title": f"ABC-{num}: thing",
            "author": "dev", "reason": "not reviewed yet", "tags": [], "tier": 1}
    return {**base, **kw}


def legacy(num, sources=("Review requested",), author="dev", draft=False, repo="api"):
    return {"url": f"https://github.com/acme/{repo}/pull/{num}", "title": f"PR {num}",
            "author": author, "sources": list(sources), "draft": draft}


def mine_row(num, waiting, draft=False):
    return {"url": f"https://github.com/acme/web/pull/{num}", "title": f"Mine {num}",
            "repo": "web", "number": num, "isDraft": draft, "waiting": waiting,
            "blockers": [], "threads_waiting": [], "unanswered_comments": []}


class BuildSectionsTest(unittest.TestCase):
    def data(self, **kw):
        base = {"me": ME, "reviews": {"needs_you": [], "author": [], "well_approved": [],
                                      "done": [], "unreadable": []},
                "mine": [], "legacy": []}
        base["reviews"].update(kw.pop("reviews", {}))
        return {**base, **kw}

    def test_parked_reviews_are_not_owed_and_not_counted(self):
        parked = st(2, tier=menu.PARKED_TIER, tags=["CI failing: build", "waiting 3d"])
        s = menu.build_sections(self.data(reviews={"needs_you": [st(1), parked]}), [])
        self.assertEqual([x["pr_url"] for x in s["owed"]], [st(1)["pr_url"]])
        self.assertEqual(s["count"], 1)
        reasons = [r for r, _ in s["other"]]
        self.assertEqual(reasons, ["parked — CI failing: build"])

    def test_count_is_actionable_followup_prs_plus_my_waiting_prs_plus_owed(self):
        def job(pr_id, status, at="2026-01-01T00:00:00Z"):
            return {"status": status, "pr": {"id": pr_id}, "created_at": at}
        jobs = [job("A", "ready"), job("A", "ready", "2026-01-03T00:00:00Z"),
                job("B", "running"), job("C", "failed"), job("D", "posted")]
        data = self.data(reviews={"needs_you": [st(1)]},
                         mine=[mine_row(5, True), mine_row(6, False)])
        s = menu.build_sections(data, jobs)
        groups = s["followups"]
        self.assertEqual([g["pr"]["id"] for g in groups], ["A", "B", "C"])  # one row per PR
        self.assertEqual(groups[0]["acted_at"], "2026-01-03T00:00:00Z")      # newest move
        self.assertEqual(s["count"], 2 + 1 + 1)   # A and C; B is still running

    def test_dismissed_pr_of_mine_hides_until_new_activity(self):
        row = mine_row(5, True)
        row["blockers"] = [{"author": "dev", "at": "2026-01-02T00:00:00Z"}]
        data = self.data(mine=[row])
        hidden = menu.build_sections(data, [], {row["url"]: "2026-01-02T00:00:00Z"})
        [listed] = hidden["mine"]                  # still listed under Your PRs ...
        self.assertTrue(listed["dismissed"])
        self.assertEqual(listed["state"], "changes")
        self.assertEqual(hidden["count"], 0)       # ... but no longer counted
        row["threads_waiting"] = [{"last_author": "dev", "last_at": "2026-01-05T00:00:00Z"}]
        back = menu.build_sections(data, [], {row["url"]: "2026-01-02T00:00:00Z"})
        self.assertEqual(back["mine"][0]["state"], "move")
        self.assertEqual(back["count"], 1)

    def test_every_search_hit_lands_somewhere_with_a_reason(self):
        data = self.data(
            reviews={"well_approved": [st(1, reason="2 approvals, no change requests")],
                     "author": [st(2, reason="you commented; no new commits")]},
            mine=[mine_row(9, False, draft=True)],
            legacy=[legacy(1), legacy(2), legacy(3, draft=True),
                    legacy(4, sources=("Assigned to you",)),
                    legacy(9, author=ME, repo="web"), legacy(7)])
        s = menu.build_sections(data, [])
        by_url = {item.get("pr_url"): reason for reason, item in s["other"]}
        self.assertEqual(by_url["https://github.com/acme/api/pull/1"],
                         "enough approvals — 2 approvals, no change requests")
        self.assertEqual(by_url["https://github.com/acme/api/pull/2"],
                         "waiting on the author — you commented; no new commits")
        self.assertEqual(by_url["https://github.com/acme/api/pull/3"],
                         "GitHub draft PR — not ready for review yet")
        self.assertEqual(by_url["https://github.com/acme/api/pull/4"],
                         "not on you — matched only: assigned to you")
        self.assertNotIn("https://github.com/acme/web/pull/9", by_url)   # under Your PRs
        self.assertEqual([r["url"] for r in s["mine_drafts"]],
                         ["https://github.com/acme/web/pull/9"])
        self.assertIn("check this", by_url["https://github.com/acme/api/pull/7"])
        self.assertEqual(len(s["other"]), 5)   # no PR listed twice
        self.assertEqual(s["count"], 0)

    def test_my_waiting_pr_is_not_repeated_in_everything_else(self):
        data = self.data(mine=[mine_row(9, True)], legacy=[legacy(9, author=ME, repo="web")])
        s = menu.build_sections(data, [])
        self.assertEqual(s["other"], [])
        self.assertEqual(s["count"], 1)


def gql_comment(login, at, typename="User", body="x"):
    return {"author": {"login": login, "__typename": typename}, "createdAt": at,
            "body": body, "path": "A.scala", "url": f"u/{login}/{at}"}


def gql_pr(threads=(), reviews=(), comments=(), decision=None):
    return {"isDraft": False, "reviewDecision": decision,
            "commits": {"nodes": []},
            "reviewThreads": {"nodes": [{"isResolved": False, "isOutdated": False,
                                         "comments": {"nodes": list(t)}} for t in threads]},
            "reviews": {"nodes": list(reviews)},
            "comments": {"nodes": list(comments)}}


class MineAnalyzeTest(unittest.TestCase):
    def test_bot_only_thread_is_not_waiting_on_me(self):
        pr = gql_pr(threads=[[gql_comment("copilot-pull-request-reviewer", "2026-01-01T00:00:00Z",
                                          typename="Bot")]])
        self.assertEqual(mine.analyze(pr, ME)["threads_waiting"], [])

    def test_named_bot_in_config_is_ignored(self):
        pr = gql_pr(comments=[gql_comment("avettabot", "2026-01-02T00:00:00Z")])
        self.assertEqual(mine.analyze(pr, ME, {"avettabot"})["unanswered_comments"], [])
        self.assertEqual(len(mine.analyze(pr, ME)["unanswered_comments"]), 1)

    def test_bot_reply_after_my_answer_does_not_reopen_a_thread(self):
        pr = gql_pr(threads=[[gql_comment("dev", "2026-01-01T00:00:00Z"),
                              gql_comment(ME, "2026-01-02T00:00:00Z"),
                              gql_comment("some-app[bot]", "2026-01-03T00:00:00Z")]])
        self.assertEqual(mine.analyze(pr, ME)["threads_waiting"], [])

    def test_human_thread_waits_on_me(self):
        pr = gql_pr(threads=[[gql_comment(ME, "2026-01-01T00:00:00Z"),
                              gql_comment("dev", "2026-01-02T00:00:00Z")]])
        waiting = mine.analyze(pr, ME)["threads_waiting"]
        self.assertEqual([t["last_author"] for t in waiting], ["dev"])

    def test_change_request_is_on_me_until_i_rerequest_the_reviewer(self):
        review = {"author": {"login": "rev", "__typename": "User"},
                  "state": "CHANGES_REQUESTED", "submittedAt": "2026-01-02T00:00:00Z",
                  "body": "", "url": "r/1"}
        pr = gql_pr(reviews=[review], decision="CHANGES_REQUESTED")
        pr["timelineItems"] = {"nodes": [
            {"createdAt": "2026-01-01T00:00:00Z", "requestedReviewer": {"login": "rev"}}]}
        before = mine.analyze(pr, ME)
        self.assertEqual([b["author"] for b in before["blockers"]], ["rev"])
        pr["timelineItems"]["nodes"].append(
            {"createdAt": "2026-01-03T00:00:00Z", "requestedReviewer": {"login": "rev"}})
        after = mine.analyze(pr, ME)
        self.assertEqual(after["blockers"], [])
        self.assertEqual(after["answered_change_requests"][0]["rerequested_at"],
                         "2026-01-03T00:00:00Z")

    def test_bot_change_request_is_not_a_blocker(self):
        review = {"author": {"login": "scanner", "__typename": "Bot"},
                  "state": "CHANGES_REQUESTED", "submittedAt": "2026-01-01T00:00:00Z",
                  "body": "", "url": "r/1"}
        pr = gql_pr(reviews=[review], decision="CHANGES_REQUESTED")
        self.assertEqual(mine.analyze(pr, ME)["blockers"], [])


class StatusReportTest(unittest.TestCase):
    def test_every_menu_pr_gets_a_file_and_stale_ones_go(self):
        import tempfile
        from pathlib import Path

        import status_report
        mine_clear = mine_row(9, False)
        mine_clear["answered_change_requests"] = [
            {"author": "rev", "at": "2026-01-02T00:00:00Z", "url": "r/1",
             "rerequested_at": "2026-01-03T00:00:00Z"}]
        data = {"me": ME, "reviews": {"needs_you": [st(1)], "author": [], "unreadable": [],
                                      "well_approved": [st(2, reason="2 approvals")],
                                      "done": []},
                "mine": [mine_clear], "legacy": []}
        sections = menu.build_sections(data, [])
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "gone#1.md").write_text("stale")
            status_report.write(sections, data, directory=d)
            names = sorted(p.name for p in d.glob("*.md"))
            self.assertEqual(names, ["api#1.md", "api#2.md", "index.md", "web#9.md"])
            web = (d / "web#9.md").read_text()
            self.assertIn("re-requested them at", web)
            self.assertIn("waiting on their re-review", web)
            self.assertIn("**Menu section:** Reviews you owe", (d / "api#1.md").read_text())
            self.assertIn("[api#2](api#2.md)", (d / "index.md").read_text())


class ReviewStatusTest(unittest.TestCase):
    def test_draft_is_current_only_for_the_live_head(self):
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            old_index, old_state = menu.REVIEW_INDEX, menu.REVIEW_STATE
            menu.REVIEW_INDEX = menu.REVIEW_STATE = Path(tmp)
            try:
                (Path(tmp) / "api#1.json").write_text(json.dumps(
                    {"head": "abc1234def", "severity": "clean", "counts": {"nit": 0}}))
                self.assertTrue(menu.review_status("api#1", "abc1234def")["draft"]["current"])
                self.assertTrue(menu.review_status("api#1", "abc1234")["draft"]["current"])
                self.assertFalse(menu.review_status("api#1", "fff0000")["draft"]["current"])
                self.assertFalse(menu.review_status("api#1", "")["draft"]["current"])
            finally:
                menu.REVIEW_INDEX, menu.REVIEW_STATE = old_index, old_state


if __name__ == "__main__":
    unittest.main()
