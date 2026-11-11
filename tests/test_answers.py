from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import swiftbar_plugin as menu
from triage import priority as P
from watcher import __main__ as W
from watcher import followup

ME = "me-login"
URL = "https://github.com/avetta/ui/pull/7"
NOW = "2026-11-10T18:00:00Z"
NICK = {"plaxdan": "Woods", "mthomas46": "Mykal"}


def row(threads=(), comments=(), blockers=()):
    return {"url": URL, "title": "[CONN-1234] Show the total", "waiting": True,
            "threads_waiting": list(threads), "unanswered_comments": list(comments),
            "blockers": list(blockers), "answered_change_requests": [], "approvals": [],
            "requested": []}


def thread(tid="T1", body="**Question:** why a Maybe here?", author="plaxdan",
           last_at="2026-11-10T17:00:00Z", bodies=None):
    bodies = bodies or [{"author": author, "body": body}]
    return {"thread_id": tid, "path": "src/Total.elm", "last_author": bodies[-1]["author"],
            "last_at": last_at, "url": f"{URL}#discussion_r{tid}", "outdated": False,
            "bodies": bodies, "author": author, "kind": "question", "summary": "why a Maybe here?"}


def top_comment(body="Can we add a screenshot?", author="mthomas46"):
    return {"author": author, "at": "2026-11-10T16:00:00Z", "body": body,
            "url": f"{URL}#issuecomment-9", "kind": "question", "summary": body}


def blocker(body="This breaks paging on the admin list.", author="plaxdan"):
    return {"author": author, "body": body, "at": "2026-11-10T15:00:00Z",
            "url": f"{URL}#pullrequestreview-5", "rerequested_at": ""}


def snap(threads=()):
    return {"id": "PR_7", "kind": "mine", "repo": "ui", "number": 7, "url": URL,
            "title": "[CONN-1234] Show the total", "author": ME, "head": "abc123",
            "threads": list(threads)}


class AsksTest(unittest.TestCase):
    def test_every_kind_of_comment_waiting_on_you_is_an_ask(self):
        asks = followup.answer_asks(row([thread()], [top_comment()], [blocker()]))
        self.assertEqual([a["where"] for a in asks], ["inline", "issue", "review"])
        self.assertEqual([a["actor"] for a in asks], ["plaxdan", "mthomas46", "plaxdan"])
        self.assertEqual(asks[2]["kind"], "blocking")

    def test_a_thread_answers_its_last_comment_with_the_thread_for_context(self):
        t = thread(bodies=[{"author": "plaxdan", "body": "Rename t?"},
                           {"author": ME, "body": "To what?"},
                           {"author": "plaxdan", "body": "total"}])
        [a] = followup.answer_asks(row([t]))
        self.assertEqual((a["body"], len(a["thread"])), ("total", 3))

    def test_a_new_reply_in_the_same_thread_is_a_new_job(self):
        a1 = followup.answer_asks(row([thread()]))[0]
        a2 = followup.answer_asks(row([thread(last_at="2026-11-10T19:00:00Z")]))[0]
        self.assertNotEqual(followup.answer_job_id(a1), followup.answer_job_id(a2))

    def test_a_change_request_with_no_body_has_nothing_to_answer(self):
        self.assertEqual(followup.answer_asks(row(blockers=[blocker(body="")])), [])

    def test_queue_skips_known_jobs_and_threads_being_fixed(self):
        asks = followup.answer_asks(row([thread("T1"), thread("T2")], [top_comment()]))
        known = [{"id": followup.answer_job_id(asks[2])}]
        todo = followup.answers_to_queue(snap(), asks, known, fixing_threads={"T2"})
        self.assertEqual([a["thread_id"] for a in todo], ["T1"])

    def test_a_job_whose_comment_stopped_waiting_clears(self):
        asks = followup.answer_asks(row([thread("T1")]))
        jobs = [{"id": followup.answer_job_id(asks[0]), "trigger": "answer", "status": "ready",
                 "pr": {"id": "PR_7"}},
                {"id": "old", "trigger": "answer", "status": "ready", "pr": {"id": "PR_7"}},
                {"id": "posted", "trigger": "answer", "status": "posted", "pr": {"id": "PR_7"}},
                {"id": "other-pr", "trigger": "answer", "status": "ready", "pr": {"id": "PR_8"}}]
        self.assertEqual(followup.stale_answers("PR_7", asks, jobs), [("old", "you answered it")])

    def test_an_inline_reply_posts_under_the_thread_root(self):
        asks = followup.answer_asks(row([thread()], [top_comment()]))
        inline = followup.answer_job(snap(), asks[0], 101, NOW)
        top = followup.answer_job(snap(), asks[1], 101, NOW)
        self.assertEqual((inline["root_id"], top["root_id"]), (101, None))
        self.assertEqual(followup.reply_endpoint("ui", 7, inline["root_id"]),
                         "repos/avetta/ui/pulls/7/comments/101/replies")
        self.assertEqual(followup.reply_endpoint("ui", 7, top["root_id"]),
                         "repos/avetta/ui/issues/7/comments")
        self.assertEqual((inline["trigger"], inline["status"]), ("answer", "queued"))
        json.dumps(inline)


class PromptAndResultTest(unittest.TestCase):
    def job(self):
        ask = followup.answer_asks(row([thread()]))[0]
        return followup.answer_job(snap(), ask, 101, NOW)

    def test_the_prompt_forbids_claiming_a_fix_and_carries_the_comment(self):
        prompt = followup.build_answer_prompt(self.job(), Path("/nonexistent"), ME)
        self.assertIn("@plaxdan left a question on @me-login's own pull request", prompt)
        self.assertIn("why a Maybe here?", prompt)
        self.assertIn("never say or imply that it's done", prompt)
        self.assertIn("an inline comment on src/Total.elm", prompt)

    def test_the_result_carries_a_stance_and_an_unknown_one_needs_you(self):
        r = followup.parse_answer_result(json.dumps(
            {"stance": "push_back", "summary": "s", "note": "n", "reply": "@plaxdan no"}))
        self.assertEqual((r["verdict"], r["note"]), ("push_back", "n"))
        r = followup.parse_answer_result('{"stance": "maybe", "reply": "x"}')
        self.assertEqual(r["verdict"], "needs_you")
        with self.assertRaises(ValueError):
            followup.parse_answer_result('{"stance": "answer", "reply": " "}')

    def test_the_report_says_the_stance_and_how_to_act(self):
        r = followup.parse_answer_result(json.dumps(
            {"stance": "agree", "summary": "asks for a rename", "note": "cheap", "reply": "@plaxdan sure"}))
        md = followup.answer_report(self.job(), r)
        self.assertIn("**Claude's draft agrees to change it.** cheap", md)
        self.assertIn("Drafted with Claude 🤖", md)
        self.assertIn("prdigest://respond?pr=", md)


class PlanTest(unittest.TestCase):
    def test_plan_queues_new_asks_and_clears_answered_ones(self):
        r = row([thread("T1"), thread("T2")])
        groups = {"PR_7": {"group": "needs_you", "reason": "", "asks": followup.answer_asks(r)}}
        threads = [{"id": "T1", "comments": [{"id": 101}]}, {"id": "T2", "comments": [{"id": 202}]}]
        fixing = [{"pr": {"id": "PR_7"}, "thread_id": "T2", "status": "running"}]
        stale = {"id": "gone", "trigger": "answer", "status": "queued", "pr": {"id": "PR_7"}}
        new, cleared = W.plan_answers({"PR_7": snap(threads)}, groups, [stale], fixing, NOW)
        self.assertEqual([(j["thread_id"], j["root_id"]) for j in new], [("T1", 101)])
        self.assertEqual(cleared, [("gone", "you answered it")])


class HoldTest(unittest.TestCase):
    def jobs(self, status):
        return [{"id": "a1", "trigger": "answer", "status": status, "pr": {"id": "PR_7"}}]

    def test_your_pr_waits_for_its_drafted_replies_then_goes_out_at_its_color(self):
        pending = {}
        note = (snap(), "red", "Woods asked a question", [])
        self.assertEqual(W.hold_for_drafts([note], pending, self.jobs("running"), NOW, NICK, ME,
                                           still_colored={"PR_7"}), [])
        json.dumps(pending)
        out = W.hold_for_drafts([], pending, self.jobs("ready"), NOW, NICK, ME,
                                still_colored={"PR_7"})
        self.assertEqual([(p["id"], c, r) for p, c, r, _, _ in out],
                         [("PR_7", "red", "Woods asked a question")])

    def test_your_pr_answered_while_waiting_is_dropped(self):
        pending = {}
        W.hold_for_drafts([(snap(), "red", "x", [])], pending, self.jobs("running"), NOW, NICK, ME,
                          still_colored={"PR_7"})
        self.assertEqual(W.hold_for_drafts([], pending, self.jobs("cleared"), NOW, NICK, ME,
                                           still_colored=set()), [])

    def test_the_popup_opens_the_drafts_once_ready(self):
        cfg = {"watcher": {"reports_dir": ""}}
        self.assertTrue(W.notification_href(snap(), self.jobs("failed"), cfg, True)
                        .startswith("prdigest://respond"))
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "r.md"
            report.write_text("# Reply\n\nDrafted reply")
            ready = [{**self.jobs("ready")[0], "report": str(report)}]
            self.assertTrue(W.notification_href(snap(), ready, cfg, True).startswith("(page of"))


class ClassifyTest(unittest.TestCase):
    def entries(self, status):
        jobs = [{"id": "a1", "trigger": "answer", "status": status, "pr": {"id": "PR_7", "url": URL}}]
        return P.classify([row([thread()])], {}, jobs, nick=NICK,
                          now=datetime(2026, 11, 10, 18, tzinfo=timezone.utc))

    def test_answer_jobs_ride_on_your_pr_and_never_become_follow_ups(self):
        out = self.entries("ready")
        self.assertEqual([(e["section"], len(e["answers"])) for e in out], [(P.MINE, 1)])
        self.assertTrue(out[0]["color"])

    def test_the_row_has_no_dot_while_claude_drafts(self):
        [e] = self.entries("running")
        self.assertIsNone(e["color"])
        self.assertTrue(e["reason"].startswith("Claude is drafting replies · "))


class MenuTest(unittest.TestCase):
    def test_each_comment_shows_its_draft_and_the_pr_can_post_them_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = row([thread()], [top_comment()])
            asks = followup.answer_asks(r)
            reply = Path(tmp) / "a.reply.md"
            reply.write_text("@plaxdan it can be absent.\n\nDrafted with Claude 🤖\n")
            jobs = [{**followup.answer_job(snap(), asks[0], 101, NOW), "status": "ready",
                     "verdict": "answer", "note": "the decoder allows null",
                     "reply_file": str(reply), "report": str(Path(tmp) / "a.md")},
                    {**followup.answer_job(snap(), asks[1], None, NOW), "status": "running"}]
            e = {"row": r, "url": URL, "title": r["title"], "color": "red", "reason": "x",
                 "answers": jobs, "autofix": []}
            buf = io.StringIO()
            with redirect_stdout(buf):
                menu.mine_block(e, NICK, 2)
        out = buf.getvalue()
        self.assertIn("· reply drafted, answers it", out)
        self.assertIn("· Claude is drafting a reply", out)
        self.assertIn("----Claude: the decoder allows null", out)
        self.assertIn("----Post this reply", out)
        self.assertIn(f"--Post the drafted reply | bash={menu.WATCHER} param1=post param2={jobs[0]['id']}", out)
        self.assertIn("----Read Claude's draft", out)


if __name__ == "__main__":
    unittest.main()
