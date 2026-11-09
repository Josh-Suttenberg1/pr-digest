from __future__ import annotations

import unittest
import unittest.mock

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
            "blockers": [], "threads_waiting": [], "unanswered_comments": [],
            "approvals": [], "requested": [], "answered_change_requests": []}


class BuildSectionsTest(unittest.TestCase):
    def data(self, **kw):
        base = {"me": ME, "reviews": {"needs_you": [], "author": [], "well_approved": [],
                                      "done": [], "unreadable": []},
                "mine": [], "legacy": []}
        base["reviews"].update(kw.pop("reviews", {}))
        return {**base, **kw}

    def test_parked_reviews_are_not_owed_and_not_counted(self):
        parked = st(2, tier=menu.PARKED_TIER, tags=["CI failing: build", "waiting 3d"],
                    bucket="needs_you")
        s = menu.build_sections(self.data(reviews={"needs_you": [st(1, bucket="needs_you"),
                                                                 parked]}), [])
        self.assertEqual([e["url"] for e in s["owed"]], [st(1)["pr_url"]])
        self.assertEqual(s["count"], 0)      # a new review request (green) isn't counted
        self.assertEqual(s["title"], "✓")
        self.assertEqual([(e["color"], e["reason"]) for e in s["other"]],
                         [("low", "Parked: CI failing (build)")])

    def test_followups_are_one_row_per_pr_and_only_open_jobs_count(self):
        def job(pr_id, status, at="2026-01-01T00:00:00Z"):
            return {"id": f"{pr_id}{status}{at}", "status": status, "created_at": at,
                    "trigger": "reply", "actor": "dev", "my_comment": "x",
                    "pr": {"id": pr_id, "url": f"https://github.com/acme/api/pull/{pr_id}",
                           "title": f"ABC-{pr_id}: thing", "author": "dev"}}
        jobs = [job(1, "ready"), job(1, "ready", "2026-01-03T00:00:00Z"),
                job(2, "running"), job(3, "failed"), job(4, "posted")]
        data = self.data(mine=[mine_row(5, True), mine_row(6, False)])
        s = menu.build_sections(data, jobs)
        urls = [e["url"] for e in s["followups"]]
        self.assertEqual(sorted(urls), [f"https://github.com/acme/api/pull/{n}" for n in (1, 2, 3)])
        one = next(e for e in s["followups"] if e["url"].endswith("/1"))
        self.assertEqual(len(one["jobs"]), 2)
        self.assertEqual(one["waiting_since"], "2026-01-01T00:00:00Z")   # the longest wait
        self.assertEqual(s["count"], 3 + 1)      # three yellow follow-ups, one red PR of mine
        self.assertEqual(s["title"], "🔴1 🟡3")

    def test_dismissed_pr_of_mine_loses_its_dot_until_new_activity(self):
        row = mine_row(5, True)
        row["blockers"] = [{"author": "dev", "at": "2026-01-02T00:00:00Z"}]
        data = self.data(mine=[row])
        hidden = menu.build_sections(data, [], {row["url"]: "2026-01-02T00:00:00Z"})
        [listed] = hidden["mine"]                  # still listed under Your PRs ...
        self.assertTrue(listed["dismissed"])
        self.assertIsNone(listed["color"])
        self.assertEqual(hidden["count"], 0)       # ... but no longer counted
        self.assertEqual(hidden["title"], "✓")
        row["threads_waiting"] = [{"last_author": "dev", "author": "dev", "kind": "nit",
                                   "last_at": "2026-01-05T00:00:00Z"}]
        back = menu.build_sections(data, [], {row["url"]: "2026-01-02T00:00:00Z"})
        self.assertEqual(back["mine"][0]["color"], "red")
        self.assertEqual(back["count"], 1)

    def test_every_search_hit_lands_somewhere_with_a_reason(self):
        data = self.data(
            reviews={"well_approved": [st(1, reason="2 approvals, no change requests",
                                          bucket="well_approved")],
                     "author": [st(2, reason="you commented; no new commits", bucket="author")]},
            mine=[mine_row(9, False, draft=True)],
            legacy=[legacy(1), legacy(2), legacy(3, draft=True),
                    legacy(4, sources=("Assigned to you",)),
                    legacy(9, author=ME, repo="web"), legacy(7)])
        s = menu.build_sections(data, [])
        by_url = {e["url"]: e["reason"] for e in s["other"]}
        self.assertEqual(by_url["https://github.com/acme/api/pull/1"],
                         "0 approvals, no change requests")
        self.assertEqual(by_url["https://github.com/acme/api/pull/2"],
                         "Waiting on the author · 0 of 2 approvals")
        self.assertEqual(by_url["https://github.com/acme/api/pull/3"],
                         "GitHub draft, not ready for review")
        self.assertEqual(by_url["https://github.com/acme/api/pull/4"], "Not on you")
        self.assertNotIn("https://github.com/acme/web/pull/9", by_url)   # under Your PRs
        self.assertEqual([e["url"] for e in s["mine_drafts"]],
                         ["https://github.com/acme/web/pull/9"])
        self.assertIn("check this", by_url["https://github.com/acme/api/pull/7"])
        self.assertEqual(len(s["other"]), 5)   # no PR listed twice
        # Waiting on the author has no dot; the rest is ⚪ under Nothing needed.
        self.assertEqual([e["url"] for e in s["others_waiting"]],
                         ["https://github.com/acme/api/pull/2"])
        self.assertIsNone(s["others_waiting"][0]["color"])
        self.assertTrue(all(e["color"] == "low" for e in s["nothing"]))
        self.assertEqual(len(s["nothing"]), 4)
        self.assertEqual(s["count"], 0)
        self.assertEqual(s["title"], "✓")    # ⚪ rows never count
        self.assertEqual([e["reason"] for e in s["other"]][:2],
                         ["Waiting on the author · 0 of 2 approvals",
                          "0 approvals, no change requests"])

    def test_other_prs_needs_you_mixes_follow_ups_and_reviews_by_severity(self):
        def job(pr_id, blocking):
            return {"id": f"j{pr_id}", "status": "ready", "created_at": "2026-01-01T00:00:00Z",
                    "trigger": "reply", "actor": "dev", "my_comment": "x", "blocking": blocking,
                    "pr": {"id": pr_id, "url": f"https://github.com/acme/api/pull/{pr_id}",
                           "title": f"ABC-{pr_id}: thing", "author": "dev"}}
        stale = st(3, bucket="needs_you", waiting_since="2020-01-01T00:00:00Z", approvals=[],
                   engaged=[])
        fresh = st(4, bucket="needs_you", waiting_since="2999-01-01T00:00:00Z")
        s = menu.build_sections(self.data(reviews={"needs_you": [fresh, stale]}),
                                [job(1, True), job(2, False)])
        self.assertEqual([(e["url"].rsplit("/", 1)[1], e["color"]) for e in s["others_needs"]],
                         [("1", "red"), ("2", "yellow"), ("3", "yellow"), ("4", "green")])
        self.assertEqual(s["title"], "🔴1 🟡2")

    def test_other_prs_render_needs_you_waiting_then_a_collapsed_nothing_needed(self):
        import contextlib
        import io
        data = self.data(
            reviews={"needs_you": [st(1, bucket="needs_you")],
                     "author": [st(2, reason="you commented; no new commits", bucket="author",
                                   my_review=["2026-01-01T00:00:00Z", "abc"])],
                     "well_approved": [st(3, reason="2 approvals", bucket="well_approved",
                                          approvals=["a", "b"])]})
        s = menu.build_sections(data, [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                unittest.mock.patch.object(menu, "_reviews_on", return_value=False):
            menu.others_rows(s, {})
        top = [x.split(" | ")[0].strip("─ ") for x in out.getvalue().splitlines()
               if not x.startswith("--")]
        self.assertEqual(top[0], "Other PRs")
        self.assertEqual([x for x in top if x in ("Needs you", "Waiting on others")],
                         ["Needs you", "Waiting on others"])
        self.assertEqual(top[-1], "Nothing needed (1)")
        nested = [x for x in out.getvalue().splitlines() if x.startswith("--⚪ ABC-3")]
        self.assertEqual(len(nested), 1)        # the ⚪ row sits inside the submenu
        self.assertTrue(any(x.startswith("----") and "Open on GitHub" in x
                            for x in out.getvalue().splitlines()))

    def test_my_waiting_pr_is_not_repeated_in_everything_else(self):
        data = self.data(mine=[mine_row(9, True)], legacy=[legacy(9, author=ME, repo="web")])
        s = menu.build_sections(data, [])
        self.assertEqual(s["other"], [])
        self.assertEqual(s["count"], 1)
        self.assertEqual(s["mine"][0]["color"], "red")


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
        data = {"me": ME, "reviews": {"needs_you": [st(1, bucket="needs_you")], "author": [],
                                      "unreadable": [],
                                      "well_approved": [st(2, reason="2 approvals",
                                                           bucket="well_approved")],
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
            self.assertIn("**Priority:** 🟢 green (new)", (d / "api#1.md").read_text())
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
