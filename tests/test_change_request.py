"""Responses to your standing change request on someone else's PR: the watcher's
change_request event (watcher/events.py), its follow-up job and clearing
(watcher/followup.py), its red row (triage/priority.py), its one popup
(watcher/__main__.py, watcher/render.py), and the real-commits check (_real_work)."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

from triage import priority as P
from watcher import __main__ as W
from watcher import events as ev
from watcher import followup, render

ME, AUTHOR = "Josh-Suttenberg1", "dknight-avetta"
URL = "https://github.com/avetta/processservice/pull/870"
NICK = {AUTHOR: "DaVinci"}
NOW = datetime(2026, 11, 12, 18, 0, tzinfo=timezone.utc)
CR_AT = "2026-11-11T23:25:00Z"
CR_BODY = "**Blocking:** the v1 reads still have a caller in adminservice."


def rv(rid, author, state, at, body="", commit="c1", bot=False):
    return {"id": rid, "state": state, "submitted_at": at, "body": body, "url": f"r/{rid}",
            "commit": commit, "author": author, "is_bot": bot}


def ic(cid, author, at, body="Removed the caller, see c2.", bot=False):
    return {"id": cid, "author": author, "is_bot": bot, "created_at": at, "url": f"ic/{cid}",
            "body": body}


def snap(reviews=(), issue=(), head="c1", threads=()):
    return {"id": "PR870", "kind": "others", "repo": "processservice", "number": 870,
            "title": "[CONN-2293] Retire three v1 reads", "url": URL, "state": "OPEN",
            "author": AUTHOR, "head": head, "updated_at": "2026-11-12T10:00:00Z",
            "approved_by": [], "reviews": list(reviews), "issue_comments": list(issue),
            "threads": list(threads), "review_requests": []}


MY_CR = rv(5866691736, ME, "CHANGES_REQUESTED", CR_AT, CR_BODY)


def ctx(real=True, **kw):
    return ev.Ctx(me=ME, real_work=lambda repo, n, base, head: real, **kw)


def seeded(reviews=(MY_CR,), **kw):
    """The PR's saved state after a silent first tick."""
    _, state = ev.diff_pr(snap(reviews=reviews, **kw), None, ctx(seeding=True))
    return state


def cr_events(events):
    return [e for e in events if e["type"] == "change_request"]


class ChangeRequestEventTest(unittest.TestCase):
    def test_seeding_arms_silently(self):
        events, state = ev.diff_pr(snap(reviews=[MY_CR], head="c2"), None, ctx(seeding=True))
        self.assertEqual(events, [])
        self.assertEqual(state["cr"]["review_id"], MY_CR["id"])
        self.assertEqual(state["cr"]["base"], "c1")
        self.assertIn("push:c2", state["cr"]["fired"])

    def test_real_commits_since_your_change_request_fire(self):
        state = seeded()
        [e] = cr_events(ev.diff_pr(snap(reviews=[MY_CR], head="c2"), state, ctx())[0])
        self.assertEqual((e["trigger"], e["triggers"], e["actor"]), ("pushed", ["pushed"], AUTHOR))
        self.assertEqual((e["arm_commit"], e["head"], e["my_comment"]), ("c1", "c2", CR_BODY))
        self.assertTrue(e["blocking"])

    def test_a_base_merge_or_whitespace_commit_does_not(self):
        state = seeded()
        events, state = ev.diff_pr(snap(reviews=[MY_CR], head="c2"), state, ctx(real=False))
        self.assertEqual(cr_events(events), [])
        # and it isn't asked about again on the next tick
        asked = []
        ev.diff_pr(snap(reviews=[MY_CR], head="c2"), state,
                   ev.Ctx(me=ME, real_work=lambda *a: asked.append(a) or True))
        self.assertEqual(asked, [])

    def test_unknown_counts_as_real(self):
        state = seeded()
        events, _ = ev.diff_pr(snap(reviews=[MY_CR], head="c2"), state, ctx(real=None))
        self.assertEqual(len(cr_events(events)), 1)

    def test_a_top_level_comment_from_the_author_fires(self):
        state = seeded()
        [e] = cr_events(ev.diff_pr(snap(reviews=[MY_CR], issue=[ic(1, AUTHOR, "2026-11-12T09:00:00Z")]),
                                   state, ctx())[0])
        self.assertEqual((e["trigger"], e["url"]), ("commented", "ic/1"))
        self.assertEqual(e["first_line"], "Removed the caller, see c2.")

    def test_a_review_body_from_the_author_fires(self):
        state = seeded()
        reply = rv(77, AUTHOR, "COMMENTED", "2026-11-12T09:00:00Z", "Kept v1 on purpose: …")
        [e] = cr_events(ev.diff_pr(snap(reviews=[MY_CR, reply]), state, ctx())[0])
        self.assertEqual(e["trigger"], "review")

    def test_push_and_comment_in_one_tick_make_one_event(self):
        state = seeded()
        [e] = cr_events(ev.diff_pr(snap(reviews=[MY_CR], head="c2",
                                        issue=[ic(1, AUTHOR, "2026-11-12T09:00:00Z")]),
                                   state, ctx())[0])
        self.assertEqual(e["triggers"], ["pushed", "commented"])
        self.assertEqual(len(e["responses"]), 2)

    def test_each_response_fires_once_and_a_later_one_fires_again(self):
        state = seeded()
        s1 = snap(reviews=[MY_CR], head="c2")
        events, state = ev.diff_pr(s1, state, ctx())
        self.assertEqual(len(cr_events(events)), 1)
        self.assertEqual(cr_events(ev.diff_pr(s1, state, ctx())[0]), [])
        s2 = snap(reviews=[MY_CR], head="c3")
        [e] = cr_events(ev.diff_pr(s2, state, ctx())[0])
        self.assertEqual(e["head"], "c3")
        self.assertEqual(e["arm_commit"], "c1")     # still judged from your change request

    def test_bots_other_people_and_mentions_of_you_dont_fire_it(self):
        state = seeded()
        issue = [ic(1, "avettabot", "2026-11-12T09:00:00Z", bot=True),
                 ic(2, "mthomas46", "2026-11-12T09:00:00Z"),
                 ic(3, AUTHOR, "2026-11-12T09:00:00Z", body=f"@{ME} fixed, take a look")]
        events, _ = ev.diff_pr(snap(reviews=[MY_CR], issue=issue), state, ctx())
        self.assertEqual(cr_events(events), [])
        self.assertEqual([e["type"] for e in events], ["mention"])

    def test_nothing_while_your_latest_decision_is_an_approval(self):
        approved = [MY_CR, rv(2, ME, "APPROVED", "2026-11-12T08:00:00Z", commit="c2")]
        state = seeded(reviews=approved)
        self.assertIsNone(state["cr"])
        events, _ = ev.diff_pr(snap(reviews=approved, head="c3",
                                    issue=[ic(1, AUTHOR, "2026-11-12T09:00:00Z")]), state, ctx())
        self.assertEqual(cr_events(events), [])

    def test_a_dismissed_change_request_disarms(self):
        state = seeded()
        dismissed = {**MY_CR, "state": "DISMISSED"}
        events, state = ev.diff_pr(snap(reviews=[dismissed], head="c2"), state, ctx())
        self.assertEqual((cr_events(events), state["cr"]), ([], None))

    def test_your_own_new_comment_rearms_so_older_responses_dont_count(self):
        early = ic(1, AUTHOR, "2026-11-12T09:00:00Z")
        state = seeded(issue=[early])
        mine = ic(2, ME, "2026-11-12T10:00:00Z", body="Still missing the adminservice caller.")
        events, state = ev.diff_pr(snap(reviews=[MY_CR], issue=[early, mine]), state, ctx())
        self.assertEqual(cr_events(events), [])
        self.assertEqual(state["cr"]["since"], "2026-11-12T10:00:00Z")
        later = ic(3, AUTHOR, "2026-11-12T11:00:00Z")
        [e] = cr_events(ev.diff_pr(snap(reviews=[MY_CR], issue=[early, mine, later]), state,
                                   ctx())[0])
        self.assertEqual(e["url"], "ic/3")

    def test_a_pr_saved_before_this_was_tracked_reports_its_backlog(self):
        state = seeded()
        state.pop("cr")
        old = ic(1, AUTHOR, "2026-11-12T09:00:00Z")
        [e] = cr_events(ev.diff_pr(snap(reviews=[MY_CR], issue=[old]), state,
                                   ctx(cutoff="2026-11-12T17:00:00Z"))[0])
        self.assertEqual(e["trigger"], "commented")

    def test_first_sight_keeps_the_cutoff_for_words(self):
        old = ic(1, AUTHOR, "2026-11-12T09:00:00Z")
        events, _ = ev.diff_pr(snap(reviews=[MY_CR], issue=[old]), None,
                               ctx(cutoff="2026-11-12T17:00:00Z"))
        self.assertEqual(cr_events(events), [])


def cr_job(jid="j1", at="2026-11-12T09:01:00+00:00", status="queued", triggers=("pushed",),
           verdict=None, review_id=MY_CR["id"]):
    j = {"id": jid, "status": status, "trigger": "change_request", "created_at": at,
         "actor": AUTHOR, "triggers": list(triggers), "review_id": review_id,
         "thread_id": None, "blocking": True, "my_comment": CR_BODY,
         "pr": {"id": "PR870", "url": URL, "title": "[CONN-2293] Retire three v1 reads",
                "author": AUTHOR, "repo": "processservice", "number": 870}}
    if verdict:
        j["verdict"] = verdict
    return j


class ChangeRequestClearingTest(unittest.TestCase):
    def test_it_stands_until_you_act(self):
        self.assertEqual(followup.cleared_jobs(snap(reviews=[MY_CR]), [cr_job()], ME), [])

    def test_a_new_review_clears_it(self):
        again = [MY_CR, rv(2, ME, "COMMENTED", "2026-11-12T10:00:00Z", "Better, one thing left.")]
        self.assertEqual(followup.cleared_jobs(snap(reviews=again), [cr_job()], ME),
                         [("j1", "you submitted a new review")])
        approved = [MY_CR, rv(2, ME, "APPROVED", "2026-11-12T10:00:00Z")]
        self.assertEqual(followup.cleared_jobs(snap(reviews=approved), [cr_job()], ME),
                         [("j1", "you submitted a new review")])

    def test_dismissing_your_change_request_clears_it(self):
        dismissed = {**MY_CR, "state": "DISMISSED"}
        self.assertEqual(followup.cleared_jobs(snap(reviews=[dismissed]), [cr_job()], ME),
                         [("j1", "your change request was dismissed")])

    def test_your_top_level_comment_clears_it(self):
        s = snap(reviews=[MY_CR], issue=[ic(9, ME, "2026-11-12T10:00:00Z", body="Not yet.")])
        self.assertEqual(followup.cleared_jobs(s, [cr_job()], ME),
                         [("j1", "you commented on the PR")])

    def test_a_newer_response_replaces_the_older_job(self):
        jobs = [cr_job("old", status="ready"), cr_job("new", at="2026-11-12T11:00:00+00:00")]
        self.assertEqual(followup.cleared_jobs(snap(reviews=[MY_CR]), jobs, ME),
                         [("old", "a newer response replaced it")])

    def test_a_running_job_is_left_for_the_next_tick(self):
        jobs = [cr_job("old", status="running"), cr_job("new", at="2026-11-12T11:00:00+00:00")]
        self.assertEqual(followup.cleared_jobs(snap(reviews=[MY_CR]), jobs, ME), [])

    def test_job_id_is_per_change_request_and_response(self):
        e = {"type": "change_request", "review_id": 1, "response_key": "push:c2"}
        self.assertEqual(followup.event_job_id(e), followup.cr_job_id(1, "push:c2"))
        self.assertNotEqual(followup.cr_job_id(1, "push:c2"), followup.cr_job_id(1, "push:c3"))

    def test_the_prompt_judges_the_change_request_body(self):
        job = {**cr_job(triggers=("pushed", "commented")), "arm_commit": "c1" * 4, "head": "c3" * 4,
               "my_comment_url": "r/5866691736",
               "my_inline": [{"path": "V1Routes.scala", "line": 12, "body": "Still used."}],
               "responses": [{"trigger": "pushed", "body": ""},
                             {"trigger": "commented", "body": "Removed it.", "url": "ic/1"}]}
        rng = {"rebased": False, "commits": "abc DaVinci: drop caller", "stat": "1 file",
               "file_diff": "diff"}
        text = followup.build_cr_prompt(job, rng, "/clone", ME)
        self.assertIn(CR_BODY, text)
        self.assertIn("pushed commits and commented on the PR", text)
        self.assertIn("V1Routes.scala:12: Still used.", text)
        self.assertIn("Removed it.", text)


class ChangeRequestRowTest(unittest.TestCase):
    def classify(self, jobs, dismissed=None, bucket="needs_you",
                 reason="you requested changes @c1; the author has pushed since"):
        st = {"pr_url": URL, "title": "[CONN-2293] Retire three v1 reads", "author": AUTHOR,
              "bucket": bucket, "reason": reason, "tier": 0,
              "mine": ("CHANGES_REQUESTED", "c1"), "commented": True,
              "my_review": (CR_AT, "c1"), "approvals": ["mthomas46"], "engaged": [],
              "rerequested_at": "", "tags": []}
        full = {"needs_you": [], "author": [], "well_approved": [], "done": [], "unreadable": []}
        full[bucket] = [st]
        [e] = P.classify([], full, list(jobs), nick=NICK, now=NOW, dismissed=dismissed)
        return e

    def test_without_a_response_job_it_waits_on_the_author(self):
        e = self.classify([])
        self.assertEqual((e["group"], e["color"]), (P.WAITING, None))

    def test_a_push_makes_it_a_red_follow_up(self):
        e = self.classify([cr_job()])
        self.assertEqual((e["section"], e["group"], e["color"]), (P.FOLLOWUP, P.NEEDS, P.RED))
        self.assertEqual(e["reason"], "DaVinci pushed commits since your change request · "
                                      "1 of 2 approvals")
        self.assertEqual(e["why"], "Why it's red: DaVinci pushed commits while your change "
                                   "request stands")

    def test_a_comment_and_a_verdict(self):
        e = self.classify([cr_job(status="ready", triggers=("pushed", "commented"),
                                  verdict="partly")],
                          reason="you requested changes; the author commented on the PR")
        self.assertEqual(e["reason"], "DaVinci pushed commits and commented since your change "
                                      "request · Claude: partly addressed · 1 of 2 approvals")

    def test_a_review_body_counts(self):
        e = self.classify([cr_job(triggers=("review",))])
        self.assertEqual(e["reason"], "DaVinci answered in a review since your change request · "
                                      "1 of 2 approvals")

    def test_dismissing_the_row_clears_it_until_a_newer_response(self):
        e = self.classify([cr_job()], dismissed={URL: "2026-11-12T09:30:00+00:00"})
        self.assertEqual((e["color"], e["dismissed"]), (None, True))
        e = self.classify([cr_job(), cr_job("j2", at="2026-11-12T10:00:00+00:00")],
                          dismissed={URL: "2026-11-12T09:30:00+00:00"})
        self.assertEqual(e["color"], P.RED)

    def test_cleared_jobs_drop_it_back_to_waiting(self):
        e = self.classify([cr_job(status="cleared")])
        self.assertEqual((e["group"], e["color"]), (P.WAITING, None))

    def test_followup_color(self):
        self.assertEqual(P.followup_color([], False, change_request=True), P.RED)
        self.assertIsNone(P.followup_color([], False))


def cr_event(trigger="pushed", key="push:c2", line=""):
    return {"type": "change_request", "trigger": trigger, "triggers": [trigger],
            "pr": {"id": "PR870", "repo": "processservice", "number": 870, "url": URL,
                   "title": "[CONN-2293] Retire three v1 reads", "author": AUTHOR, "head": "c2"},
            "actor": AUTHOR, "review_id": MY_CR["id"], "response_key": key, "blocking": True,
            "url": f"{URL}/files", "first_line": line, "my_comment": CR_BODY}


def by_jobs(pr, jobs):
    f = P.followup([j for j in jobs if j["pr"]["id"] == pr["id"]], NICK, author=pr["author"])
    return (f["color"], f["reason"]) if f else (None, "")


class ChangeRequestNotificationTest(unittest.TestCase):
    def tick(self, events, jobs=(), notified=None, cleared=None, now="2026-11-12T18:00:00Z"):
        notified = {} if notified is None else notified
        notes = W.notifications({"PR870": snap()}, events, list(jobs), notified,
                                cleared or {}, now, by_jobs)
        return notes, notified

    def test_one_red_popup_and_no_second_for_the_next_response(self):
        notes, notified = self.tick([cr_event()])
        self.assertEqual([c for _, c, _, _ in notes], ["red"])
        notes, _ = self.tick([cr_event("commented", "ic:1", "Done.")], [cr_job()], notified)
        self.assertEqual(notes, [])

    def test_after_a_menu_dismissal_a_new_response_alerts_again(self):
        _, notified = self.tick([cr_event()])
        notes, _ = self.tick([cr_event("commented", "ic:1")], [cr_job()], notified,
                             cleared={URL: "2026-11-12T18:30:00Z"}, now="2026-11-12T18:31:00Z")
        self.assertEqual(len(notes), 1)

    def test_after_your_new_review_the_next_response_alerts_again(self):
        _, notified = self.tick([cr_event()])
        _, notified = self.tick([], [], notified)            # job cleared: the row has no color
        self.assertNotIn("PR870", notified)
        notes, _ = self.tick([cr_event("pushed", "push:c3")], [], notified)
        self.assertEqual(len(notes), 1)

    def test_popup_words(self):
        self.assertEqual(render.trigger([cr_event()], NICK, "CONN-2293"),
                         "DaVinci pushed commits since your change request on CONN-2293")
        self.assertEqual(render.trigger([cr_event("commented", "ic:1", "Done.")], NICK, "CONN-2293"),
                         "DaVinci answered your change request on CONN-2293")

    def test_a_dry_run_classifies_this_ticks_event_as_a_job(self):
        [j] = W.event_jobs([cr_event()], [], "2026-11-12T18:00:00Z")
        self.assertEqual((j["trigger"], j["review_id"]), ("change_request", MY_CR["id"]))
        self.assertEqual(W.event_jobs([cr_event()], [j], "2026-11-12T18:00:00Z"), [])


class RealWorkTest(unittest.TestCase):
    def check(self, commits, work=True):
        calls = []

        def new_work(repo, number, base, head, passes=None):
            calls.append((repo, number, base, head, passes))
            return work
        with mock.patch.object(W.gh_prs, "_get", return_value={"commits": commits}), \
                mock.patch("triage.reviews._new_work_since", new_work):
            out = W._real_work("tok", "avetta", ME)("processservice", 870, "c1", "c2")
        return out, calls

    def test_only_your_own_commits_are_not_news(self):
        out, calls = self.check([{"sha": "A1", "author": {"login": ME}}])
        self.assertEqual((out, calls), (False, []))

    def test_others_commits_go_through_the_real_commits_rule_without_yours(self):
        out, calls = self.check([{"sha": "A1", "author": {"login": ME}},
                                 {"sha": "B2", "author": {"login": AUTHOR}}])
        self.assertTrue(out)
        [(repo, number, base, head, passes)] = calls
        self.assertEqual((repo, number, base, head), ("avetta/processservice", 870, "c1", "c2"))
        self.assertTrue(passes(None, "a1"))
        self.assertFalse(passes(None, "b2"))

    def test_github_unreadable_is_unknown(self):
        with mock.patch.object(W.gh_prs, "_get", side_effect=W.gh_prs.DigestError("x")):
            self.assertIsNone(W._real_work("tok", "avetta", ME)("processservice", 870, "c1", "c2"))


if __name__ == "__main__":
    unittest.main()
