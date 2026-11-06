"""Your PRs: comment kinds, approval order, the menu section, notifications, links."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import status_report
import swiftbar_plugin as menu
from review import view_draft
from triage import comment_kind as ck
from triage import mine
from triage import priority as P
from watcher import events as ev
from watcher import render

ME = "me-login"
PR = "https://github.com/avetta/ui/pull/9168"
NOW = datetime(2026, 11, 6, 18, 0, tzinfo=timezone.utc)


class CommentKindTest(unittest.TestCase):
    def test_labels(self):
        cases = {
            "**Nit:** rename this": "nit",
            "nit: rename this": "nit",
            "[nit] rename this": "nit",
            "Minor: spacing": "nit",
            "**Suggestion:** add a test": "suggestion",
            "Suggestion: add a test": "suggestion",
            "**Blocking:** this drops data": "blocking",
            "Question: why here?": "question",
            "Why is this a Set and not a List?": "question",
            "Looks off to me.": "comment",
            "Nit picking aside, this is fine.": "comment",
            "Use this:\n```suggestion\nval x = 1\n```": "suggestion",
            "**Comment 1 · Suggestion:** rename this": "suggestion",
            "**Comment 10 · Nit:** spacing": "nit",
            "Comment 2 - Blocking: drops rows": "blocking",
            "Comment 3 is wrong": "comment",
        }
        for body, want in cases.items():
            self.assertEqual(ck.kind(body), want, body)

    def test_summary_drops_the_label_and_emphasis(self):
        self.assertEqual(ck.summary("**Suggestion:** No test calls `f`.\n\n*(Drafted)*"),
                         "No test calls `f`.")
        self.assertEqual(ck.summary("**Comment 4 · Nit:** spacing"), "spacing")

    def test_phrase_orders_decisions_before_nits(self):
        self.assertEqual(ck.phrase(["nit", "suggestion", "nit", "suggestion", "nit"]),
                         "2 suggestions, 3 nits")
        self.assertEqual(ck.phrase([]), "")


def review(login, state, at, url="r"):
    return {"author": {"login": login, "__typename": "User"}, "state": state,
            "submittedAt": at, "body": "", "url": f"{url}/{login}/{at}"}


def thread(*comments):
    return {"isResolved": False, "isOutdated": False,
            "comments": {"nodes": [{"author": {"login": a, "__typename": "User"},
                                    "createdAt": at, "body": body, "path": "src/A.elm",
                                    "url": f"t/{at}"} for a, at, body in comments]}}


def gql(reviews=(), threads=(), requested=()):
    return {"isDraft": False, "reviewDecision": None, "commits": {"nodes": []},
            "reviewThreads": {"nodes": list(threads)}, "reviews": {"nodes": list(reviews)},
            "comments": {"nodes": []}, "timelineItems": {"nodes": []},
            "reviewRequests": {"nodes": [{"requestedReviewer": r} for r in requested]}}


class ApprovalsTest(unittest.TestCase):
    def test_approvals_are_numbered_in_the_order_they_stand(self):
        a = mine.analyze(gql(reviews=[
            review("plaxdan", "APPROVED", "2026-10-28T17:40:00Z"),
            review("josh-mills", "APPROVED", "2026-10-28T17:35:00Z"),
            review("plaxdan", "APPROVED", "2026-11-04T16:58:00Z"),   # re-approval
        ]), ME)
        self.assertEqual([(x["author"], x["ordinal"]) for x in a["approvals"]],
                         [("josh-mills", 1), ("plaxdan", 2)])

    def test_a_later_comment_does_not_withdraw_a_decision(self):
        a = mine.analyze(gql(reviews=[
            review("rev", "CHANGES_REQUESTED", "2026-10-29T03:50:00Z"),
            review("rev", "COMMENTED", "2026-10-30T00:00:00Z"),
            review("ok", "APPROVED", "2026-10-29T00:00:00Z"),
            review("ok", "COMMENTED", "2026-10-30T00:00:00Z"),
        ]), ME)
        self.assertEqual([b["author"] for b in a["blockers"]], ["rev"])
        self.assertEqual([x["author"] for x in a["approvals"]], ["ok"])

    def test_each_approval_says_whether_it_is_on_the_head_commit(self):
        old = {**review("plaxdan", "APPROVED", "2026-11-03T10:00:00Z"), "commit": {"oid": "aaa"}}
        new = {**review("kyler", "APPROVED", "2026-11-04T10:00:00Z"), "commit": {"oid": "bbb"}}
        pr = {**gql(reviews=[old, new], requested=[{"login": "mthomas46"}]),
              "headRefOid": "bbb",
              "commits": {"nodes": [{"commit": {"oid": "bbb", "committedDate":
                                                "2026-11-04T09:00:00Z", "author": None}}]},
              "timelineItems": {"nodes": [
                  {"createdAt": "2026-11-02T10:00:00Z", "requestedReviewer": {"login": "mthomas46"}},
                  {"createdAt": "2026-11-04T11:00:00Z", "requestedReviewer": {"login": "mthomas46"}}]}}
        a = mine.analyze(pr, ME)
        self.assertEqual([(x["author"], x["commit"], x["on_head"]) for x in a["approvals"]],
                         [("plaxdan", "aaa", False), ("kyler", "bbb", True)])
        self.assertEqual((a["head_oid"], a["head_at"]), ("bbb", "2026-11-04T09:00:00Z"))
        self.assertEqual(a["requested_since"], {"mthomas46": "2026-11-04T11:00:00Z"})

    def test_an_approval_without_commit_data_counts_as_on_the_head(self):
        a = mine.analyze(gql(reviews=[review("plaxdan", "APPROVED", "2026-11-03T10:00:00Z")]), ME)
        self.assertTrue(a["approvals"][0]["on_head"])

    def test_a_dismissed_approval_does_not_count(self):
        a = mine.analyze(gql(reviews=[review("m0n01d", "APPROVED", "2026-09-09T10:00:00Z"),
                                      review("m0n01d", "DISMISSED", "2026-09-10T10:00:00Z")]), ME)
        self.assertEqual(a["approvals"], [])

    def test_pending_requests_and_comment_kinds(self):
        a = mine.analyze(gql(
            threads=[thread(("plaxdan", "2026-11-04T16:58:42Z", "**Nit:** move the mock")),
                     thread((ME, "2026-11-01T00:00:00Z", "Should this be opaque?"),
                            ("plaxdan", "2026-11-04T16:58:42Z", "Suggestion: yes, wrap it"))],
            requested=[{"login": "dknight-avetta"}, {"name": "ui-reviewers"}]), ME)
        self.assertEqual(a["requested"], ["dknight-avetta", "team:ui-reviewers"])
        self.assertEqual([(t["kind"], t["author"], t["summary"]) for t in a["threads_waiting"]],
                         [("nit", "plaxdan", "move the mock"),
                          ("suggestion", "plaxdan", "yes, wrap it")])


def mine_row(num, *, waiting=False, approvals=0, blockers=(), answered=(), threads=(),
             updated="2026-11-01T00:00:00Z", draft=False):
    return {"url": f"https://github.com/avetta/ui/pull/{num}", "title": f"[ABC-{num}] thing {num}",
            "repo": "ui", "number": num, "isDraft": draft, "waiting": waiting,
            "updated_at": updated, "unanswered_comments": [], "requested": [],
            "blockers": list(blockers), "answered_change_requests": list(answered),
            "threads_waiting": list(threads),
            "approvals": [{"author": f"a{i}", "at": "2026-11-01T00:00:00Z", "url": "",
                           "ordinal": i} for i in range(1, approvals + 1)]}


def woods_thread(kind, summary):
    return {"path": "src/elm/Types.elm", "author": "plaxdan", "last_author": "plaxdan",
            "last_at": "2026-11-04T16:58:42Z", "url": f"{PR}#discussion_r1", "kind": kind,
            "summary": summary, "bodies": [{"author": "plaxdan",
                                            "body": f"**{kind.title()}:** {summary}"}]}


class YourPrsSectionTest(unittest.TestCase):
    def data(self, rows):
        return {"me": ME, "reviews": {}, "mine": rows, "legacy": []}

    def test_needs_you_then_waiting_then_ready_and_drafts_collapse(self):
        cr = {"author": "mthomas46", "at": "2026-10-30T06:16:00Z", "url": "",
              "rerequested_at": "2026-10-30T22:40:00Z"}
        rows = [mine_row(1, approvals=2, updated="2026-11-03T00:00:00Z"), mine_row(2),
                mine_row(3, answered=[cr]),
                mine_row(4, waiting=True, threads=[woods_thread("question", "x")]),
                mine_row(5, draft=True)]
        s = menu.build_sections(self.data(rows), [], now=NOW)
        self.assertEqual([(e["row"]["number"], e["group"], e["color"]) for e in s["mine"]],
                         [(4, "needs_you", "red"), (3, "waiting", "yellow"),
                          (2, "waiting", None), (1, "ready", None)])
        self.assertEqual([e["row"]["number"] for e in s["mine_drafts"]], [5])
        self.assertEqual(s["count"], 2)

    def test_row_reads_ticket_then_the_action_then_title(self):
        cr = {"author": "mthomas46", "at": "2026-10-30T03:50:00Z", "url": "",
              "rerequested_at": "2026-10-30T23:15:00Z"}
        row = mine_row(9168, waiting=True, approvals=2, answered=[cr], threads=[
            woods_thread("suggestion", "add a test"), woods_thread("nit", "rename"),
            woods_thread("nit", "move the mock")])
        [e] = menu.build_sections(self.data([row]), [], nick={"plaxdan": "Woods"},
                                  now=NOW)["mine"]
        self.assertEqual(menu.row_text(e),
                         "🟡 ABC-9168 · Reply to Woods: 1 suggestion, 2 nits · 2d · thing 9168")
        self.assertEqual(e["why"], "Why it's yellow: 3 comments from Woods are unanswered; "
                                   "only nits and suggestions are waiting")
        waiting = mine_row(1, answered=[cr], approvals=1)
        [w] = menu.build_sections(self.data([waiting]), [], nick={"mthomas46": "Mykal"},
                                  now=NOW)["mine"]
        self.assertEqual(menu.row_text(w), "🟡 ABC-1 · Waiting on re-review: Mykal · 7d · thing 1")

    def test_sub_headers_appear_once_each_above_their_rows(self):
        rows = [mine_row(1, approvals=2), mine_row(2),
                mine_row(4, waiting=True, threads=[woods_thread("question", "x")])]
        s = menu.build_sections(self.data(rows), [], now=NOW)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            menu.mine_rows(s["mine"], {}, 2)
        top = [x.split(" | ")[0].strip("\u00a0") for x in out.getvalue().splitlines()
               if not x.startswith("--")]
        heads = [x for x in top if x in P.MINE_GROUPS.values()]
        self.assertEqual(heads, ["Needs you", "Waiting on others", "Ready"])

    def test_submenu_belongs_to_the_row_not_its_dismiss_alternate(self):
        row = mine_row(9168, waiting=True, approvals=2,
                       threads=[woods_thread("suggestion", "add a test")])
        s = menu.build_sections(self.data([row]), [], nick={"plaxdan": "Woods"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            menu.mine_block(s["mine"][0], {"plaxdan": "Woods"}, 2)
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("🟡 ABC-9168 · Reply to Woods: 1 suggestion"))
        dismiss = next(i for i, x in enumerate(lines) if "alternate=true" in x)
        last_child = max(i for i, x in enumerate(lines) if x.startswith("--"))
        self.assertGreater(dismiss, last_child)
        self.assertTrue(any(x.startswith("--Approvals: 2 of 2 (a1 ") for x in lines))
        self.assertTrue(any(x.startswith("--Suggestion · Woods · Types.elm — add a test")
                            for x in lines))
        self.assertTrue(any("Go through these with Claude" in x for x in lines))
        self.assertFalse(any(icon in out.getvalue() for icon in ("💬", "✏️", "⏳", "💡", "🔷")))

    def test_status_file_carries_each_comment_in_full(self):
        row = mine_row(9168, waiting=True, approvals=1,
                       threads=[woods_thread("nit", "rename"), woods_thread("suggestion", "test")])
        data = self.data([row])
        with tempfile.TemporaryDirectory() as tmp:
            status_report.write(menu.build_sections(data, []), data, {"plaxdan": "Woods"},
                                directory=Path(tmp))
            text = (Path(tmp) / "ui#9168.md").read_text()
        self.assertIn("**Menu section:** My PRs", text)
        self.assertIn("Approvals on the latest push: 1 of 2.", text)
        self.assertIn("Approvals: 1 of 2.", text)
        comments = text.split("## Comments to answer", 1)[1]
        self.assertLess(comments.index("1. Suggestion from Woods (plaxdan)"),
                        comments.index("2. Nit from Woods (plaxdan)"))
        self.assertIn("**Nit:** rename", comments)


def snapshot(reviews=(), threads=(), approved_by=(), changes_requested_by=()):
    return {"id": "PR1", "kind": "mine", "repo": "ui", "number": 9168,
            "title": "[ABC-5915] Show the exact total", "url": PR, "state": "OPEN",
            "author": ME, "head": "h1", "updated_at": "2026-11-04T17:00:00Z",
            "approved_by": list(approved_by), "changes_requested_by": list(changes_requested_by),
            "reviews": list(reviews), "issue_comments": [], "threads": list(threads)}


def w_review(rid, author, state, body=""):
    return {"id": rid, "state": state, "submitted_at": "2026-11-04T16:58:00Z", "body": body,
            "url": f"{PR}#pullrequestreview-{rid}", "author": author, "is_bot": False}


def w_thread(cid, body, author="plaxdan"):
    return {"id": f"T{cid}", "resolved": False, "outdated": False, "resolved_by": None,
            "path": "A.elm", "line": 1, "original_line": 1,
            "comments": [{"id": cid, "author": author, "is_bot": False,
                          "created_at": "2026-11-04T16:58:00Z", "url": f"{PR}#discussion_r{cid}",
                          "body": body, "commit": "c", "original_commit": "c"}]}


class ReapprovalHoldTest(unittest.TestCase):
    def test_latest_checked_reason_shows(self):
        jobs = [{"status": "ready", "finished_at": 1, "reapproval": "1 of your comments isn't addressed yet"},
                {"status": "ready", "finished_at": 2, "reapproval": "rev's change request is still unanswered"},
                {"status": "running"}]
        self.assertEqual(menu.reapproval_hold(jobs),
                         "Not re-approving: rev's change request is still unanswered")

    def test_nothing_when_off_or_unchecked(self):
        self.assertEqual(menu.reapproval_hold([{"status": "ready", "reapproval": "auto re-approval is off"}]), "")
        self.assertEqual(menu.reapproval_hold([{"status": "queued"}]), "")
        self.assertEqual(menu.reapproval_hold(
            [{"status": "ready", "reapproval": "your latest decision on the PR isn't an approval"}]), "")


class NotificationTest(unittest.TestCase):
    NICK = {"plaxdan": "Woods", "mthomas46": "Mykal"}

    def event(self, snap):
        _, st = ev.diff_pr(snapshot(), None, ev.Ctx(me=ME, seeding=True))
        [e], _ = ev.diff_pr(snap, st, ev.Ctx(me=ME))
        return e

    def test_one_alert_per_pr_titled_with_dot_ticket_and_reason_opens_claude(self):
        from watcher import __main__ as W
        e = self.event(snapshot(
            reviews=[w_review(1, "plaxdan", "APPROVED")],
            threads=[w_thread(11, "**Suggestion:** add a test"), w_thread(12, "**Nit:** rename")],
            approved_by=["josh-mills", "plaxdan"]))
        snap = {**snapshot(), "kind": "mine"}
        href = W.notification_href(snap, [], {"watcher": {}}, dry_run=True)
        n = render.render_pr(snap, "red", "Woods left 2 comments (1 suggestion, 1 nit)", [e],
                             self.NICK, href)
        self.assertEqual(n["title"], "🔴 ABC-5915 · Woods left 2 comments (1 suggestion, 1 nit)")
        self.assertEqual(n["subtitle"], "Show the exact total")
        self.assertEqual(n["body"], "Woods: add a test")
        self.assertTrue(n["href"].startswith("prdigest://respond?pr="))
        self.assertEqual(view_draft.validated_pr(n["href"], {"org": "avetta"}), PR)

    def test_followup_body_lists_each_response(self):
        pr_ref = {**snapshot(), "author": "mthomas46"}
        events = [{"type": "thread", "trigger": "reply", "actor": "mthomas46", "first_line": "ok",
                   "url": "nit-url", "blocking": False, "path": "A.elm"},
                  {"type": "thread", "trigger": "outdated", "actor": "mthomas46",
                   "first_line": "", "url": "blocking-url", "blocking": True, "path": "B.elm"}]
        n = render.render_pr(pr_ref, "red", "Mykal responded to 2 of your comments, 1 blocking",
                             events, self.NICK, "file:///page.html")
        self.assertEqual(n["body"], "Mykal replied: ok · the code changed under your comment on B.elm")


class RespondLinkTest(unittest.TestCase):
    CFG = {"org": "avetta"}

    def test_only_a_pull_request_in_the_org_is_accepted(self):
        import session
        self.assertEqual(view_draft.validated_pr(session.respond_url(PR), self.CFG), PR)
        for bad in ("https://github.com/evil/ui/pull/1", "https://example.com/avetta/ui/pull/1",
                    PR + "/files", PR + "&x=1"):
            with self.assertRaises(ValueError, msg=bad):
                view_draft.validated_pr(session.respond_url(bad), self.CFG)
        with self.assertRaises(ValueError):
            view_draft.validated_pr(f"prdigest://discuss?pr={PR}", self.CFG)

    def test_respond_prompt_points_at_the_status_file(self):
        import session
        prompt = session.respond_prompt(PR)
        self.assertIn(PR, prompt)
        self.assertIn("output/status/ui#9168.md", prompt)
        self.assertIn("Don't change code or post anything", prompt)


if __name__ == "__main__":
    unittest.main()
