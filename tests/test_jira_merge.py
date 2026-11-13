from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from watcher import fyi
from watcher import jira_merge as J

CFG = {"me": "me", "org": "avetta",
       "jira": {"cli": "acli", "browse_url": "https://jira.example/browse/"},
       "triage": {"team_tickets": ["WS", "CONN"]},
       "notify": {"swiftbar_plugin": "p.1h.sh", "sounds": True, "alert_sound": "Glass"}}
PROJECTS = ["WS", "CONN"]


def merged(n=7, repo="ui", branch="CONN-1234", title="[CONN-1234] Show the total",
           at="2026-11-12T18:00:00Z"):
    return {"number": n, "title": title, "url": f"https://github.com/avetta/{repo}/pull/{n}",
            "mergedAt": at, "headRefName": branch, "baseRefName": "main",
            "repository": {"name": repo}}


def ticket(project="CONN", type_="Story", status="Team Tests Passed", key="CONN-1234"):
    return {"key": key, "project": project, "type": type_, "status": status, "summary": "Total"}


class KeyTest(unittest.TestCase):
    def test_branch_wins_then_title(self):
        self.assertEqual(J.key_of("CONN-12", "[CONN-99] x", PROJECTS), "CONN-12")
        self.assertEqual(J.key_of("conn-12", "", PROJECTS), "CONN-12")
        for title in ("CONN-5: fix", "[CONN-5] fix", "[ CONN-5 ] fix", "Ws-5 fix"):
            with self.subTest(title=title):
                self.assertEqual(J.key_of("feature/x", title, PROJECTS),
                                 "WS-5" if title.startswith("Ws") else "CONN-5")

    def test_other_projects_and_no_key(self):
        self.assertIsNone(J.key_of("main-sync", "Fix duplicated /v1 segment", PROJECTS))
        self.assertIsNone(J.key_of("TSP-4", "[TSP-4] x", PROJECTS))

    def test_carries_by_title_or_branch(self):
        keys = {"WS-5517", "CONN-1515"}
        self.assertTrue(J.carries({"title": "[CONN-1515] x", "headRefName": "y"}, keys, PROJECTS))
        self.assertTrue(J.carries({"title": "x", "headRefName": "ws-5517"}, keys, PROJECTS))
        self.assertFalse(J.carries({"title": "[CONN-15150] x", "headRefName": "y"}, keys, PROJECTS))


class DecideTest(unittest.TestCase):
    def test_each_rule_moves_from_its_source_to_its_target(self):
        cases = [
            ("CONN", "Story", "Team Tests Passed", "Integration"),
            ("CONN", "Bug", "Team Tests Passed", "Integration"),
            ("CONN", "Sub-task", "In Review", "Done"),
            ("CONN", "Spike", "In Review", "Done"),
            ("CONN", "Non-Code Task", "In Review", "Done"),
            ("WS", "Bug", "Ready to merge", "Ready for Integration"),
            ("WS", "Task", "Ready to merge", "Ready for Integration"),
            ("WS", "Code Sub-task", "Ready to merge", "Ready for Integration"),
            ("WS", "User Story", "Team Testing In Progress", "Ready for Integration"),
            ("WS", "QA Sub-task", "Team Testing In Progress", "Ready for Integration"),
            ("WS", "Spike", "In Review", "Done"),
            ("WS", "Non-Code Task", "In Review", "Done"),
        ]
        for project, type_, source, target in cases:
            with self.subTest(project=project, type=type_):
                outcome, rule = J.decide(ticket(project, type_, source), [])
                self.assertEqual((outcome, rule.target), (J.MOVED, target))

    def test_target_or_later_is_already_done(self):
        for status in ("Integration", "Ready for Release", "Released"):
            with self.subTest(status=status):
                self.assertEqual(J.decide(ticket(status=status), [])[0], J.ALREADY)
        self.assertEqual(J.decide(ticket("WS", "Bug", "Ready for Integration"), [])[0], J.ALREADY)

    def test_any_other_status_is_unexpected_never_routed_through(self):
        for status in ("Team Testing In Progress", "Code Review", "In Development",
                       "Ready for Team Testing", "Will Not Implement", "Open"):
            with self.subTest(status=status):
                self.assertEqual(J.decide(ticket(status=status), [])[0], J.UNEXPECTED)
        # WS Bug/Task workflows have Team Testing In Progress too, but QA moves those
        # to Ready to merge; only the User Story workflow merges from it.
        self.assertEqual(J.decide(ticket("WS", "Bug", "Team Testing In Progress"), [])[0],
                         J.UNEXPECTED)

    def test_open_sibling_holds_only_a_ticket_in_its_source_status(self):
        self.assertEqual(J.decide(ticket(), ["processservice#9"])[0], J.HELD)
        self.assertEqual(J.decide(ticket(status="Integration"), ["processservice#9"])[0], J.ALREADY)
        self.assertEqual(J.decide(ticket(status="Code Review"), ["processservice#9"])[0],
                         J.UNEXPECTED)

    def test_no_rule_for_epics_or_ws_sub_tasks(self):
        self.assertEqual(J.decide(ticket("CONN", "Epic", "Zone 3 - Development & Testing"), [])[0],
                         J.NO_RULE)
        self.assertEqual(J.decide(ticket("WS", "Sub-task", "In Progress"), [])[0], J.NO_RULE)


class HandleTest(unittest.TestCase):
    def run_handle(self, tk, sibs=(), move=None, pr=None, dry_run=False):
        moves = []

        def jira_move(k, s):
            if move:
                move(k, s)
            moves.append((k, s))
        o = J.handle(pr or merged(), CFG, "tok", dry_run=dry_run, jira_view=lambda k: tk,
                     jira_move=jira_move, siblings_of=lambda keys: list(sibs))
        return o, moves

    def test_moves_the_resolved_key(self):
        o, moves = self.run_handle(ticket(key="CONN-1515"),
                                   pr=merged(branch="WS-5517", title="WS-5517: fix"))
        self.assertEqual(o["outcome"], J.MOVED)
        self.assertEqual(moves, [("CONN-1515", "Integration")])

    def test_the_merged_pr_never_holds_its_own_ticket(self):
        o, moves = self.run_handle(ticket(), sibs=("ui#7",))
        self.assertEqual((o["outcome"], o["siblings"]), (J.MOVED, []))
        self.assertEqual(moves, [("CONN-1234", "Integration")])

    def test_dry_run_and_non_moves_never_transition(self):
        for tk, sibs, dry in ((ticket(), (), True), (ticket(), ("api#2",), False),
                              (ticket(status="Code Review"), (), False)):
            with self.subTest(status=tk["status"], sibs=sibs, dry=dry):
                _, moves = self.run_handle(tk, sibs, dry_run=dry)
                self.assertEqual(moves, [])

    def test_a_failed_transition_is_reported(self):
        def boom(k, s):
            raise J.JiraError("no transition to Integration")
        o, _ = self.run_handle(ticket(), move=boom)
        self.assertEqual((o["outcome"], o["error"]), (J.FAILED, "no transition to Integration"))

    def test_no_key_reads_nothing(self):
        o = J.handle(merged(branch="cleanup", title="Fix a typo"), CFG, "tok",
                     jira_view=mock.Mock(side_effect=AssertionError("read Jira")))
        self.assertEqual(o["outcome"], J.NO_KEY)


class MessageTest(unittest.TestCase):
    def outcome(self, kind, tk=None, **kw):
        rule = J.rule_for("CONN", "Story")
        return {"ref": "ui#7", "url": merged()["url"], "outcome": kind, "key": "CONN-1234",
                "ticket": tk or ticket(), "rule": {"source": rule.source, "target": rule.target},
                "siblings": [], **kw}

    def test_texts(self):
        self.assertEqual(J.message(self.outcome(J.MOVED))[1],
                         "Merged · CONN-1234 Team Tests Passed → Integration")
        self.assertIn("stays in Team Tests Passed: api#2 still open",
                      J.message(self.outcome(J.HELD, siblings=["api#2"]))[1])
        self.assertIn("it's in Code Review, the merge moves it only from Team Tests Passed",
                      J.message(self.outcome(J.UNEXPECTED, ticket(status="Code Review")))[1])
        self.assertIsNone(J.message(self.outcome(J.ALREADY)))
        self.assertIsNone(J.message(self.outcome(J.NO_KEY)))

    def test_moves_are_quiet_fyis_and_problems_alert(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(fyi, "PATH", Path(d) / "fyi.json"), \
                mock.patch("watcher.popups.send") as send:
            alerts = []
            J.tell([self.outcome(J.MOVED),
                    self.outcome(J.UNEXPECTED, ticket(status="Code Review"))],
                   CFG, alert=lambda t, b, h: alerts.append((t, h)))
            items = fyi.load(Path(d) / "fyi.json")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.kwargs["href"], "https://jira.example/browse/CONN-1234")
        self.assertEqual(sorted(items), ["jira-moved-ui-7", "jira-unexpected-ui-7"])
        self.assertEqual(alerts, [("⚠️ ui#7 merged, but CONN-1234 is in Code Review",
                                   "https://jira.example/browse/CONN-1234")])


class RunTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "jira_merge.json"
        self.tell = mock.patch.object(J, "tell").start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.dir.cleanup)

    def run_pass(self, prs, view=None, now=1_000_000.0, **kw):
        moves = []
        with mock.patch.object(J, "merged_since",
                               return_value=(prs, "Thu, 12 Nov 2026 19:00:00 GMT")) as search:
            out = J.run(CFG, "tok", path=self.path, now=now, log=lambda m: None,
                        jira_view=view or (lambda k: ticket(key=k)),
                        jira_move=lambda k, s: moves.append((k, s)),
                        siblings_of=lambda keys: [], **kw)
        return out, moves, search

    def test_first_run_records_the_time_and_leaves_past_merges(self):
        out, moves, search = self.run_pass([merged()])
        self.assertEqual((out, moves), ([], []))
        search.assert_not_called()
        self.assertTrue(J.load(self.path)["since"])

    def test_moves_each_new_merge_once(self):
        J.save({"since": "2026-11-12T17:00:00Z", "handled": {}, "retries": {}}, self.path)
        out, moves, search = self.run_pass([merged()])
        self.assertEqual(moves, [("CONN-1234", "Integration")])
        self.assertEqual(search.call_args.args[3], "2026-11-12T16:00:00Z")  # one hour back
        st = J.load(self.path)
        self.assertEqual(st["since"], "2026-11-12T19:00:00Z")
        self.assertEqual(st["handled"][merged()["url"]]["outcome"], J.MOVED)
        self.tell.assert_called_once()
        _, moves, _ = self.run_pass([merged()])
        self.assertEqual(moves, [])

    def test_unreadable_ticket_retries_then_alerts(self):
        J.save({"since": "2026-11-12T17:00:00Z", "handled": {}, "retries": {}}, self.path)

        def down(k):
            raise J.JiraError("acli: not logged in")
        for i in range(J.MAX_RETRIES - 1):
            out, _, _ = self.run_pass([merged()], view=down)
            self.assertEqual(out, [])
            self.assertEqual(J.load(self.path)["retries"][merged()["url"]], i + 1)
        out, _, _ = self.run_pass([merged()], view=down)
        self.assertEqual([o["outcome"] for o in out], [J.FAILED])
        st = J.load(self.path)
        self.assertEqual(st["retries"], {})
        self.assertIn(merged()["url"], st["handled"])

    def test_replay_and_dry_run_save_nothing(self):
        J.save({"since": "2026-11-12T17:00:00Z", "handled": {}, "retries": {}}, self.path)
        before = self.path.read_text()
        out, moves, _ = self.run_pass([merged()], dry_run=True)
        self.assertEqual(([o["outcome"] for o in out], moves), ([J.MOVED], []))
        self.run_pass([merged()], since="2026-10-01T00:00:00Z", dry_run=True)
        self.assertEqual(self.path.read_text(), before)
        self.tell.assert_not_called()

    def test_old_handled_entries_are_pruned(self):
        old = {"https://github.com/avetta/ui/pull/1": {"at": 1, "outcome": J.MOVED}}
        J.save({"since": "2026-11-12T17:00:00Z", "handled": old, "retries": {}}, self.path)
        self.run_pass([], now=1_800_000_000.0)
        self.assertEqual(J.load(self.path)["handled"], {})


if __name__ == "__main__":
    unittest.main()
