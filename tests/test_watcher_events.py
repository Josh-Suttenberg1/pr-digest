from __future__ import annotations

import copy
import unittest

from watcher import events as ev
from watcher import render, state

ME = "Josh-Suttenberg1"


def c(cid, author, body="looks off", at="2026-11-02T10:00:00Z", bot=False, review_id=None,
      original_commit="aaa111"):
    return {"id": cid, "author": author, "is_bot": bot, "created_at": at, "url": f"u/{cid}",
            "body": body, "commit": original_commit, "original_commit": original_commit,
            "review_id": review_id}


def review(rid, author, st="COMMENTED", at="2026-11-02T10:00:00Z", body="", bot=False):
    return {"id": rid, "state": st, "submitted_at": at, "body": body, "url": f"r/{rid}",
            "author": author, "is_bot": bot}


def thread(tid, comments, outdated=False, resolved=False, resolved_by=None, path="A.scala"):
    return {"id": tid, "resolved": resolved, "outdated": outdated, "resolved_by": resolved_by,
            "path": path, "line": 10, "original_line": 10, "comments": comments}


def pr(kind="mine", reviews=(), threads=(), issue=(), approved_by=(), head="h1", author=None):
    return {"id": "PR1", "kind": kind, "repo": "processservice", "number": 833,
            "title": "WS-5857: version2 instance steps", "url": "https://g/pr/833",
            "state": "OPEN", "author": author or (ME if kind == "mine" else "dev"),
            "head": head, "updated_at": "2026-11-02T10:00:00Z",
            "approved_by": list(approved_by), "reviews": list(reviews),
            "issue_comments": list(issue), "threads": list(threads)}


def ctx(**kw):
    return ev.Ctx(me=ME, **kw)


class MinePrTests(unittest.TestCase):
    def test_seeding_is_silent_and_marks_everything_seen(self):
        p = pr(reviews=[review(1, "plaxdan", "APPROVED")], issue=[c(5, "plaxdan")])
        events, st = ev.diff_pr(p, None, ctx(seeding=True))
        self.assertEqual(events, [])
        self.assertEqual(st["seen"], ["ic:5", "rv:1"])

    def test_approval_counts_toward_two(self):
        _, st = ev.diff_pr(pr(), None, ctx(seeding=True))
        p = pr(reviews=[review(1, "plaxdan", "APPROVED")], approved_by=["plaxdan"])
        [e], st = ev.diff_pr(p, st, ctx())
        self.assertEqual((e["type"], e["state"], e["approvals"]), ("decision", "APPROVED", 1))
        p2 = pr(reviews=[review(1, "plaxdan", "APPROVED"), review(2, "dknight-avetta", "APPROVED")],
                approved_by=["plaxdan", "dknight-avetta"])
        [e2], _ = ev.diff_pr(p2, st, ctx())
        self.assertEqual((e2["actor"], e2["approvals"]), ("dknight-avetta", 2))

    def test_change_request_folds_its_inline_comments(self):
        _, st = ev.diff_pr(pr(), None, ctx(seeding=True))
        p = pr(reviews=[review(9, "plaxdan", "CHANGES_REQUESTED")],
               threads=[thread("T1", [c(11, "plaxdan", "nit: rename x", review_id=9)]),
                        thread("T2", [c(12, "plaxdan", "guard null", review_id=9)])])
        [e], _ = ev.diff_pr(p, st, ctx())
        self.assertEqual((e["type"], e["state"], e["count"]), ("decision", "CHANGES_REQUESTED", 2))

    def test_three_comments_one_notification(self):
        _, st = ev.diff_pr(pr(), None, ctx(seeding=True))
        p = pr(reviews=[review(9, "plaxdan")],
               threads=[thread(f"T{i}", [c(i, "plaxdan", f"point {i}", review_id=9)]) for i in (1, 2, 3)])
        [e], _ = ev.diff_pr(p, st, ctx())
        self.assertEqual((e["type"], e["count"], e["first_line"]), ("comments", 3, "point 1"))

    def test_bots_and_me_are_ignored(self):
        _, st = ev.diff_pr(pr(), None, ctx(seeding=True))
        p = pr(issue=[c(1, "avettabot", bot=True), c(2, ME)],
               reviews=[review(3, "copilot-pull-request-reviewer", body="x", bot=True)])
        events, _ = ev.diff_pr(p, st, ctx())
        self.assertEqual(events, [])

    def test_late_pending_review_still_notifies(self):
        _, st = ev.diff_pr(pr(), None, ctx(seeding=True))
        # written an hour before the last tick, submitted after it
        p = pr(reviews=[review(4, "plaxdan", at="2026-11-02T09:00:00Z")],
               threads=[thread("T1", [c(40, "plaxdan", at="2026-11-02T09:00:00Z", review_id=4)])])
        [e], _ = ev.diff_pr(p, st, ctx(cutoff="2026-11-02T09:55:00Z"))
        self.assertEqual(e["type"], "comments")

    def test_pending_reviews_are_skipped(self):
        _, st = ev.diff_pr(pr(), None, ctx(seeding=True))
        events, _ = ev.diff_pr(pr(reviews=[review(5, "plaxdan", "PENDING", body="draft")]), st, ctx())
        self.assertEqual(events, [])

    def test_first_sight_after_seeding_uses_cutoff(self):
        p = pr(issue=[c(1, "plaxdan", at="2026-11-01T00:00:00Z"),
                      c(2, "plaxdan", "fresh", at="2026-11-02T10:00:00Z")])
        [e], _ = ev.diff_pr(p, None, ctx(cutoff="2026-11-02T09:00:00Z"))
        self.assertEqual((e["count"], e["first_line"]), (1, "fresh"))

    def test_deleted_comment_is_pruned_from_seen(self):
        _, st = ev.diff_pr(pr(issue=[c(1, "plaxdan")]), None, ctx(seeding=True))
        _, st2 = ev.diff_pr(pr(), st, ctx())
        self.assertEqual(st2["seen"], [])

    def test_edited_comment_does_not_refire(self):
        _, st = ev.diff_pr(pr(issue=[c(1, "plaxdan", "v1")]), None, ctx(seeding=True))
        events, _ = ev.diff_pr(pr(issue=[c(1, "plaxdan", "v2 edited")]), st, ctx())
        self.assertEqual(events, [])


class ThreadTests(unittest.TestCase):
    def base(self, comments, **kw):
        return pr(kind="others", threads=[thread("T1", comments, **kw)])

    def armed(self, comments=None, **kw):
        comments = comments or [c(1, ME, "please batch this")]
        _, st = ev.diff_pr(self.base(comments, **kw), None, ctx(seeding=True))
        return st

    def test_reply_fires_once_then_outdated_does_not(self):
        st = self.armed()
        p = self.base([c(1, ME), c(2, "dev", "done in abc")])
        [e], st = ev.diff_pr(p, st, ctx())
        self.assertEqual((e["trigger"], e["actor"], e["root_id"], e["arm_id"]), ("reply", "dev", 1, 1))
        p2 = self.base([c(1, ME), c(2, "dev")], outdated=True)
        self.assertEqual(ev.diff_pr(p2, st, ctx())[0], [])

    def test_outdated_fires_once_then_reply_does_not(self):
        st = self.armed()
        [e], st = ev.diff_pr(self.base([c(1, ME)], outdated=True), st, ctx())
        self.assertEqual((e["trigger"], e["blocking"]), ("outdated", False))
        self.assertEqual(ev.diff_pr(self.base([c(1, ME), c(2, "dev")], outdated=True), st, ctx())[0], [])

    def test_my_new_comment_rearms(self):
        st = self.armed()
        _, st = ev.diff_pr(self.base([c(1, ME), c(2, "dev")]), st, ctx())
        _, st = ev.diff_pr(self.base([c(1, ME), c(2, "dev"), c(3, ME, "still missing")]), st, ctx())
        [e], _ = ev.diff_pr(self.base([c(1, ME), c(2, "dev"), c(3, ME), c(4, "dev", "fixed")]), st, ctx())
        self.assertEqual((e["arm_id"], e["first_line"]), (3, "fixed"))

    def test_outdated_at_arm_uses_paths_changed(self):
        st = self.armed(outdated=True)
        moved = self.base([c(1, ME)], outdated=True)
        moved["head"] = "h2"
        self.assertEqual(ev.diff_pr(moved, st, ctx(paths_changed=lambda r, b, h: {"Other.scala"}))[0], [])
        [e], _ = ev.diff_pr(moved, st, ctx(paths_changed=lambda r, b, h: {"A.scala"}))
        self.assertEqual(e["trigger"], "moved")

    def test_resolved_by_me_disarms_silently(self):
        st = self.armed()
        events, st = ev.diff_pr(self.base([c(1, ME)], resolved=True, resolved_by=ME), st, ctx())
        self.assertEqual(events, [])
        self.assertEqual(st["threads"]["T1"]["fired"]["trigger"], "resolved")

    def test_resolved_by_author_after_push_counts(self):
        st = self.armed()
        p = self.base([c(1, ME)], resolved=True, resolved_by="dev")
        p["head"] = "h2"
        [e], _ = ev.diff_pr(p, st, ctx())
        self.assertEqual(e["trigger"], "outdated")

    def test_third_party_reply_counts(self):
        st = self.armed()
        [e], _ = ev.diff_pr(self.base([c(1, ME), c(2, "plaxdan", "agree")]), st, ctx())
        self.assertEqual(e["actor"], "plaxdan")

    def test_bot_reply_does_not_count(self):
        st = self.armed()
        events, _ = ev.diff_pr(self.base([c(1, ME), c(2, "copilot", bot=True)]), st, ctx())
        self.assertEqual(events, [])

    def test_threads_without_my_comment_are_ignored(self):
        events, st = ev.diff_pr(self.base([c(1, "plaxdan"), c(2, "dev")]), None, ctx())
        self.assertEqual((events, st["threads"]), ([], {}))

    def test_seeding_marks_answered_threads_fired(self):
        st = self.armed([c(1, ME), c(2, "dev")])
        self.assertTrue(st["threads"]["T1"]["fired"]["silent"])
        self.assertEqual(ev.diff_pr(self.base([c(1, ME), c(2, "dev"), c(3, "dev")]), st, ctx())[0], [])

    def test_state_survives_a_json_round_trip(self):
        st = copy.deepcopy(self.armed())
        import json
        st = json.loads(json.dumps(st))
        [e], _ = ev.diff_pr(self.base([c(1, ME), c(2, "dev")]), st, ctx())
        self.assertEqual(e["trigger"], "reply")


class RenderAndStateTests(unittest.TestCase):
    def test_subtitle_uses_ticket_key(self):
        self.assertEqual(render.subtitle({"title": "[CONN-1555] Delete step safeguard lists calculations that use it",
                                          "repo": "ui", "number": 1}),
                         "CONN-1555 · Delete step safeguard lists calculation…")
        self.assertEqual(render.subtitle({"title": "no key", "repo": "ui", "number": 7}), "ui#7 · no key")

    def test_subtitle_recognizes_any_teams_ticket_key(self):
        self.assertEqual(render.subtitle({"title": "ABC-77: Short title", "repo": "ui", "number": 1}),
                         "ABC-77 · Short title")

    def test_backoff_schedule(self):
        self.assertEqual([state.backoff_delay(n) for n in (1, 2, 3, 4, 9)], [120, 240, 480, 1800, 1800])


if __name__ == "__main__":
    unittest.main()
