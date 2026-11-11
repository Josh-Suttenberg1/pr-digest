from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import swiftbar_plugin as menu
from watcher import __main__ as W
from watcher import fyi, gh, popups

CFG = {"notify": {"swiftbar_plugin": "p.1h.sh", "sounds": True, "alert_sound": "Glass",
                  "red_sound": "/red.aiff", "yellow_sound": "/yellow.aiff"},
       "watcher": {"required_approvals": 2}, "models": {"fyi": "haiku"}}
NICK = {"kylerscharpen": "Kyler", "mthomas46": "Mykal"}
URL = "https://github.com/avetta/ui/pull/7"


def mine_pr(approved_by=()):
    return {"id": "PR_7", "kind": "mine", "repo": "ui", "number": 7, "url": URL,
            "title": "[CONN-1234] Show the total", "approved_by": list(approved_by)}


def approval(actor, body="", count=0):
    return {"type": "decision", "state": "APPROVED", "actor": actor, "body": body,
            "count": count, "pr": {"id": "PR_7"}}


class PraiseTest(unittest.TestCase):
    def test_empty_and_obvious_praise_need_no_claude(self):
        never = mock.Mock(side_effect=AssertionError("asked Claude"))
        for body in ("", "LGTM", "lgtm!", "Looks good to me.", "Nice work 🚀", "👍",
                     "Ship it", "Thanks!\n\nDrafted with Claude 🤖"):
            with self.subTest(body=body):
                self.assertTrue(fyi.praise_only(body, CFG, ask_claude=never))

    def test_anything_that_might_ask_is_not_praise(self):
        never = mock.Mock(side_effect=AssertionError("asked Claude"))
        for body in ("LGTM, but can you rename x?", "Nice. Please add a test", "nit: spacing",
                     "Looks good, consider a follow-up ticket", "Should this be async"):
            with self.subTest(body=body):
                self.assertFalse(fyi.praise_only(body, CFG, ask_claude=never))

    def test_claude_decides_the_rest_and_doubt_means_it_asks(self):
        body = "The paging reads really well now, great call splitting the decoder."
        self.assertTrue(fyi.praise_only(body, CFG, ask_claude=lambda t, c: False))
        self.assertFalse(fyi.praise_only(body, CFG, ask_claude=lambda t, c: True))
        self.assertFalse(fyi.praise_only(body, CFG, ask_claude=lambda t, c: None))

        def boom(t, c):
            raise subprocess.TimeoutExpired("claude", 90)
        self.assertFalse(fyi.praise_only(body, CFG, ask_claude=boom))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "fyi.json"
        self.item = {"id": "approved-ui-7-kylerscharpen", "kind": "approved",
                     "pr": {"url": URL, "repo": "ui", "number": 7, "title": "x"},
                     "headline": "Kyler approved CONN-1234, no comment",
                     "row": "Kyler approved, no comment"}

    def test_add_stores_once_and_sends_a_silent_banner(self):
        calls = []

        def run(argv, **_):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")
        added = fyi.add([self.item], CFG, path=self.path, run=run)
        again = fyi.add([self.item], CFG, path=self.path, run=run)
        self.assertEqual((len(added), again), (1, []))
        sends = [c for c in calls if "-message" in c]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][sends[0].index("-group") + 1], "fyi-approved-ui-7-kylerscharpen")
        self.assertFalse(any(c[0] == "/usr/bin/afplay" for c in calls))
        self.assertEqual(popups.load()["fyi-approved-ui-7-kylerscharpen"]["kind"], "fyi")

    def test_seen_on_menu_open_then_gone_ten_seconds_later(self):
        fyi.add([self.item], CFG, path=self.path, send=False)
        withdrawn = []
        shown = fyi.on_render("", now=1000, path=self.path, withdraw=withdrawn.extend)
        self.assertEqual(len(shown), 1)
        self.assertIsNone(fyi.load(self.path)[self.item["id"]]["seen_at"])   # not opened yet
        shown = fyi.on_render("MenuOpen", now=1000, path=self.path, withdraw=withdrawn.extend)
        self.assertEqual(len(shown), 1)                     # shown in the render that saw it
        self.assertEqual(withdrawn, ["fyi-approved-ui-7-kylerscharpen"])
        self.assertEqual(len(fyi.on_render("MenuOpen", now=1009, path=self.path,
                                           withdraw=withdrawn.extend)), 1)
        self.assertEqual(withdrawn, ["fyi-approved-ui-7-kylerscharpen"])   # once
        self.assertEqual(fyi.on_render("", now=1010, path=self.path, withdraw=withdrawn.extend), [])
        self.assertEqual(fyi.load(self.path), {})

    def test_the_fyi_popup_survives_its_pr_closing(self):
        entries = {"fyi-x": {"kind": "fyi", "pr": {"id": "PR_7", "url": URL}}}
        self.assertEqual(popups.resolved_closed(entries, {"PR_7": "MERGED"}), {})


class ApprovalFyiTest(unittest.TestCase):
    def fyis(self, events, approved_by, now_group=None, before=None, delivered=(), praise=None):
        groups_now = {"PR_7": {"group": now_group, "reason": "Ready: 2 of 2 approvals"}} if now_group else {}
        return W.approval_fyis({"PR_7": mine_pr(approved_by)}, events, groups_now,
                               {"PR_7": before} if before else {}, set(delivered), NICK, CFG,
                               praise=praise or (lambda b: fyi.praise_only(b, CFG,
                                                                           ask_claude=lambda t, c: True)))

    def test_a_bare_approval_is_an_fyi(self):
        [it] = self.fyis([approval("kylerscharpen")], ["kylerscharpen"], "waiting", "waiting")
        self.assertEqual(it["headline"], "Kyler approved CONN-1234, no comment")
        self.assertEqual(it["row"], "Kyler approved, no comment")
        self.assertEqual(it["id"], "approved-ui-7-kylerscharpen")

    def test_a_praise_only_approval_quotes_it(self):
        [it] = self.fyis([approval("kylerscharpen", "LGTM 🚀")], ["kylerscharpen"], "waiting", "waiting")
        self.assertEqual(it["headline"], "Kyler approved CONN-1234: “LGTM 🚀”")

    def test_an_approval_that_asks_or_has_inline_comments_is_not(self):
        self.assertEqual(self.fyis([approval("kylerscharpen", "LGTM, rename x?")],
                                   ["kylerscharpen"], "needs_you", "waiting"), [])
        self.assertEqual(self.fyis([approval("kylerscharpen", count=2)],
                                   ["kylerscharpen"], "needs_you", "waiting"), [])

    def test_becoming_ready_replaces_the_approval(self):
        [it] = self.fyis([approval("kylerscharpen")], ["mthomas46", "kylerscharpen"], "ready", "waiting")
        self.assertEqual(it["kind"], "ready")
        self.assertEqual(it["headline"], "CONN-1234 · Ready: 2 of 2 approvals")
        self.assertEqual(it["row"], "Ready: 2 of 2 approvals · Kyler approved")

    def test_ready_without_an_earlier_classification_counts_approvals(self):
        [it] = self.fyis([approval("kylerscharpen")], ["mthomas46", "kylerscharpen"], "ready")
        self.assertEqual(it["kind"], "ready")
        [it] = self.fyis([approval("kylerscharpen")], ["a", "b", "kylerscharpen"], "ready")
        self.assertEqual(it["kind"], "approved")      # was Ready already with 2 of 2

    def test_already_ready_is_just_an_approval(self):
        [it] = self.fyis([approval("kylerscharpen")], ["a", "kylerscharpen"], "ready", "ready")
        self.assertEqual(it["kind"], "approved")

    def test_a_pr_that_got_an_action_popup_gets_no_fyi(self):
        self.assertEqual(self.fyis([approval("kylerscharpen")], ["kylerscharpen"], "waiting",
                                   "waiting", delivered=["PR_7"]), [])

    def test_others_prs_get_no_approval_fyi(self):
        pr = {**mine_pr(["kylerscharpen"]), "kind": "others"}
        self.assertEqual(W.approval_fyis({"PR_7": pr}, [approval("kylerscharpen")], {}, {}, set(),
                                         NICK, CFG, praise=lambda b: True), [])


class OwedFyiTest(unittest.TestCase):
    OWED = "https://github.com/avetta/api/pull/3"
    KEPT = "https://github.com/avetta/api/pull/4"

    def run_owed(self, st, owed_urls, live, stamp="t2"):
        cache = {"fetched_at": stamp, "reviews": {}}
        entries = [{"url": u, "title": f"[CONN-{u[-1]}] x"} for u in owed_urls]
        with mock.patch("review.auto_draft.cached_triage", return_value=(cache, 0)), \
                mock.patch("review.auto_draft.classify_owed", return_value=entries), \
                mock.patch.object(gh, "review_states", return_value=live) as rs:
            out = W.owed_fyis(st, CFG, "me", "tok", NICK, "2026-11-10T12:00:00Z")
        return out, rs

    def test_the_first_look_only_records(self):
        st = {}
        out, rs = self.run_owed(st, [self.OWED], {})
        self.assertEqual(out, [])
        rs.assert_not_called()
        self.assertEqual(st["owed"][self.OWED]["since"], "2026-11-10T12:00:00Z")

    def test_merged_and_removed_are_fyis_and_reviewed_is_not(self):
        since = {"since": "2026-11-09T00:00:00Z", "title": "[CONN-9] x"}
        st = {"owed_fetched_at": "t1", "owed": {self.OWED: since, self.KEPT: since,
                                                "https://github.com/avetta/api/pull/5": since}}
        live = {self.OWED: {"state": "MERGED", "reviews": []},
                self.KEPT: {"state": "OPEN", "reviews": [], "requested": False},
                "https://github.com/avetta/api/pull/5": {
                    "state": "OPEN", "requested": False,
                    "reviews": [{"state": "APPROVED", "submitted_at": "2026-11-10T00:00:00Z"}]}}
        out, _ = self.run_owed(st, [], live)
        self.assertEqual([it["row"] for it in out],
                         ["Review no longer needed: the PR merged",
                          "Review no longer needed: you were removed as a reviewer"])
        self.assertEqual(st["owed"], {})

    def test_still_requested_but_off_the_list_is_no_fyi(self):
        st = {"owed_fetched_at": "t1", "owed": {self.OWED: {"since": "2026-11-09T00:00:00Z"}}}
        out, _ = self.run_owed(st, [], {self.OWED: {"state": "OPEN", "reviews": [], "requested": True}})
        self.assertEqual(out, [])
        out, _ = self.run_owed({"owed_fetched_at": "t1", "owed": {self.OWED: {"since": "x"}}}, [],
                               {self.OWED: {"state": "OPEN", "reviews": [], "requested": None}})
        self.assertEqual(out, [])         # a team is requested: can't tell, so nothing

    def test_the_same_fetch_is_read_once(self):
        st = {"owed_fetched_at": "t2", "owed": {self.OWED: {"since": "x"}}}
        out, rs = self.run_owed(st, [], {})
        self.assertEqual(out, [])
        rs.assert_not_called()

    def test_a_github_error_keeps_the_set_for_next_tick(self):
        st = {"owed_fetched_at": "t1", "owed": {self.OWED: {"since": "x"}}}
        cache = {"fetched_at": "t2", "reviews": {}}
        with mock.patch("review.auto_draft.cached_triage", return_value=(cache, 0)), \
                mock.patch("review.auto_draft.classify_owed", return_value=[]), \
                mock.patch.object(gh, "review_states", side_effect=gh.GhError("boom")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(W.owed_fyis(st, CFG, "me", "t", NICK, "2026-11-10T12:00:00Z"), [])
        self.assertEqual((st["owed_fetched_at"], list(st["owed"])), ("t1", [self.OWED]))


class ReviewOutcomeTest(unittest.TestCase):
    def test_inline_only_review_counts_once_the_request_is_gone(self):
        live = {"state": "OPEN", "requested": False,
                "reviews": [{"state": "COMMENTED", "body": "", "submitted_at": "2026-11-10T11:00:00Z"}]}
        self.assertEqual(popups.review_outcome(live, "2026-11-10T09:00:00Z"),
                         ("reviewed", "you reviewed it"))
        self.assertIsNone(popups.review_outcome({**live, "requested": True}, "2026-11-10T09:00:00Z"))

    def test_removed_resolves_a_review_popup(self):
        entry = {"kind": "review", "since": "2026-11-10T09:00:00Z"}
        self.assertEqual(popups.review_resolution(entry, {"state": "OPEN", "reviews": [],
                                                          "requested": False}, "me"),
                         "you were removed as a reviewer")

    def test_requested_reads_users_and_teams(self):
        def pr(*reviewers):
            return {"reviewRequests": {"nodes": [{"requestedReviewer": r} for r in reviewers]}}
        self.assertTrue(gh._requested(pr({"__typename": "User", "login": "me"}), "me"))
        self.assertIsNone(gh._requested(pr({"__typename": "Team"}), "me"))
        self.assertFalse(gh._requested(pr({"__typename": "User", "login": "x"}), "me"))


class MenuTest(unittest.TestCase):
    def test_fyi_rows_lead_with_the_white_dot_and_open_the_pr(self):
        items = [{"pr": {"url": URL, "title": "[CONN-1234] Show the total"},
                  "row": "Kyler approved, no comment", "headline": "h", "at": None}]
        buf = io.StringIO()
        with redirect_stdout(buf):
            menu.fyi_rows(items)
        lines = buf.getvalue().splitlines()
        self.assertEqual(lines[0].split("|")[0].strip(), "FYI")
        self.assertTrue(lines[1].startswith("⚪ CONN-1234 · Kyler approved, no comment"))
        self.assertIn(f"href={URL}", lines[1])

    def test_a_broken_store_never_breaks_the_menu(self):
        with mock.patch.object(fyi, "on_render", side_effect=OSError("disk")):
            self.assertEqual(menu.fyi_items(), [])


if __name__ == "__main__":
    unittest.main()
