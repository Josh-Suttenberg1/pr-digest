"""A teammate's PR counts as a review request, and a review you owe pops up while
automatic drafts are off."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import settings
from review import auto_draft as A
from triage import priority as P
from triage import reviews as R
from watcher import __main__ as W
from watcher import gh, popups


def an_hour_ago() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


NOW = datetime(2026, 11, 11, 18, 0, tzinfo=timezone.utc)
TEAM = ["plaxdan", "mthomas46"]
TICKETS = ["WS", "CONN"]


def pr_url(n, repo="ui"):
    return f"https://github.com/avetta/{repo}/pull/{n}"


class TeamPrTest(unittest.TestCase):
    def test_a_teammate_with_a_ticket_key_counts(self):
        self.assertTrue(P.team_pr("plaxdan", "[WS-5850] Supplier settings", TEAM, TICKETS))
        self.assertTrue(P.team_pr("mthomas46", "CONN-1880: escape params", TEAM, TICKETS))

    def test_no_key_another_project_or_not_a_teammate_doesnt(self):
        self.assertFalse(P.team_pr("mthomas46", "feat(state): tracking", TEAM, TICKETS))
        self.assertFalse(P.team_pr("mthomas46", "ABC-12: other project", TEAM, TICKETS))
        self.assertFalse(P.team_pr("mthomas46", "XWS-12: not a WS key", TEAM, TICKETS))
        self.assertFalse(P.team_pr("jackhp95", "[CONN-1271] x", TEAM, TICKETS))
        self.assertFalse(P.team_pr("", "[CONN-1271] x", TEAM, TICKETS))

    def test_no_ticket_keys_counts_every_title(self):
        self.assertTrue(P.team_pr("mthomas46", "Update README.md", TEAM, []))

    def test_the_search_ors_the_authors_and_leaves_you_out(self):
        self.assertEqual(P.team_search("avetta", "me", ["plaxdan", "me", "mthomas46"]),
                         "is:pr is:open org:avetta draft:false author:mthomas46 author:plaxdan")
        self.assertEqual(P.team_search("avetta", "me", []), "")


class CandidatesTest(unittest.TestCase):
    def setUp(self):
        self.patches = [mock.patch.object(R, "TEAM", set(TEAM)),
                        mock.patch.object(R, "TEAM_TICKETS", tuple(TICKETS)),
                        mock.patch.object(R, "ORG", "avetta")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def fake_gh(self, args):
        if args[:2] == ["gh", "api"]:
            self.team_query = args[args.index("-f") + 1]
            item = lambda n, who, title, draft=False: {  # noqa: E731
                "html_url": pr_url(n), "title": title, "user": {"login": who}, "draft": draft,
                "repository_url": "https://api.github.com/repos/avetta/ui"}
            return {"items": [item(9125, "plaxdan", "[WS-5850] settings"),
                              item(1091, "mthomas46", "feat(state): tracking"),
                              item(9300, "mthomas46", "[WS-1] wip", draft=True),
                              item(9221, "mthomas46", "[WS-5844] permission")]}
        if "--review-requested=@me" in args:
            return [{"url": pr_url(9221), "title": "[WS-5844] permission",
                     "author": {"login": "mthomas46"},
                     "repository": {"nameWithOwner": "avetta/ui"}, "isDraft": False}]
        return []

    def test_teammates_prs_with_a_key_join_the_requested_ones(self):
        with mock.patch.object(R, "_gh_json", self.fake_gh):
            found = R.candidates("me", [])
        self.assertEqual(sorted(found), [pr_url(9125), pr_url(9221)])
        self.assertEqual(found[pr_url(9125)]["repository"]["nameWithOwner"], "avetta/ui")
        self.assertEqual(found[pr_url(9125)]["author"]["login"], "plaxdan")
        self.assertIn("author:mthomas46 author:plaxdan", self.team_query)

    def review_state(self, requested):
        pull = {"state": "open", "draft": False, "title": "[WS-5850] settings",
                "user": {"login": "plaxdan"}, "head": {"sha": "abc"},
                "requested_reviewers": [{"login": "me"}] if requested else [],
                "requested_teams": [], "created_at": "2026-10-21T15:18:11Z"}
        answers = iter([pull, []])
        with mock.patch.object(R, "_gh_json", lambda args: next(answers)):
            return R._review_state(pr_url(9125), {"repository": {"nameWithOwner": "avetta/ui"}},
                                   "me", [])

    def test_team_ask_only_when_github_doesnt_request_you(self):
        self.assertTrue(self.review_state(requested=False)["team_ask"])
        self.assertFalse(self.review_state(requested=True)["team_ask"])

    def test_a_teammates_pr_ranks_like_a_direct_request(self):
        st = {**self.review_state(requested=False), "lines_changed": 500}
        with mock.patch.object(R, "_ci_failing", return_value=[]), \
                mock.patch.object(R, "_waiting_since", return_value=an_hour_ago()):
            R._rank(st)
        self.assertIn("your teammate's PR", st["tags"])
        # +3 for a teammate's PR, plus under half a point for the wait (real clock).
        self.assertGreaterEqual(st["score"], 3)
        self.assertLess(st["score"], 3.5)


class OwedRowTest(unittest.TestCase):
    def test_the_hover_text_says_why_it_counts(self):
        st = {"pr_url": pr_url(9125), "author": "plaxdan", "approvals": ["mthomas46"],
              "engaged": ["mthomas46"], "waiting_since": "2026-10-21T15:18:11Z",
              "team_ask": True}
        row = P.owed(st, {"plaxdan": "Woods"}, NOW)
        self.assertEqual(row["color"], P.YELLOW)
        self.assertTrue(row["why"].startswith(
            "Why it's yellow: Woods is your teammate, so it counts as asking you though GitHub "
            "doesn't request you · ready for review 21 days ago"))
        self.assertIn("requested", P.owed({**st, "team_ask": False}, {}, NOW)["why"])


def cfg(**review):
    c = settings._merge(settings.DEFAULTS, {"me": "me", "review": {"auto_draft": False, **review},
                                            "triage": {"team": TEAM}})
    c["repos_root"] = "/nonexistent"
    return c


class AnnounceTest(unittest.TestCase):
    """auto_draft.announce, with the cached triage and the GitHub search stood in for."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = {"reviews": {"needs_you": [], "author": [], "well_approved": [],
                                  "done": [], "unreadable": []}}
        self.patches = [
            mock.patch.object(A, "DRAFTS_STATE", Path(self.tmp.name) / "drafts"),
            mock.patch.object(A, "cached_triage", lambda: (self.cache, 10)),
            mock.patch.object(A, "requested_heads", lambda c: {}),
            mock.patch.object(A, "refresh_triage", lambda: False),
            mock.patch("watcher.followup.all_jobs", lambda: []),
            mock.patch("names.book", lambda fetch=False: {}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def add(self, n, **kw):
        self.cache["reviews"]["needs_you"].append({
            "pr_url": pr_url(n), "title": f"[WS-{n}] t", "author": "plaxdan",
            "bucket": "needs_you", "tier": 1, "head_sha": "a" * 40,
            "waiting_since": "2026-11-11T17:00:00Z", "approvals": [], **kw})

    def announce(self, now=NOW.timestamp(), c=None):
        notes = []
        A.announce(c or cfg(), now=now, deliver=notes.extend, log=lambda m: None)
        return notes

    def test_the_first_pass_records_whats_owed_without_a_popup(self):
        self.add(1)
        self.assertEqual(self.announce(), [])
        self.assertTrue(A.load_job("ui#1")["announced_at"])
        self.assertEqual(self.announce(), [])

    def test_a_new_review_pops_up_once(self):
        self.announce()
        self.add(2, team_ask=True)
        [note] = self.announce()
        self.assertEqual((note["key"], note["color"], note["team"], note["draft"]),
                         ("ui#2", P.GREEN, True, None))
        self.assertEqual(note["reason"], "Review it · 0 of 2 approvals · today")
        self.assertEqual(self.announce(), [])

    def test_turning_drafts_on_later_doesnt_announce_it_again(self):
        self.announce()
        self.add(3)
        self.announce()
        with mock.patch.object(A, "start", lambda j, *a, **k: A.save_job({**j, "status": "running",
                                                                         "pid": 0})):
            notes = []
            A.tick(cfg(auto_draft=True), now=NOW.timestamp() + 60, deliver=notes.extend,
                   log=lambda m: None)
        self.assertEqual(notes, [])


class NotifyTest(unittest.TestCase):
    def test_a_teammates_pr_says_review_it_and_is_marked_team(self):
        sent = []
        note = {"pr": {"url": pr_url(9125), "repo": "ui", "number": 9125, "author": "plaxdan",
                       "key": "WS-5850", "title": "[WS-5850] Supplier settings"},
                "color": P.YELLOW, "reason": "Review it · 1 of 2 approvals · 21d",
                "draft": None, "url": pr_url(9125), "key": "ui#9125", "team": True}
        with mock.patch("names.book", lambda fetch=False: {"plaxdan": "Woods"}), \
                mock.patch.object(popups, "send", lambda *a, **k: sent.append((a, k))), \
                redirect_stdout(io.StringIO()):
            A.notify_reviews([note], cfg(), log=lambda m: None)
        [(args, kw)] = sent
        self.assertEqual(args[3], "🟡 Review Woods's WS-5850")
        self.assertTrue(kw["team"])


class TeamPopupTest(unittest.TestCase):
    OPEN = {"state": "OPEN", "reviews": [], "requested": False}

    def test_an_absent_request_doesnt_settle_a_teammates_pr(self):
        self.assertIsNone(popups.review_outcome(self.OPEN, "2026-11-10T09:00:00Z", team=True))
        self.assertEqual(popups.review_outcome(self.OPEN, "2026-11-10T09:00:00Z")[0], "removed")
        self.assertEqual(popups.review_outcome({**self.OPEN, "state": "MERGED"}, "x", team=True),
                         ("closed", "the PR merged"))

    def test_it_settles_once_it_leaves_the_owed_list(self):
        entry = {"kind": "review", "since": "2026-11-10T09:00:00Z", "team": True,
                 "pr": {"url": pr_url(9125)}}
        self.assertIsNone(popups.review_resolution(entry, self.OPEN, "me", {pr_url(9125)}))
        self.assertIsNone(popups.review_resolution(entry, self.OPEN, "me", None))
        self.assertEqual(popups.review_resolution(entry, self.OPEN, "me", set()),
                         "it no longer waits on your review")

    def test_send_records_team(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("notify.popup", return_value="banner"):
            path = Path(d) / "popups.json"
            popups.send(popups.REVIEW, {"url": pr_url(1), "repo": "ui", "number": 1}, "green",
                        "t", "s", "b", {}, team=True, path=path)
            self.assertTrue(popups.load(path)["review-ui-1"]["team"])


class OwedFyiTest(unittest.TestCase):
    def test_a_teammates_pr_leaving_the_list_is_no_fyi_unless_it_closed(self):
        url = pr_url(9125)
        st = {"owed_fetched_at": "t1", "owed": {url: {"since": "2026-11-09T00:00:00Z",
                                                       "title": "[WS-5850] x", "team": True}}}
        live = {url: {"state": "OPEN", "reviews": [], "requested": False}}
        with mock.patch("review.auto_draft.cached_triage",
                        return_value=({"fetched_at": "t2", "reviews": {}}, 0)), \
                mock.patch("review.auto_draft.classify_owed", return_value=[]), \
                mock.patch.object(gh, "review_states", return_value=live):
            self.assertEqual(W.owed_fyis(st, cfg(), "me", "tok", {}, "2026-11-11T12:00:00Z"), [])

    def test_the_owed_list_remembers_which_are_teammates(self):
        st = {}
        entries = [{"url": pr_url(1), "title": "[WS-1] x", "st": {"team_ask": True}},
                   {"url": pr_url(2), "title": "[WS-2] x", "st": {}}]
        with mock.patch("review.auto_draft.cached_triage",
                        return_value=({"fetched_at": "t2", "reviews": {}}, 0)), \
                mock.patch("review.auto_draft.classify_owed", return_value=entries):
            W.owed_fyis(st, cfg(), "me", "tok", {}, "2026-11-11T12:00:00Z")
        self.assertEqual({u: v["team"] for u, v in st["owed"].items()},
                         {pr_url(1): True, pr_url(2): False})


if __name__ == "__main__":
    unittest.main()
