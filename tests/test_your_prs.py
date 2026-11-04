"""Your PRs: comment kinds, approval order, the menu section, notifications, links."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import status_report
import swiftbar_plugin as menu
from review import view_draft
from triage import comment_kind as ck
from triage import mine
from watcher import events as ev
from watcher import render

ME = "me-login"
PR = "https://github.com/avetta/ui/pull/9168"


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
        }
        for body, want in cases.items():
            self.assertEqual(ck.kind(body), want, body)

    def test_summary_drops_the_label_and_emphasis(self):
        self.assertEqual(ck.summary("**Suggestion:** No test calls `f`.\n\n*(Drafted)*"),
                         "No test calls `f`.")

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
    return {"url": f"https://github.com/avetta/ui/pull/{num}", "title": f"[WS-{num}] thing {num}",
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

    def test_your_move_first_then_change_requests_then_waiting_then_approved(self):
        cr = {"author": "mthomas46", "at": "2026-10-30T06:16:00Z", "url": "",
              "rerequested_at": "2026-10-30T22:40:00Z"}
        rows = [mine_row(1, approvals=2), mine_row(2), mine_row(3, answered=[cr]),
                mine_row(4, waiting=True, threads=[woods_thread("nit", "x")]),
                mine_row(5, draft=True)]
        s = menu.build_sections(self.data(rows), [])
        self.assertEqual([(r["number"], r["state"]) for r in s["mine"]],
                         [(4, "move"), (3, "changes"), (2, "waiting"), (1, "approved")])
        self.assertEqual([r["number"] for r in s["mine_drafts"]], [5])
        self.assertEqual(s["count"], 1)

    def test_status_line_reads_like_the_pr(self):
        cr = {"author": "mthomas46", "at": "2026-10-30T03:50:00Z", "url": "",
              "rerequested_at": "2026-10-30T23:15:00Z"}
        row = mine_row(9168, waiting=True, approvals=2, answered=[cr], threads=[
            woods_thread("suggestion", "add a test"), woods_thread("nit", "rename"),
            woods_thread("nit", "move the mock")])
        line = menu.mine_status_line(row, {"mthomas46": "Mykal"}, 2)
        self.assertEqual(line, "💬 3 to answer (1 suggestion, 2 nits) · ✅ 2/2 approved · "
                               "✏️ Mykal requested changes, re-requested")

    def test_submenu_belongs_to_the_row_not_its_dismiss_alternate(self):
        row = mine_row(9168, waiting=True, approvals=2,
                       threads=[woods_thread("suggestion", "add a test")])
        s = menu.build_sections(self.data([row]), [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            menu.mine_block(s["mine"][0], {"plaxdan": "Woods"}, 2)
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("💬 WS-9168 · ui#9168"))
        dismiss = next(i for i, x in enumerate(lines) if "alternate=true" in x)
        last_child = max(i for i, x in enumerate(lines) if x.startswith("--"))
        self.assertGreater(dismiss, last_child)
        self.assertTrue(any(x.startswith("--✅ a2 approved · 2/2") for x in lines))
        self.assertTrue(any(x.startswith("--💡 Suggestion · Woods · Types.elm — add a test")
                            for x in lines))
        self.assertTrue(any("Go through these with Claude" in x for x in lines))

    def test_status_file_carries_each_comment_in_full(self):
        row = mine_row(9168, waiting=True, approvals=1,
                       threads=[woods_thread("nit", "rename"), woods_thread("suggestion", "test")])
        data = self.data([row])
        with tempfile.TemporaryDirectory() as tmp:
            status_report.write(menu.build_sections(data, []), data, {"plaxdan": "Woods"},
                                directory=Path(tmp))
            text = (Path(tmp) / "ui#9168.md").read_text()
        self.assertIn("**Menu section:** Your PRs", text)
        self.assertIn("Approvals: 1 of 2.", text)
        comments = text.split("## Comments to answer", 1)[1]
        self.assertLess(comments.index("1. Suggestion from Woods (plaxdan)"),
                        comments.index("2. Nit from Woods (plaxdan)"))
        self.assertIn("**Nit:** rename", comments)


def snapshot(reviews=(), threads=(), approved_by=(), changes_requested_by=()):
    return {"id": "PR1", "kind": "mine", "repo": "ui", "number": 9168,
            "title": "[WS-5915] Show the exact total", "url": PR, "state": "OPEN",
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


class NotificationTest(unittest.TestCase):
    NICK = {"plaxdan": "Woods", "mthomas46": "Mykal"}

    def event(self, snap):
        _, st = ev.diff_pr(snapshot(), None, ev.Ctx(me=ME, seeding=True))
        [e], _ = ev.diff_pr(snap, st, ev.Ctx(me=ME))
        return e

    def test_approval_with_comments_names_kinds_open_change_request_and_opens_claude(self):
        e = self.event(snapshot(
            reviews=[w_review(1, "plaxdan", "APPROVED")],
            threads=[w_thread(11, "**Suggestion:** add a test"), w_thread(12, "**Nit:** rename"),
                     w_thread(13, "**Nit:** move the mock")],
            approved_by=["josh-mills", "plaxdan"], changes_requested_by=["mthomas46"]))
        n = render.render(e, self.NICK)
        self.assertEqual(n["title"], "✅ Woods approved your PR · 2/2")
        self.assertEqual(n["body"], "3 comments to answer: 1 suggestion, 2 nits · "
                                    "Mykal's change request is still open")
        self.assertTrue(n["href"].startswith("prdigest://respond?pr="))
        self.assertEqual(view_draft.validated_pr(n["href"], {"org": "avetta"}), PR)

    def test_bare_approval_opens_the_review(self):
        e = self.event(snapshot(reviews=[w_review(1, "plaxdan", "APPROVED")],
                                approved_by=["plaxdan"]))
        n = render.render(e, self.NICK)
        self.assertEqual((n["title"], n["body"]), ("✅ Woods approved your PR · 1/2", "No comments"))
        self.assertEqual(n["href"], f"{PR}#pullrequestreview-1")

    def test_change_request_opens_claude(self):
        e = self.event(snapshot(reviews=[w_review(2, "mthomas46", "CHANGES_REQUESTED")],
                                threads=[w_thread(21, "Why a Set here?", "mthomas46")],
                                changes_requested_by=["mthomas46"]))
        n = render.render(e, self.NICK)
        self.assertEqual((n["title"], n["body"]),
                         ("✏️ Mykal requested changes", "1 comment to answer: 1 question"))
        self.assertTrue(n["href"].startswith("prdigest://respond?pr="))


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
