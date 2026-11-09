from __future__ import annotations

import contextlib
import io
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone

import swiftbar_plugin as menu
from triage import priority as P

NOW = datetime(2026, 11, 6, 20, 0, tzinfo=timezone.utc)
URL = "https://github.com/acme/api/pull/7"
NICK = {"plaxdan": "Woods"}


def ask(tid, kind="nit", author="plaxdan"):
    return {"thread_id": tid, "author": author, "last_author": author, "kind": kind,
            "last_at": "2026-11-06T18:00:00Z", "summary": f"{kind} {tid}", "path": "a.py",
            "url": f"{URL}#{tid}", "bodies": [{"author": author, "body": f"Nit: {tid}"}]}


def row(*threads, approvals=()):
    return {"url": URL, "title": "ABC-123: total the cart", "repo": "api", "number": 7,
            "isDraft": False, "waiting": bool(threads), "threads_waiting": list(threads),
            "unanswered_comments": [], "blockers": [], "answered_change_requests": [],
            "approvals": list(approvals), "requested": []}


def fix(tid, status, **kw):
    base = {"id": f"j{tid}", "status": status, "thread_id": tid, "reviewer": "plaxdan",
            "kind": "nit", "path": "a.py", "line": 3, "comment_url": f"{URL}#{tid}",
            "pr": {"id": "PR1", "url": URL, "title": "ABC-123: total the cart"}}
    return {**base, **kw}


def at(hours_ago: float) -> int:
    return int((NOW - timedelta(hours=hours_ago)).timestamp())


class OverlayTest(unittest.TestCase):
    def mine(self, r, jobs):
        return P.mine(r, NICK, now=NOW, autofix=jobs)

    def test_a_comment_being_fixed_doesnt_count(self):
        p = self.mine(row(ask("T1")), [fix("T1", "running")])
        self.assertEqual((p["group"], p["color"]), (P.NEEDS, None))
        self.assertEqual(p["reason"], "Claude is fixing Woods's nit · 0 of 2 approvals")
        p = self.mine(row(ask("T1"), ask("T2", "suggestion")), [fix("T1", "queued")])
        self.assertEqual(p["color"], P.YELLOW)
        self.assertIn("Reply to Woods: 1 suggestion · Claude is fixing Woods's nit", p["reason"])

    def test_an_escalation_turns_the_row_red_with_its_reason(self):
        p = self.mine(row(ask("T1")), [fix("T1", "escalated", reason="validation failed",
                                            escalated_at="2026-11-06T19:00:00Z")])
        self.assertEqual(p["color"], P.RED)
        self.assertTrue(p["reason"].startswith("Couldn't auto-fix: validation failed · Reply to Woods: 1 nit"))
        self.assertIn("Claude couldn't auto-fix Woods's nit: validation failed", p["why"])
        self.assertEqual(p["activity_at"], "2026-11-06T19:00:00Z")

    def test_dry_runs_and_finished_fixes_change_nothing(self):
        plain = self.mine(row(ask("T1")), [])
        for job in (fix("T1", "running", dry_run=True), fix("T1", "escalated", dry_run=True),
                    fix("T1", "dry_run", dry_run=True), fix("T1", "done")):
            p = self.mine(row(ask("T1")), [job])
            self.assertEqual((p["color"], p["reason"]), (plain["color"], plain["reason"]))


class DoneForYouTest(unittest.TestCase):
    def test_todays_fixes_and_reapprovals_one_row_per_pr(self):
        fixes = [fix("T1", "done", done_at=at(1), commit_sha="a1b2c3d4", resolved=True),
                 fix("T2", "done", done_at=at(2), commit_sha="b2c3d4e5", resolved=True),
                 fix("T3", "done", done_at=at(30), commit_sha="c" * 8, resolved=True),
                 fix("T4", "done", done_at=at(1), done_dismissed_at=at(0)),
                 fix("T5", "escalated")]
        reapproved = [{"id": "f1", "status": "reapproved", "actor": "dknight-avetta",
                       "reapproved_at": at(3), "posted_url": "x",
                       "pr": {"url": "https://github.com/acme/web/pull/9", "title": "ABC-9: x"}}]
        [a, b] = P.done_for_you(fixes, reapproved, NICK, NOW)
        self.assertEqual(a["url"], URL)
        self.assertEqual(a["reason"], "Fixed 2 of Woods's nits · pushed 2 commits, replied, resolved")
        self.assertEqual(b["reason"], "Re-approved: dknight-avetta did what you asked")
        self.assertEqual((a["color"], a["section"]), (P.DONE, P.DONE_SECTION))
        [one] = P.done_for_you(fixes[:1], [], NICK, NOW)
        self.assertEqual(one["reason"], "Fixed Woods's nit · pushed a1b2c3d, replied, resolved")

    def test_never_counted(self):
        data = {"me": "me", "reviews": {}, "mine": [], "legacy": []}
        s = menu.build_sections(data, [], now=NOW,
                                autofix_jobs=[fix("T1", "done", done_at=at(1), commit_sha="a" * 8)])
        self.assertEqual(len(s["done"]), 1)
        self.assertEqual((s["title"], s["count"]), ("✓", 0))


class MenuRenderTest(unittest.TestCase):
    def render(self, fn, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                unittest.mock.patch.object(menu, "_reviews_on", return_value=False), \
                unittest.mock.patch.object(menu, "_status_prompt", return_value="p"):
            fn(*args)
        return out.getvalue().splitlines()

    def test_the_red_row_and_the_auto_fix_submenu(self):
        data = {"me": "me", "reviews": {}, "mine": [row(ask("T1"))], "legacy": []}
        jobs = [fix("T1", "escalated", reason="Claude disagrees with the comment",
                    steps=[{"step": "assess", "ok": False, "detail": "Claude disagrees"}])]
        s = menu.build_sections(data, [], nick=NICK, now=NOW, autofix_jobs=jobs)
        self.assertEqual(s["title"], "🔴1")
        lines = self.render(menu.mine_rows, s["mine_view"], NICK, 2)
        self.assertTrue(lines[1].startswith("🔴 ABC-123 · Couldn't auto-fix: Claude disagrees"))
        self.assertTrue(any("couldn’t auto-fix: Claude disagrees" in x for x in lines
                            if x.startswith("--Nit")))
        self.assertIn("--Auto-fix (1) | color=#888888", lines)
        self.assertTrue(any(x.startswith("----✗ assess") for x in lines))

    def test_done_for_you_sits_before_nothing_needed_with_its_proof(self):
        done = fix("T1", "done", done_at=at(1), commit_sha="a1b2c3d4e5", resolved=True,
                   summary="renamed t", reply_text="Done — renamed t in [a1b2c3d](x).\n\nDrafted with Claude 🤖\n",
                   reply_url=f"{URL}#discussion_r9", rerequested=True)
        data = {"me": "me", "legacy": [], "mine": [],
                "reviews": {"well_approved": [{"pr_url": "https://github.com/acme/api/pull/3",
                                               "title": "ABC-3: x", "author": "dev", "tags": [],
                                               "bucket": "well_approved", "approvals": ["a", "b"],
                                               "reason": "2 approvals"}]}}
        s = menu.build_sections(data, [], nick=NICK, now=NOW, autofix_jobs=[done])
        with unittest.mock.patch.object(menu, "_org", return_value="acme"):
            lines = self.render(menu.others_rows, s, NICK)
        top = [x.split(" | ")[0] for x in lines if not x.startswith("--")]
        self.assertLess(top.index("Done for you"), top.index("Nothing needed (1)"))
        row_line = next(x for x in lines if x.startswith("✅ ABC-123"))
        self.assertIn("Fixed Woods's nit · pushed a1b2c3d, replied, resolved", row_line)
        self.assertIn("----Commit a1b2c3d | href=https://github.com/acme/api/commit/a1b2c3d4e5", lines)
        self.assertTrue(any(x.startswith("----Done — renamed t") for x in lines))
        self.assertTrue(any(x.startswith("----Open the resolved thread") for x in lines))
        dismiss = next(x for x in lines if "Dismiss · ABC-123" in x)
        self.assertIn("param1=--dismiss-done param2=a:jT1", dismiss)

    def test_dismiss_done_marks_each_kind_of_job(self):
        from watcher import autofix, followup
        with unittest.mock.patch.object(autofix, "dismiss_done") as a, \
                unittest.mock.patch.object(followup, "dismiss_done") as f:
            menu.dismiss_done("a:j1,f:f1,a:j2")
        self.assertEqual([c.args[0] for c in a.call_args_list], ["j1", "j2"])
        self.assertEqual([c.args[0] for c in f.call_args_list], ["f1"])


if __name__ == "__main__":
    unittest.main()
