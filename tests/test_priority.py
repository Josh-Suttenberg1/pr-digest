"""triage/priority.py rules, and the watcher's one-notification-per-PR rule built on it."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

import notify
from triage import priority as P
from watcher import __main__ as W
from watcher import events as ev
from watcher import followup

ME = "me-login"
NOW = datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc)
NICK = {"mthomas46": "Mykal", "plaxdan": "Woods", "kylerscharpen": "Kyler"}


def url(n):
    return f"https://github.com/avetta/process-domain/pull/{n}"


def st(n, **kw):
    """A review_triage state."""
    base = {"pr_url": url(n), "title": f"ABC-{n}: thing {n}", "author": "mthomas46",
            "bucket": "needs_you", "reason": "not reviewed yet", "tier": 1, "mine": None,
            "commented": False, "approvals": [], "engaged": [], "rerequested_at": "",
            "waiting_since": "2026-11-04T15:00:00Z", "tags": []}
    return {**base, **kw}


def job(n, jid, *, blocking=False, trigger="reply", status="ready", at="2026-11-04T16:00:00Z",
        actor="mthomas46", body="please rename"):
    return {"id": jid, "status": status, "created_at": at, "trigger": trigger, "actor": actor,
            "blocking": blocking, "my_comment": body, "thread_id": f"T{jid}", "arm_id": 1,
            "pr": {"id": f"PR{n}", "url": url(n), "title": f"ABC-{n}: thing {n}",
                   "author": "mthomas46"}}


def classify(reviews=None, jobs=(), mine=(), dismissed=None):
    full = {"needs_you": [], "author": [], "well_approved": [], "done": [], "unreadable": []}
    full.update(reviews or {})
    out = P.classify(list(mine), full, list(jobs), nick=NICK, now=NOW, dismissed=dismissed)
    return {e["url"]: e for e in out}


class FollowupRulesTest(unittest.TestCase):
    def test_a_rerequest_is_red_even_when_every_comment_was_non_blocking(self):
        s = st(1, commented=True, rerequested_at="2026-11-04T17:00:00Z",
               reason="you commented; you were re-requested")
        e = classify({"needs_you": [s]}, [job(1, "a", blocking=False)])[url(1)]
        self.assertEqual((e["section"], e["color"]), (P.FOLLOWUP, P.RED))
        self.assertEqual(e["reason"], "Mykal re-requested your review and answered your comment · "
                                     "0 of 2 approvals")

    def test_a_rerequest_is_red_whatever_your_earlier_decision(self):
        s = st(1, mine=("APPROVED", "abc"), bucket="well_approved",
               rerequested_at="2026-11-04T17:00:00Z")
        e = classify({"well_approved": [s]})[url(1)]
        self.assertEqual((e["section"], e["color"]), (P.FOLLOWUP, P.RED))

    def test_blocking_and_non_blocking_responses_make_one_red_row(self):
        s = st(1, commented=True, reason="you commented; the author replied on your thread")
        entries = classify({"needs_you": [s]}, [job(1, "a", blocking=True),
                                                job(1, "b", blocking=False)])
        self.assertEqual(len(entries), 1)
        e = entries[url(1)]
        self.assertEqual((e["section"], e["color"], len(e["jobs"])), (P.FOLLOWUP, P.RED, 2))
        self.assertEqual(e["reason"], "Mykal answered 2 of your comments, 1 blocking · "
                                      "0 of 2 approvals")
        self.assertEqual(e["why"], "Why it's red: Mykal replied to your blocking comment")

    def test_non_blocking_responses_are_yellow_even_after_you_approved(self):
        s = st(1, mine=("APPROVED", "abc"), bucket="done", reason="you approved")
        e = classify({"done": [s]}, [job(1, "a", trigger="outdated", body="**Nit:** x")])[url(1)]
        self.assertEqual((e["section"], e["color"]), (P.FOLLOWUP, P.YELLOW))
        self.assertEqual(e["reason"], "Mykal changed the code under your comment · 0 of 2 approvals")

    def test_commits_touching_none_of_your_threads_go_to_everything_else(self):
        s = st(1, commented=True, reason="you commented @abc12345, real commits landed since",
               tier=0)
        e = classify({"needs_you": [s]})[url(1)]
        self.assertEqual((e["section"], e["group"], e["color"]), (P.OTHER, P.WAITING, None))
        self.assertEqual(e["reason"], "The author pushed commits that touch none of your "
                                      "comments · 0 of 2 approvals")

    def test_answered_or_dismissed_jobs_no_longer_count(self):
        s = st(1, commented=True, bucket="author", reason="no new commits")
        e = classify({"author": [s]}, [job(1, "a", status="posted"),
                                       job(1, "b", status="cleared")])[url(1)]
        self.assertEqual(e["section"], P.OTHER)

    def test_old_jobs_without_a_blocking_field_use_the_comment_tag(self):
        j = job(1, "a")
        del j["blocking"]
        j["my_comment"] = "**Blocking:** this drops data"
        self.assertTrue(P.job_blocking(j))

    def test_blocking_definition(self):
        self.assertTrue(P.is_blocking("blocking", "COMMENTED"))
        self.assertTrue(P.is_blocking("comment", "CHANGES_REQUESTED"))
        self.assertTrue(P.is_blocking("question", "CHANGES_REQUESTED"))
        self.assertFalse(P.is_blocking("nit", "CHANGES_REQUESTED"))
        self.assertFalse(P.is_blocking("suggestion", "CHANGES_REQUESTED"))
        self.assertFalse(P.is_blocking("comment", "COMMENTED"))


class OwedRulesTest(unittest.TestCase):
    def test_new_request_is_green(self):
        e = classify({"needs_you": [st(1, waiting_since="2026-11-03T20:00:00Z")]})[url(1)]
        self.assertEqual((e["section"], e["color"]), (P.OWED, P.GREEN))
        self.assertEqual(e["reason"], "Review it · 0 of 2 approvals · 1d")

    def test_stalled_review_is_yellow(self):
        s = st(1, waiting_since="2026-11-01T12:00:00Z", engaged=["kylerscharpen"])
        e = classify({"needs_you": [s]})[url(1)]
        self.assertEqual(e["color"], P.YELLOW)
        self.assertEqual(e["reason"], "Review it · 0 of 2 approvals · 3d")
        self.assertEqual(e["why"], "Why it's yellow: requested 3 days ago · no approvals · "
                                   "only Kyler has reviewed or commented")

    def test_the_stale_gap_more_than_two_days_but_two_engaged_is_green(self):
        s = st(1, waiting_since="2026-11-01T12:00:00Z", engaged=["kylerscharpen", "plaxdan"])
        e = classify({"needs_you": [s]})[url(1)]
        self.assertEqual(e["color"], P.GREEN)
        self.assertEqual(e["reason"], "Review it · 0 of 2 approvals · 3d · Kyler and Woods "
                                      "are on it")

    def test_two_days_exactly_is_not_yet_stale_and_enough_approvals_never_is(self):
        self.assertEqual(classify({"needs_you": [st(1, waiting_since="2026-11-02T18:00:00Z")]})
                         [url(1)]["color"], P.GREEN)
        s = st(1, waiting_since="2026-10-20T00:00:00Z", approvals=["a", "b"])
        self.assertEqual(classify({"needs_you": [s]})[url(1)]["color"], P.GREEN)

    def test_a_dismissed_review_lands_in_reviews_you_owe(self):
        s = st(1, mine=("DISMISSED", "abc"), reason="your earlier review was dismissed", tier=0)
        e = classify({"needs_you": [s]})[url(1)]
        self.assertEqual(e["section"], P.OWED)
        self.assertTrue(e["reason"].startswith("Your review was dismissed"))

    def test_a_rereview_never_lands_in_reviews_you_owe(self):
        s = st(1, mine=("CHANGES_REQUESTED", "abc"), tier=0,
               reason="you requested changes; the author has pushed since")
        self.assertEqual(classify({"needs_you": [s]})[url(1)]["section"], P.OTHER)

    def test_parked_first_review_is_everything_else(self):
        s = st(1, tier=P.PARKED_TIER, tags=["CI failing: build"])
        e = classify({"needs_you": [s]})[url(1)]
        self.assertEqual((e["section"], e["reason"]), (P.OTHER, "Parked: CI failing (build)"))


def mine_row(n, waiting=False, **kw):
    base = {"url": url(n), "title": f"ABC-{n}: mine {n}", "isDraft": False, "waiting": waiting,
            "threads_waiting": [], "unanswered_comments": [], "blockers": [],
            "answered_change_requests": [], "approvals": [], "requested": []}
    return {**base, **kw}


def appr(login, at="2026-11-03T18:00:00Z", on_head=True):
    return {"author": login, "at": at, "url": "", "on_head": on_head}


def ask(kind, author="plaxdan", at="2026-11-02T18:00:00Z"):
    return {"author": author, "kind": kind, "last_at": at}


def mine_p(row):
    return P.mine(row, NICK, now=NOW)


class MineNeedsYouTest(unittest.TestCase):
    def test_only_nits_and_suggestions_are_yellow(self):
        p = mine_p(mine_row(1, True, threads_waiting=[ask("nit"), ask("suggestion")]))
        self.assertEqual((p["group"], p["color"]), (P.NEEDS, P.YELLOW))
        self.assertEqual(p["reason"], "Reply to Woods: 1 suggestion, 1 nit · 0 of 2 approvals · 2d")
        self.assertIn("only nits and suggestions", p["why"])

    def test_a_question_a_blocking_or_an_unlabeled_comment_is_red(self):
        for kind in ("question", "blocking", "comment"):
            p = mine_p(mine_row(1, True, threads_waiting=[ask("nit"), ask(kind)]))
            self.assertEqual((kind, p["color"]), (kind, P.RED))

    def test_change_request_not_yet_rerequested_is_red(self):
        row = mine_row(1, True, blockers=[{"author": "mthomas46", "at": "2026-11-04T10:00:00Z"}],
                       threads_waiting=[ask("question", "mthomas46", "2026-11-04T10:00:00Z")])
        p = mine_p(row)
        self.assertEqual((p["group"], p["color"]), (P.NEEDS, P.RED))
        self.assertEqual(p["reason"], "Answer Mykal's change request: 1 question · 0 of 2 approvals · today")

    def test_comments_from_others_than_the_change_requester_get_their_own_part(self):
        row = mine_row(1, True, blockers=[{"author": "mthomas46", "at": "2026-11-04T10:00:00Z"}],
                       threads_waiting=[ask("nit")])
        self.assertEqual(mine_p(row)["reason"],
                         "Answer Mykal's change request · Reply to Woods: 1 nit · 0 of 2 approvals · 2d")


class MineNotOnYouTest(unittest.TestCase):
    def test_ready_is_the_required_approvals_with_no_change_request(self):
        p = mine_p(mine_row(1, approvals=[appr("plaxdan"), appr("kylerscharpen")]))
        self.assertEqual((p["group"], p["color"], p["reason"]),
                         (P.READY, None, "Ready: 2 of 2 approvals"))

    def test_old_approvals_still_count_where_the_repo_does_not_require_a_fresh_one(self):
        row = mine_row(1, approvals=[appr("plaxdan", on_head=False),
                                     appr("kylerscharpen", on_head=False)])
        self.assertEqual(mine_p(row)["reason"], "Ready: 2 of 2 approvals")

    def test_a_repo_needing_a_latest_push_approval_says_so_without_a_dot(self):
        row = mine_row(1, last_push_required=True,
                       approvals=[appr("plaxdan", on_head=False),
                                  appr("kylerscharpen", on_head=False)])
        p = mine_p(row)
        self.assertEqual((p["group"], p["color"]), (P.READY, None))
        self.assertEqual(p["reason"],
                         "Ready · re-approve the latest push before merge · 2 of 2 approvals")
        self.assertEqual(p["why"], "Why: GitHub needs an approval of the latest push here, "
                                   "and none is on it yet")

    def test_one_approval_on_the_latest_push_is_enough_for_that_rule(self):
        row = mine_row(1, last_push_required=True,
                       approvals=[appr("plaxdan", on_head=False), appr("kylerscharpen")])
        self.assertEqual(mine_p(row)["reason"], "Ready: 2 of 2 approvals")

    def test_waiting_on_review_names_who_and_since_when(self):
        row = mine_row(1, approvals=[appr("plaxdan")], requested=["mthomas46", "kylerscharpen"],
                       requested_since={"mthomas46": "2026-11-04T15:00:00Z",
                                        "kylerscharpen": "2026-11-04T16:00:00Z"})
        p = mine_p(row)
        self.assertEqual((p["group"], p["color"]), (P.WAITING, None))
        self.assertEqual(p["reason"], "Waiting on review: Mykal and Kyler · 1 of 2 approvals · today")

    def test_a_wait_past_two_days_turns_yellow_as_a_nudge(self):
        row = mine_row(1, requested=["mthomas46"],
                       requested_since={"mthomas46": "2026-11-01T12:00:00Z"})
        p = mine_p(row)
        self.assertEqual((p["group"], p["color"]), (P.WAITING, P.YELLOW))
        self.assertEqual(p["reason"], "Waiting on review: Mykal · 0 of 2 approvals · 3d")
        self.assertEqual(p["why"], "Why it's yellow: waiting 3 days, time to nudge Mykal")

    def test_two_days_exactly_is_not_yet_stale(self):
        row = mine_row(1, requested=["mthomas46"],
                       requested_since={"mthomas46": "2026-11-02T18:00:00Z"})
        self.assertIsNone(mine_p(row)["color"])

    def test_waiting_on_re_review_says_who_since_when_and_why(self):
        cr = {"author": "mthomas46", "at": "2026-10-30T06:00:00Z",
              "rerequested_at": "2026-11-03T20:00:00Z"}
        p = mine_p(mine_row(1, answered_change_requests=[cr], approvals=[appr("plaxdan")]))
        self.assertEqual((p["group"], p["color"]), (P.WAITING, None))
        self.assertEqual(p["reason"], "Waiting on re-review: Mykal · 1 of 2 approvals · 1d")
        self.assertEqual(p["why"], "Why: Mykal requested changes 5 days ago and you "
                                   "re-requested them yesterday")

    def test_a_re_requested_approver_is_a_re_review(self):
        row = mine_row(1, approvals=[appr("plaxdan"), appr("kylerscharpen")],
                       requested=["plaxdan"], requested_since={"plaxdan": "2026-11-04T17:00:00Z"})
        p = mine_p(row)
        self.assertEqual(p["reason"], "Waiting on re-review: Woods · 2 of 2 approvals · today")
        self.assertEqual(p["why"], "Why: you re-requested Woods after their approval")

    def test_no_reviewer_requested_and_drafts_have_no_age(self):
        self.assertEqual(mine_p(mine_row(1))["reason"], "No reviewer requested · 0 of 2 approvals")
        self.assertEqual(mine_p(mine_row(1, isDraft=True))["reason"],
                         "GitHub draft, nothing to answer")

    def test_age_is_today_or_days(self):
        self.assertEqual(P.age("2026-11-04T15:00:00Z", NOW), "today")
        self.assertEqual(P.age("2026-10-30T15:00:00Z", NOW), "5d")
        self.assertEqual(P.age("", NOW), "")


class OrderAndTitleTest(unittest.TestCase):
    def test_title_counts_each_color_and_shows_a_check_when_empty(self):
        es = [{"color": P.RED}, {"color": P.RED}, {"color": P.YELLOW}, {"color": P.GREEN},
              {"color": None}]
        self.assertEqual(P.title(es), "🔴2 🟡1")
        self.assertEqual(P.title([{"color": P.GREEN}]), "✓")      # new requests don't count
        self.assertEqual(P.title([{"color": None}]), "✓")

    def test_red_then_yellow_then_green_then_none_longest_wait_first(self):
        es = [{"id": 1, "color": None, "waiting_since": "2026-01-01"},
              {"id": 2, "color": P.YELLOW, "waiting_since": "2026-01-05"},
              {"id": 3, "color": P.RED, "waiting_since": "2026-01-09"},
              {"id": 4, "color": P.RED, "waiting_since": "2026-01-02"}]
        self.assertEqual([e["id"] for e in sorted(es, key=P.sort_key)], [4, 3, 2, 1])


# --- watcher ---------------------------------------------------------------------------


def snap(pid, kind="others"):
    return {"id": pid, "kind": kind, "repo": "process-domain", "number": 1, "url": url(1),
            "title": "ABC-1: thing", "author": "mthomas46", "head": "h"}


def thread_ev(pid, tid, blocking):
    return {"type": "thread", "pr": snap(pid), "thread_id": tid, "trigger": "reply",
            "actor": "mthomas46", "blocking": blocking, "url": "u", "arm_id": 1}


def by_jobs(pr, jobs):
    """The watcher's follow-up classification without a re-request."""
    f = P.followup([j for j in jobs if j["pr"]["id"] == pr["id"]], NICK, author=pr["author"])
    return (f["color"], f["reason"]) if f else (None, "")


class NotificationRuleTest(unittest.TestCase):
    def run_tick(self, events, jobs=(), notified=None, cleared=None, now="2026-11-04T18:00:00Z"):
        notified = {} if notified is None else notified
        notes = W.notifications({"PR1": snap("PR1")}, events, list(jobs), notified,
                                cleared or {}, now, by_jobs)
        return notes, notified

    def test_one_notification_per_pr_per_tick_at_its_highest_color(self):
        notes, notified = self.run_tick([thread_ev("PR1", "T1", False),
                                         thread_ev("PR1", "T2", True)])
        self.assertEqual([(pr["id"], color) for pr, color, _, _ in notes], [("PR1", "red")])
        self.assertEqual(len(notes[0][3]), 2)
        self.assertEqual(notified["PR1"]["color"], "red")

    def test_same_or_lower_color_does_not_pop_up_again_but_a_rise_does(self):
        _, notified = self.run_tick([thread_ev("PR1", "T1", False)])
        self.assertEqual(notified["PR1"]["color"], "yellow")
        open_job = job(1, "j1")
        open_job["pr"]["id"] = "PR1"
        open_job["thread_id"] = "T1"
        notes, _ = self.run_tick([thread_ev("PR1", "T3", False)], [open_job], notified)
        self.assertEqual(notes, [])
        notes, _ = self.run_tick([thread_ev("PR1", "T4", True)], [open_job], notified)
        self.assertEqual([c for _, c, _, _ in notes], ["red"])

    def test_once_the_row_clears_the_next_response_notifies_afresh(self):
        _, notified = self.run_tick([thread_ev("PR1", "T1", True)])
        _, notified = self.run_tick([], [], notified)        # Josh answered: no open job
        self.assertNotIn("PR1", notified)
        notes, _ = self.run_tick([thread_ev("PR1", "T5", True)], [], notified)
        self.assertEqual(len(notes), 1)

    def test_a_menu_dismissal_resets_the_stored_color(self):
        _, notified = self.run_tick([thread_ev("PR1", "T1", True)])
        open_job = {**job(1, "j1", blocking=True), "thread_id": "T1"}
        open_job["pr"] = {**open_job["pr"], "id": "PR1"}
        notes, _ = self.run_tick([thread_ev("PR1", "T2", True)], [open_job], notified,
                                 cleared={url(1): "2026-11-04T18:30:00Z"},
                                 now="2026-11-04T18:31:00Z")
        self.assertEqual(len(notes), 1)

    def test_uncolored_activity_never_notifies(self):
        approval = {"type": "decision", "state": "APPROVED", "pr": snap("PR1", "mine"),
                    "actor": "plaxdan", "count": 0, "first_line": ""}
        notes = W.notifications({"PR1": snap("PR1", "mine")}, [approval], [], {}, {},
                                "2026-11-04T18:00:00Z", lambda pr, js: (None, ""))
        self.assertEqual(notes, [])

    def test_seeding_records_colors_without_notifying(self):
        notified = {}
        notes = W.notifications({"PR1": snap("PR1")}, [thread_ev("PR1", "T1", True)], [],
                                notified, {}, "2026-11-04T18:00:00Z", by_jobs, seed=True)
        self.assertEqual((notes, notified["PR1"]["color"]), ([], "red"))


def wpr(reviews=(), threads=(), requests=(), issue=()):
    return {"id": "PR1", "kind": "others", "repo": "process-domain", "number": 1,
            "title": "ABC-1: thing", "url": url(1), "state": "OPEN", "author": "mthomas46",
            "head": "h1", "updated_at": "2026-11-04T10:00:00Z", "approved_by": [],
            "reviews": list(reviews), "issue_comments": list(issue), "threads": list(threads),
            "review_requests": list(requests)}


def wc(cid, author, body="x", at="2026-11-04T09:00:00Z", review_state="COMMENTED"):
    return {"id": cid, "author": author, "is_bot": False, "created_at": at, "url": f"u/{cid}",
            "body": body, "commit": "c", "original_commit": "c", "review_id": 9,
            "review_state": review_state}


def wthread(tid, comments, resolved=False):
    return {"id": tid, "resolved": resolved, "outdated": False, "resolved_by": None,
            "path": "A.scala", "line": 3, "original_line": 3, "comments": comments}


def wreview(author, state, at, body=""):
    return {"id": 1, "state": state, "submitted_at": at, "body": body, "url": "r",
            "author": author, "is_bot": False}


class WatcherEventsTest(unittest.TestCase):
    def test_a_comment_in_a_change_request_review_arms_a_blocking_thread(self):
        t = wthread("T1", [wc(1, ME, "This drops the region filter.",
                              review_state="CHANGES_REQUESTED")])
        _, state = ev.diff_pr(wpr(threads=[t]), None, ev.Ctx(me=ME, seeding=True))
        self.assertTrue(state["threads"]["T1"]["blocking"])
        t2 = wthread("T1", t["comments"] + [wc(2, "mthomas46", "fixed")])
        [e], _ = ev.diff_pr(wpr(threads=[t2]), state, ev.Ctx(me=ME))
        self.assertTrue(e["blocking"])
        nit = wthread("T2", [wc(3, ME, "**Nit:** rename", review_state="CHANGES_REQUESTED")])
        self.assertFalse(ev.thread_blocking(nit, ME))

    def test_a_rerequest_after_your_review_fires_once(self):
        mine = [wreview(ME, "COMMENTED", "2026-11-03T10:00:00Z", "see comments")]
        _, state = ev.diff_pr(wpr(reviews=mine), None, ev.Ctx(me=ME, seeding=True))
        req = {"id": "RR1", "at": "2026-11-04T10:00:00Z", "by": "mthomas46", "reviewer": ME}
        [e], state = ev.diff_pr(wpr(reviews=mine, requests=[req]), state, ev.Ctx(me=ME))
        self.assertEqual((e["type"], e["actor"]), ("rerequest", "mthomas46"))
        self.assertEqual(ev.diff_pr(wpr(reviews=mine, requests=[req]), state, ev.Ctx(me=ME))[0], [])
        self.assertEqual(ev.pending_rerequest(wpr(reviews=mine, requests=[req]), ME),
                         "2026-11-04T10:00:00Z")
        again = mine + [wreview(ME, "APPROVED", "2026-11-04T11:00:00Z")]
        self.assertEqual(ev.pending_rerequest(wpr(reviews=again, requests=[req]), ME), "")

    def test_a_first_request_is_not_a_rerequest(self):
        req = {"id": "RR1", "at": "2026-11-04T10:00:00Z", "by": "mthomas46", "reviewer": ME}
        self.assertEqual(ev.pending_rerequest(wpr(requests=[req]), ME), "")


class ClearedJobsTest(unittest.TestCase):
    def jobs(self):
        return [{**job(1, "a"), "thread_id": "T1", "arm_id": 1, "created_at": "2026-11-04T10:00:00Z"},
                {**job(1, "b"), "thread_id": "T2", "arm_id": 5, "created_at": "2026-11-04T10:00:00Z"},
                {**job(1, "c", status="running"), "thread_id": "T3", "arm_id": 7}]

    def fix(self, jobs):
        for j in jobs:
            j["pr"] = {**j["pr"], "id": "PR1"}
        return jobs

    def test_replying_or_resolving_clears_that_thread_only(self):
        t1 = wthread("T1", [wc(1, ME), wc(2, "mthomas46"), wc(3, ME, "thanks")])
        t2 = wthread("T2", [wc(5, ME), wc(6, "mthomas46")])
        out = followup.cleared_jobs(wpr(threads=[t1, t2]), self.fix(self.jobs()), ME)
        self.assertEqual(out, [("a", "you replied in the thread")])
        t2r = wthread("T2", [wc(5, ME), wc(6, "mthomas46")], resolved=True)
        out = followup.cleared_jobs(wpr(threads=[t1, t2r]), self.fix(self.jobs()), ME)
        self.assertEqual(sorted(out), [("a", "you replied in the thread"),
                                       ("b", "the thread is resolved")])

    def test_a_new_review_clears_every_open_job_but_not_a_running_one(self):
        rv = [wreview(ME, "APPROVED", "2026-11-04T12:00:00Z")]
        out = followup.cleared_jobs(wpr(reviews=rv), self.fix(self.jobs()), ME)
        self.assertEqual([jid for jid, _ in out], ["a", "b"])
        bare = [wreview(ME, "COMMENTED", "2026-11-04T12:00:00Z")]   # a lone thread reply
        self.assertEqual(followup.cleared_jobs(wpr(reviews=bare), self.fix(self.jobs()), ME), [])


class SoundTest(unittest.TestCase):
    def test_a_pr_alert_is_silent_in_swiftbar_and_plays_its_own_sound(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return mock.Mock(returncode=0)
        notify.alert("🔴 ABC-1 · x", "t", "b", href="https://x", run=run,
                     sound_file="/System/Library/Sounds/Glass.aiff")
        self.assertEqual(calls[0][:2], ["pgrep", "-x"])
        self.assertIn("silent=true", calls[1][2])
        self.assertEqual(calls[2], ["/usr/bin/afplay", "/System/Library/Sounds/Glass.aiff"])

    def test_sounds_off_plays_nothing(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return mock.Mock(returncode=0)
        cfg = {"notify": {"sounds": False, "red_sound": "/x.aiff", "alert_sound": "Glass"}}
        notify.alert("🔴 ABC-1 · x", "t", "b", run=run, **notify.sound_options(cfg, "red"))
        self.assertIn("silent=true", calls[1][2])
        self.assertEqual(len(calls), 2)

    def test_sound_options_by_color_and_for_notices(self):
        cfg = {"notify": {"red_sound": "/x.aiff", "alert_sound": "Glass"}}
        self.assertEqual(notify.sound_options(cfg, "red"), {"sound_file": "/x.aiff"})
        self.assertEqual(notify.sound_options(cfg), {"sound": "Glass"})



# --- @mentions, drafts before popups -----------------------------------------

import json  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402


class MentionTest(unittest.TestCase):
    def test_a_mention_fires_once_and_not_inside_your_own_thread(self):
        pr0 = wpr()
        _, state = ev.diff_pr(pr0, None, ev.Ctx(me=ME, seeding=True))
        ask = {"id": 50, "author": "kylerscharpen", "is_bot": False,
               "created_at": "2026-11-04T12:00:00Z", "url": "u/50", "body": f"@{ME} is this right?"}
        mine_thread = wthread("T1", [wc(1, ME, "why?"), wc(2, "mthomas46", f"@{ME} see above")])
        p = wpr(issue=[ask], threads=[mine_thread])
        events, state = ev.diff_pr(p, state, ev.Ctx(me=ME))
        mentions = [e for e in events if e["type"] == "mention"]
        self.assertEqual([(e["actor"], e["where"]) for e in mentions], [("kylerscharpen", "issue")])
        again, _ = ev.diff_pr(p, state, ev.Ctx(me=ME))
        self.assertEqual([e for e in again if e["type"] == "mention"], [])

    def test_a_mention_makes_a_red_row_even_on_a_pr_you_never_reviewed(self):
        j = {**job(1, "m1", body=""), "trigger": "mention", "actor": "kylerscharpen"}
        e = classify({"needs_you": [st(1)]}, [j])[url(1)]
        self.assertEqual((e["section"], e["color"], e["reason"]),
                         (P.FOLLOWUP, P.RED, "Kyler mentioned you · 0 of 2 approvals"))

    def test_answering_a_top_level_mention_clears_it(self):
        j = {**job(1, "m1"), "trigger": "mention", "where": "issue", "comment_id": 50,
             "thread_id": None, "mentioned_at": "2026-11-04T12:00:00Z"}
        j["pr"] = {**j["pr"], "id": "PR1"}
        self.assertEqual(followup.cleared_jobs(wpr(), [j], ME), [])
        reply = {"id": 51, "author": ME, "is_bot": False, "created_at": "2026-11-04T13:00:00Z",
                 "url": "u/51", "body": "yes"}
        self.assertEqual(followup.cleared_jobs(wpr(issue=[reply]), [j], ME),
                         [("m1", "you answered the mention")])


class HoldForDraftsTest(unittest.TestCase):
    NOW = "2026-11-04T18:00:00Z"

    def pr(self, kind="others"):
        return {**snap("PR1", kind), "reviews": [], "issue_comments": [], "threads": []}

    def jobs(self, status):
        j = {**job(1, "j1", blocking=True, status=status), "thread_id": "T1"}
        j["pr"] = {**j["pr"], "id": "PR1"}
        return [j]

    def test_a_followup_waits_until_its_draft_is_written(self):
        pending = {}
        note = (self.pr(), "red", "Mykal answered your blocking comment", [thread_ev("PR1", "T1", True)])
        out = W.hold_for_drafts([note], pending, self.jobs("running"), self.NOW, NICK, ME)
        self.assertEqual((out, list(pending)), ([], ["PR1"]))
        json.dumps(pending)                       # stored in state.json
        out = W.hold_for_drafts([], pending, self.jobs("running"), "2026-11-04T18:02:00Z", NICK, ME)
        self.assertEqual(out, [])
        out = W.hold_for_drafts([], pending, self.jobs("ready"), "2026-11-04T18:04:00Z", NICK, ME)
        self.assertEqual([(p["id"], c, r) for p, c, r, _, _ in out],
                         [("PR1", "red", "Mykal answered your blocking comment")])
        self.assertEqual(pending, {})

    def test_a_draft_that_never_comes_goes_out_after_the_wait(self):
        pending = {}
        note = (self.pr(), "red", "x", [thread_ev("PR1", "T1", True)])
        W.hold_for_drafts([note], pending, self.jobs("queued"), self.NOW, NICK, ME)
        out = W.hold_for_drafts([], pending, self.jobs("queued"), "2026-11-04T18:31:00Z", NICK, ME)
        self.assertEqual(len(out), 1)

    def test_a_row_cleared_while_waiting_is_dropped(self):
        pending = {}
        note = (self.pr(), "red", "x", [thread_ev("PR1", "T1", True)])
        W.hold_for_drafts([note], pending, self.jobs("running"), self.NOW, NICK, ME)
        self.assertEqual(W.hold_for_drafts([], pending, self.jobs("dismissed"), self.NOW, NICK, ME), [])

    def test_your_own_prs_and_bare_rerequests_go_out_at_once(self):
        note = (self.pr("mine"), "red", "Woods left 1 comment", [])
        self.assertEqual(len(W.hold_for_drafts([note], {}, [], self.NOW, NICK, ME)), 1)
        self.assertEqual(len(W.hold_for_drafts([(self.pr(), "red", "re-requested", [])], {}, [],
                                               self.NOW, NICK, ME)), 1)

    def test_the_link_always_exists(self):
        cfg = {"watcher": {"reports_dir": ""}}
        pr = self.pr()
        self.assertEqual(W.notification_href(pr, [], cfg, True), pr["url"])
        failed = [{**self.jobs("failed")[0], "url": "https://github.com/c/1"}]
        self.assertEqual(W.notification_href(pr, failed, cfg, True), "https://github.com/c/1")
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "r.md"
            report.write_text(f"# Follow-up\n\n🔗 {url(1)}\n\nDrafted reply")
            ready = [{**self.jobs("ready")[0], "report": str(report)}]
            from review import view_draft
            with mock.patch.object(view_draft, "OUT_DIR", Path(tmp) / "html"):
                href = W.notification_href(pr, ready, cfg, False)
            self.assertTrue(href.startswith("file://"))
            self.assertTrue(Path(href[len("file://"):]).exists())


if __name__ == "__main__":
    unittest.main()
