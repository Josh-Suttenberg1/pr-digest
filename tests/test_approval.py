"""Ready to approve: what your approval of someone else's PR waits on
(watcher/approval.py needs), whether Claude's follow-ups cover it (status), the red
row and popup that lead with it (triage/priority.py followup, watcher/__main__.py
hold_for_drafts), the menu's Approve (swiftbar_plugin.py followup_block), and the
click (watcher/followup.py approve)."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import swiftbar_plugin as menu
from triage import priority as P
from watcher import __main__ as W
from watcher import approval as A
from watcher import followup

ME, AUTHOR = "Josh-Suttenberg1", "dknight-avetta"
URL = "https://github.com/avetta/processservice/pull/870"
NICK = {AUTHOR: "DaVinci"}
CR_ID = 5866691736
BLOCKING = "**Blocking:** nothing tests the 410 Gone paths."
NIT = "nit: rename this."


def rv(rid, state, at, commit="c1", author=ME):
    return {"id": rid, "state": state, "submitted_at": at, "body": "", "url": f"r/{rid}",
            "commit": commit, "author": author, "is_bot": False}


def thread(tid, body, cid, resolved=False, path="app/Routes.scala", line=203, reply=True):
    comments = [{"id": cid, "author": ME, "is_bot": False, "body": body,
                 "created_at": "2026-11-11T23:25:00Z"}]
    if reply:
        comments.append({"id": cid + 1, "author": AUTHOR, "is_bot": False, "body": "Done",
                         "created_at": "2026-11-12T14:00:00Z"})
    return {"id": tid, "resolved": resolved, "outdated": False, "path": path, "line": line,
            "comments": comments}


def snap(reviews=(), threads=(), head="c2", state="OPEN"):
    return {"id": "PR870", "kind": "others", "repo": "processservice", "number": 870,
            "title": "[CONN-2293] Retire three v1 reads", "url": URL, "state": state,
            "author": AUTHOR, "head": head, "reviews": list(reviews), "threads": list(threads),
            "issue_comments": [], "review_requests": [], "approved_by": []}


MY_CR = rv(CR_ID, "CHANGES_REQUESTED", "2026-11-11T23:25:28Z")
T_BLOCK = thread("T1", BLOCKING, 100)
T_NIT = thread("T2", NIT, 200, path="app/Other.scala", line=7)

PR = {"id": "PR870", "repo": "processservice", "number": 870, "url": URL, "author": AUTHOR,
      "title": "[CONN-2293] Retire three v1 reads"}


def cr_job(verdict="addressed", head="c2", status="ready", jid="cr1", at="2026-11-12T15:00:00Z"):
    return {"id": jid, "trigger": "change_request", "status": status, "verdict": verdict,
            "review_id": CR_ID, "head": head, "pr": PR, "actor": AUTHOR, "created_at": at,
            "triggers": ["pushed"]}


def t_job(tid="T1", arm=100, verdict="addressed", head="c2", status="ready", jid="t1",
          blocking=True):
    return {"id": jid, "trigger": "reply", "status": status, "verdict": verdict,
            "thread_id": tid, "arm_id": arm, "head": head, "pr": PR, "actor": AUTHOR,
            "blocking": blocking, "created_at": "2026-11-12T14:50:00Z", "my_comment": BLOCKING}


class NeedsTest(unittest.TestCase):
    def test_a_change_request_and_a_blocking_thread(self):
        n = A.needs(snap([MY_CR], [T_BLOCK, T_NIT]), ME)
        self.assertEqual((n["verb"], n["change_request"], n["head"]), ("approve", CR_ID, "c2"))
        self.assertEqual([(t["id"], t["arm_id"], t["blocking"], t["where"]) for t in n["threads"]],
                         [("T1", 100, True, "Routes.scala:203"), ("T2", 200, False, "Other.scala:7")])

    def test_resolved_threads_and_threads_without_you_dont_count(self):
        theirs = {**thread("T3", "a question", 300), "comments": [
            {"id": 300, "author": "mthomas46", "is_bot": False, "body": "?"}]}
        n = A.needs(snap([MY_CR], [thread("T1", BLOCKING, 100, resolved=True), theirs]), ME)
        self.assertEqual(n["threads"], [])

    def test_an_older_approval_is_a_reapproval(self):
        n = A.needs(snap([rv(1, "APPROVED", "2026-11-11T10:00:00Z", commit="c1")], [T_NIT]), ME)
        self.assertEqual((n["verb"], n["change_request"]), ("reapprove", None))

    def test_nothing_to_approve(self):
        self.assertIsNone(A.needs(snap([rv(1, "APPROVED", "2026-11-12T10:00:00Z", "c2")],
                                       [T_BLOCK]), ME))
        self.assertIsNone(A.needs(snap([MY_CR], state="MERGED"), ME))
        self.assertIsNone(A.needs(snap([]), ME))
        self.assertIsNone(A.needs({**snap([MY_CR]), "author": ME}, ME))

    def test_your_latest_decision_counts(self):
        later = rv(2, "APPROVED", "2026-11-12T00:00:00Z", commit="c1")
        n = A.needs(snap([MY_CR, later], [T_BLOCK]), ME)
        self.assertEqual((n["verb"], n["change_request"]), ("reapprove", None))


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.needs = A.needs(snap([MY_CR], [T_BLOCK, T_NIT]), ME)

    def test_ready_when_the_change_request_and_blocking_comment_are_addressed(self):
        st = A.status(self.needs, [cr_job(), t_job()])
        self.assertTrue(st["ready"])
        self.assertEqual((st["label"], st["what"], st["why"]),
                         ("Ready to approve", "your blocking comment and change request", ""))

    def test_a_nit_not_addressed_doesnt_hold_it_back(self):
        st = A.status(self.needs, [cr_job(), t_job(),
                                   t_job("T2", 200, "not_addressed", jid="t2", blocking=False)])
        self.assertTrue(st["ready"])

    def test_the_change_request_must_be_judged_on_the_current_head(self):
        st = A.status(self.needs, [cr_job(head="c1"), t_job()])
        self.assertFalse(st["ready"])
        self.assertEqual(st["why"], "the author pushed after Claude checked your change request")

    def test_a_partly_addressed_change_request(self):
        st = A.status(self.needs, [cr_job("partly"), t_job()])
        self.assertEqual(st["why"], "Claude found your change request partly addressed")

    def test_the_newest_change_request_job_decides(self):
        old = cr_job("partly", jid="cr0", at="2026-11-12T13:00:00Z")
        self.assertTrue(A.status(self.needs, [old, cr_job(), t_job()])["ready"])

    def test_an_unanswered_blocking_comment(self):
        st = A.status(self.needs, [cr_job()])
        self.assertFalse(st["ready"])
        self.assertEqual(st["why"], "your comment on Routes.scala:203 has no answer yet")

    def test_a_job_armed_on_an_older_comment_of_yours_doesnt_count(self):
        st = A.status(self.needs, [cr_job(), t_job(arm=99)])
        self.assertFalse(st["ready"])

    def test_not_while_claude_is_still_checking(self):
        st = A.status(self.needs, [cr_job(status="running", verdict=None), t_job()])
        self.assertFalse(st["ready"])
        self.assertIn("Claude is still checking", st["why"])

    def test_a_posted_reply_still_counts(self):
        self.assertTrue(A.status(self.needs, [cr_job(status="posted"), t_job(status="posted")])["ready"])

    def test_nothing_to_say_without_follow_ups(self):
        self.assertIsNone(A.status(self.needs, []))
        self.assertIsNone(A.status(None, [cr_job()]))
        other = {**cr_job(), "pr": {**PR, "id": "OTHER"}}
        self.assertIsNone(A.status(self.needs, [other]))

    def test_with_nothing_blocking_every_answered_comment_must_be_addressed(self):
        n = A.needs(snap([rv(1, "APPROVED", "2026-11-11T10:00:00Z", "c1")], [T_NIT]), ME)
        nit = t_job("T2", 200, jid="t2", blocking=False)
        st = A.status(n, [nit])
        self.assertTrue(st["ready"])
        self.assertEqual((st["label"], st["what"]), ("Ready to re-approve", "your comment"))
        self.assertFalse(A.status(n, [{**nit, "verdict": "partly"}])["ready"])

    def test_body(self):
        self.assertEqual(A.body("processservice", "cd143ba822", "approve"),
                         "Approving, my review comments are addressed as of "
                         "[cd143ba](https://github.com/avetta/processservice/commit/cd143ba822)."
                         "\n\nDrafted with Claude 🤖")
        self.assertTrue(A.body("r", "abc1234", "reapprove").startswith("Reapproving,"))


class RowTest(unittest.TestCase):
    def ready(self):
        return A.status(A.needs(snap([MY_CR], [T_BLOCK]), ME), [cr_job(), t_job()])

    def test_the_row_leads_with_ready_to_approve(self):
        f = P.followup([cr_job(), t_job()], NICK, author=AUTHOR, approvals=1, required=2,
                       rerequested_at="2026-11-12T14:44:23Z", approval=self.ready())
        self.assertEqual(f["color"], P.RED)
        self.assertEqual(f["reason"], "DaVinci addressed your blocking comment and change "
                                      "request · ready to approve · 1 of 2 approvals")
        self.assertEqual(f["why"], "Why it's red: Claude found your blocking comment and change "
                                   "request addressed; approving is one click")
        self.assertTrue(f["approval"]["ready"])

    def test_mentions_still_show(self):
        mention = {"id": "m1", "trigger": "mention", "status": "ready", "pr": PR,
                   "actor": "mthomas46", "created_at": "2026-11-12T15:10:00Z"}
        f = P.followup([cr_job(), t_job(), mention], {**NICK, "mthomas46": "Mykal"},
                       author=AUTHOR, approval=self.ready())
        self.assertIn("ready to approve", f["reason"])
        self.assertIn("Mykal mentioned you", f["reason"])

    def test_not_ready_keeps_the_usual_row(self):
        st = A.status(A.needs(snap([MY_CR], [T_BLOCK]), ME), [cr_job("partly"), t_job()])
        f = P.followup([cr_job("partly"), t_job()], NICK, author=AUTHOR, approval=st)
        self.assertNotIn("Ready to", f["reason"])

    def test_a_dismissed_row_stays_dismissed(self):
        f = P.followup([cr_job(), t_job()], NICK, author=AUTHOR, approval=self.ready(),
                       dismissed_at="2026-11-13T00:00:00Z")
        self.assertIsNone(f["color"])

    def test_classify_carries_it_by_url(self):
        st = {"pr_url": URL, "author": AUTHOR, "title": "t", "bucket": "needs_you",
              "mine": ["CHANGES_REQUESTED", "c1"], "approvals": []}
        from datetime import datetime, timezone
        [e] = P.classify([], {"needs_you": [st]}, [cr_job(), t_job()], nick=NICK,
                         now=datetime(2026, 11, 12, 18, tzinfo=timezone.utc),
                         approvals={URL: self.ready()})
        self.assertTrue(e["reason"].startswith("DaVinci addressed"))
        self.assertIn("ready to approve", e["reason"])

    def test_the_popup_after_drafts_says_it_too(self):
        pending = {"PR870": {"pr": {**PR, "kind": "others"}, "events": [],
                             "rerequested_at": "", "since": "2026-11-12T15:00:00Z"}}
        needs = A.needs(snap([MY_CR], [T_BLOCK]), ME)
        [(_, color, reason, _, _)] = W.hold_for_drafts(
            [], pending, [cr_job(), t_job()], "2026-11-12T15:02:00Z", NICK, ME,
            needs={"PR870": needs})
        self.assertEqual(color, P.RED)
        self.assertTrue(reason.startswith("DaVinci addressed"))


class MenuTest(unittest.TestCase):
    def render(self, approval):
        jobs = [{**cr_job(), "report": "/r/cr.md"}, {**t_job(), "report": "/r/t.md"}]
        e = {"url": URL, "title": "t", "reason": "r", "why": "w", "color": P.RED,
             "jobs": jobs, "st": None, "approval": approval}
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(menu, "_org", return_value="avetta"), \
                mock.patch.object(menu, "_draft_actions"), \
                mock.patch.object(menu, "_response_detail"):
            menu.followup_block(e, NICK)
        return out.getvalue().splitlines()

    def test_an_approve_button_with_the_exact_text(self):
        st = A.status(A.needs(snap([MY_CR], [T_BLOCK]), ME), [cr_job(), t_job()])
        lines = self.render(st)
        [button] = [l for l in lines if l.startswith("--Approve |")]
        self.assertIn("param1=approve param2=PR870 param3=c2", button)
        self.assertIn("--Posts: Approving, my review comments are addressed as of c2. "
                      "Drafted with Claude 🤖 |", "\n".join(lines))

    def test_not_ready_says_why_and_has_no_button(self):
        st = A.status(A.needs(snap([MY_CR], [T_BLOCK]), ME), [cr_job("partly"), t_job()])
        lines = self.render(st)
        self.assertFalse(any(l.startswith("--Approve") for l in lines))
        self.assertTrue(any(l.startswith("--Not ready to approve: Claude found your change "
                                         "request partly addressed") for l in lines))

    def test_build_sections_holds_saved_needs_against_live_jobs(self):
        st = {"pr_url": URL, "author": AUTHOR, "title": "t", "bucket": "needs_you",
              "mine": ["CHANGES_REQUESTED", "c1"], "approvals": []}
        needs = A.needs(snap([MY_CR], [T_BLOCK]), ME)
        s = menu.build_sections({"reviews": {"needs_you": [st]}}, [cr_job(), t_job()],
                                nick=NICK, approval_needs={URL: needs})
        [e] = s["followups"]
        self.assertTrue(e["approval"]["ready"])


class ApproveClickTest(unittest.TestCase):
    CFG = {"me": ME, "org": "avetta", "watcher": {"bots": []}}

    def click(self, current, jobs, head="c2"):
        posted, banners, cleared = [], [], []

        def run(args, **kw):
            posted.append(json.loads(Path(args[args.index("--input") + 1]).read_text()))
            return mock.Mock(returncode=0, stdout="https://github.com/r/870#pullrequestreview-9\n")

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(followup, "JOBS_DIR", Path(d)), \
                mock.patch("watcher.gh.token", return_value="t"), \
                mock.patch("watcher.gh.details", return_value={"PR870": current}), \
                mock.patch.object(followup, "all_jobs", return_value=jobs), \
                mock.patch.object(followup, "clear", lambda jid, why: cleared.append((jid, why))), \
                mock.patch.object(followup.subprocess, "run", run), \
                mock.patch.object(followup.notify, "banner",
                                  lambda *a, **k: banners.append(a)):
            rc = followup.approve("PR870", head, self.CFG)
        return rc, posted, banners, cleared

    def test_posts_the_approval_on_the_head_and_clears_the_follow_ups(self):
        rc, posted, banners, cleared = self.click(snap([MY_CR], [T_BLOCK]), [cr_job(), t_job()])
        self.assertEqual(rc, 0)
        self.assertEqual(posted, [{"commit_id": "c2", "event": "APPROVE",
                                   "body": A.body("processservice", "c2", "approve")}])
        self.assertEqual(sorted(cleared), [("cr1", "you approved the PR"),
                                           ("t1", "you approved the PR")])
        self.assertEqual(banners[0][0], "✅ Approved")

    def test_nothing_posts_when_the_author_pushed_since(self):
        rc, posted, banners, _ = self.click(snap([MY_CR], [T_BLOCK], head="c3"),
                                            [cr_job(), t_job()])
        self.assertEqual((rc, posted), (1, []))
        self.assertIn("pushed since", banners[0][2])

    def test_nothing_posts_when_it_is_no_longer_ready(self):
        rc, posted, banners, _ = self.click(snap([MY_CR], [T_BLOCK]), [cr_job("partly"), t_job()])
        self.assertEqual((rc, posted), (1, []))
        self.assertEqual(banners[0][2], "Claude found your change request partly addressed")


if __name__ == "__main__":
    unittest.main()
