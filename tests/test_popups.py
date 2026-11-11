from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import notify
from watcher import __main__ as W
from watcher import gh, popups

CFG = {"notify": {"swiftbar_plugin": "p.1h.sh", "sounds": True, "alert_sound": "Glass",
                  "red_sound": "/red.aiff", "yellow_sound": "/yellow.aiff",
                  "done_sound": "/done.aiff"}}
PR = {"id": "PR_1", "url": "https://github.com/avetta/ui/pull/7", "repo": "ui", "number": 7}


class Rec:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")


class AlertApp(unittest.TestCase):
    """notify with the alert app installed."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        app = tmp / "PR Digest Alerts.app"
        (app / "Contents" / "MacOS").mkdir(parents=True)
        (app / "Contents" / "MacOS" / "terminal-notifier").write_text("")
        self.exe = str(app / "Contents" / "MacOS" / "terminal-notifier")
        patch = mock.patch.object(notify, "ALERT_APP", app)
        patch.start()
        self.addCleanup(patch.stop)


class PopupStyleTest(AlertApp):
    def test_red_is_an_alert_from_the_alert_app_and_replaces_a_yellow_banner(self):
        rec = Rec()
        style = notify.popup("red", "🔴 t", "s", "b", CFG, href="https://x", group="pr-ui-7", run=rec)
        self.assertEqual(style, "alert")
        self.assertEqual(rec.calls[0], [notify.TERMINAL_NOTIFIER, "-remove", "pr-ui-7"])
        self.assertEqual(rec.calls[1][0], self.exe)
        self.assertEqual(rec.calls[1][rec.calls[1].index("-group") + 1], "pr-ui-7")
        self.assertEqual(rec.calls[1][rec.calls[1].index("-open") + 1], "https://x")
        self.assertNotIn("-sound", rec.calls[1])
        self.assertEqual(rec.calls[2], ["/usr/bin/afplay", "/red.aiff"])

    def test_yellow_is_a_banner_with_its_sound_and_replaces_a_red_alert(self):
        rec = Rec()
        style = notify.popup("yellow", "🟡 t", "s", "b", CFG, group="pr-ui-7", run=rec)
        self.assertEqual(style, "banner")
        self.assertEqual(rec.calls[0], [self.exe, "-remove", "pr-ui-7"])
        self.assertEqual(rec.calls[1][0], notify.TERMINAL_NOTIFIER)
        self.assertEqual(rec.calls[2], ["/usr/bin/afplay", "/yellow.aiff"])

    def test_green_slides_away_silently(self):
        rec = Rec()
        notify.popup("green", "🟢 t", "s", "b", CFG, group="review-ui-7", run=rec)
        self.assertEqual([c[0] for c in rec.calls], [self.exe, notify.TERMINAL_NOTIFIER])

    def test_sounds_off_plays_nothing(self):
        rec = Rec()
        cfg = {"notify": {**CFG["notify"], "sounds": False}}
        notify.popup("yellow", "🟡 t", "s", "b", cfg, group="g", run=rec)
        notify.popup("red", "🔴 t", "s", "b", cfg, group="g", run=rec)
        self.assertFalse(any(c[0] == "/usr/bin/afplay" for c in rec.calls))

    def test_withdraw_removes_the_group_from_both_apps(self):
        rec = Rec()
        notify.withdraw("pr-ui-7", run=rec)
        self.assertEqual(rec.calls, [[notify.TERMINAL_NOTIFIER, "-remove", "pr-ui-7"],
                                     [self.exe, "-remove", "pr-ui-7"]])


class RefusedAlertAppTest(AlertApp):
    def test_red_falls_back_to_swiftbar_when_the_alert_app_is_refused(self):
        calls = []

        def run(argv, **_):
            calls.append(list(argv))
            refused = argv[0] == self.exe and "-remove" not in argv
            return subprocess.CompletedProcess(argv, 3 if refused else 0, "", "")
        notify.popup("red", "🔴 t", "s", "b", CFG, group="g", run=run)
        self.assertTrue(any(c[:2] == ["open", "-g"] and c[2].startswith("swiftbar://notify")
                            for c in calls))


class NoAlertAppTest(unittest.TestCase):
    def test_red_falls_back_to_swiftbar_without_the_alert_app(self):
        calls = []

        def run(argv, **_):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")
        notify.popup("red", "🔴 t", "s", "b", CFG, group="g", run=run)
        self.assertTrue(any(c[:2] == ["open", "-g"] and c[2].startswith("swiftbar://notify")
                            for c in calls))

    def test_withdraw_without_the_alert_app_uses_terminal_notifier_only(self):
        rec = Rec()
        notify.withdraw("g", run=rec)
        self.assertEqual(rec.calls, [[notify.TERMINAL_NOTIFIER, "-remove", "g"]])


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "popups.json"

    def test_send_records_and_withdraw_forgets(self):
        rec = Rec()
        sent = popups.send(popups.PR, PR, "yellow", "🟡 Mykal approved ABC-1", "s", "b", CFG,
                           run=rec, path=self.path)
        self.assertEqual(sent["group"], "pr-ui-7")
        entries = popups.load(self.path)
        self.assertEqual(entries["pr-ui-7"]["pr"], PR)
        self.assertEqual(entries["pr-ui-7"]["kind"], "pr")
        logged = []
        popups.withdraw({"pr-ui-7": "nothing left to do on it"}, run=rec, path=self.path,
                        log=logged.append)
        self.assertEqual(popups.load(self.path), {})
        self.assertIn([notify.TERMINAL_NOTIFIER, "-remove", "pr-ui-7"], rec.calls)
        self.assertIn("🟡 Mykal approved ABC-1 (nothing left to do on it)", logged[0])

    def test_each_kind_has_its_own_group(self):
        self.assertEqual(popups.group_for(popups.PR, "ui", 7), "pr-ui-7")
        self.assertEqual(popups.group_for(popups.REVIEW, "ui", 7), "review-ui-7")
        self.assertEqual(popups.group_for(popups.AUTOFIX, job="ui-7-x"), "autofix-ui-7-x")


def entry(kind, pr=PR, **kw):
    return {"kind": kind, "pr": dict(pr), "color": "red", "sent_at": "2026-11-10T10:00:00Z", **kw}


class ResolutionTest(unittest.TestCase):
    def test_watched_ids_are_pr_rows_and_autofixes(self):
        entries = {"pr-ui-7": entry("pr"), "review-ui-8": entry("review", {**PR, "id": "PR_8"}),
                   "autofix-x": entry("autofix", {**PR, "id": "PR_9"}, job="x")}
        self.assertEqual(popups.watched_ids(entries), {"PR_1", "PR_9"})

    def test_a_pr_row_resolves_only_once_classified_without_a_color(self):
        entries = {"pr-ui-7": entry("pr")}
        self.assertEqual(popups.resolved_rows(entries, {"PR_1"}, set()),
                         {"pr-ui-7": "nothing left to do on it"})
        self.assertEqual(popups.resolved_rows(entries, {"PR_1"}, {"PR_1"}), {})  # still colored
        self.assertEqual(popups.resolved_rows(entries, set(), set()), {})        # not checked

    def test_any_kind_resolves_when_its_pr_closes(self):
        entries = {"pr-ui-7": entry("pr"), "review-ui-7": entry("review", {**PR, "id": None})}
        self.assertEqual(popups.resolved_closed(entries, {"PR_1": "MERGED"}),
                         {"pr-ui-7": "the PR merged"})
        self.assertEqual(popups.resolved_closed(entries, {PR["url"]: "CLOSED"}),
                         {"pr-ui-7": "the PR closed", "review-ui-7": "the PR closed"})

    def test_a_review_resolves_once_you_review_after_the_request(self):
        e = entry("review", since="2026-11-10T09:00:00Z")
        before = {"state": "OPEN", "reviews": [{"state": "APPROVED",
                                                "submitted_at": "2026-11-09T09:00:00Z"}]}
        pending = {"state": "OPEN", "reviews": [{"state": "PENDING", "submitted_at": None}]}
        after = {"state": "OPEN", "reviews": [{"state": "COMMENTED", "body": "Looks good, two notes",
                                               "submitted_at": "2026-11-10T11:00:00Z"}]}
        bare_reply = {"state": "OPEN", "reviews": [{"state": "COMMENTED", "body": "",
                                                    "submitted_at": "2026-11-10T11:00:00Z"}]}
        dismissed = {"state": "OPEN", "reviews": [{"state": "DISMISSED", "body": "",
                                                   "submitted_at": "2026-11-10T11:00:00Z"}]}
        self.assertIsNone(popups.review_resolution(e, bare_reply, "me"))
        self.assertIsNone(popups.review_resolution(e, dismissed, "me"))
        self.assertIsNone(popups.review_resolution(e, before, "me"))
        self.assertIsNone(popups.review_resolution(e, pending, "me"))
        self.assertIsNone(popups.review_resolution(e, None, "me"))
        self.assertEqual(popups.review_resolution(e, after, "me"), "you reviewed it")
        self.assertEqual(popups.review_resolution(e, {"state": "MERGED", "reviews": []}, "me"),
                         "the PR merged")

    def test_a_review_without_since_counts_from_when_the_popup_went_out(self):
        e = entry("review")
        early = {"state": "OPEN", "reviews": [{"state": "APPROVED",
                                               "submitted_at": "2026-11-10T09:59:00Z"}]}
        self.assertIsNone(popups.review_resolution(e, early, "me"))

    def test_an_autofix_resolves_once_no_longer_escalated(self):
        e = entry("autofix", job="x")
        self.assertIsNone(popups.autofix_resolution(e, {"status": "escalated"}))
        self.assertEqual(popups.autofix_resolution(e, {"status": "cleared"}),
                         "the auto-fix is cleared")
        self.assertEqual(popups.autofix_resolution(e, None), "the auto-fix is gone")


class SettleTest(unittest.TestCase):
    def test_settle_collects_every_kind(self):
        entries = {
            "pr-ui-7": entry("pr"),
            "pr-ui-8": entry("pr", {**PR, "id": "PR_8", "number": 8}),
            "review-api-3": entry("review", {"id": None, "url": "https://github.com/avetta/api/pull/3",
                                             "repo": "api", "number": 3},
                                  since="2026-11-10T09:00:00Z"),
            "review-api-4": entry("review", {"id": None, "url": "https://github.com/avetta/api/pull/4",
                                             "repo": "api", "number": 4},
                                  since="2026-11-10T09:00:00Z"),
            "autofix-x": entry("autofix", {**PR, "id": "PR_9"}, job="x"),
        }
        live = {"https://github.com/avetta/api/pull/3": {
                    "state": "OPEN", "reviews": [{"state": "APPROVED",
                                                  "submitted_at": "2026-11-10T12:00:00Z"}]},
                "https://github.com/avetta/api/pull/4": {"state": "OPEN", "reviews": []}}
        with mock.patch.object(gh, "review_states", return_value=live) as rs, \
                mock.patch.object(W.autofix, "load_job", return_value={"status": "cleared"}):
            out = W.settle_popups(entries, checked={"PR_1", "PR_8"}, still_colored={"PR_8"},
                                  closed={}, me="me", tok="t", cfg={})
        self.assertEqual(out, {"pr-ui-7": "nothing left to do on it",
                               "review-api-3": "you reviewed it",
                               "autofix-x": "the auto-fix is cleared"})
        self.assertEqual(sorted(rs.call_args.args[0]), ["https://github.com/avetta/api/pull/3",
                                                        "https://github.com/avetta/api/pull/4"])

    def test_a_github_error_resolves_no_review(self):
        entries = {"review-api-3": entry("review", {"id": None, "repo": "api", "number": 3,
                                                    "url": "https://github.com/avetta/api/pull/3"})}
        with mock.patch.object(gh, "review_states", side_effect=gh.GhError("boom")):
            self.assertEqual(W.settle_popups(entries, set(), set(), {}, "me", "t", {}), {})

    def test_a_closed_pr_skips_the_github_read(self):
        entries = {"review-api-3": entry("review", {"id": "PR_3", "repo": "api", "number": 3,
                                                    "url": "https://github.com/avetta/api/pull/3"})}
        with mock.patch.object(gh, "review_states") as rs:
            out = W.settle_popups(entries, set(), set(), {"PR_3": "MERGED"}, "me", "t", {})
        self.assertEqual(out, {"review-api-3": "the PR merged"})
        rs.assert_not_called()


class NotificationsAgreeWithSettleTest(unittest.TestCase):
    """The tick's `notified` is what resolved_rows reads: a PR whose color is gone
    drops out of it, and its popup is withdrawn."""

    def test_a_cleared_pr_drops_from_notified_and_its_popup_resolves(self):
        pr = {**PR, "kind": "mine", "title": "ABC-1 x"}
        notified = {"PR_1": {"color": "yellow", "at": "2026-11-10T10:00:00Z", "url": PR["url"]}}
        W.notifications({"PR_1": pr}, [], [], notified, {}, "2026-11-10T11:00:00Z",
                        classify=lambda p, js: (None, ""))
        self.assertEqual(notified, {})
        self.assertEqual(popups.resolved_rows({"pr-ui-7": entry("pr")}, {"PR_1"}, set(notified)),
                         {"pr-ui-7": "nothing left to do on it"})

    def test_a_dismissed_row_drops_from_notified_too(self):
        pr = {**PR, "kind": "mine", "title": "ABC-1 x"}
        notified = {"PR_1": {"color": "yellow", "at": "2026-11-10T10:00:00Z", "url": PR["url"]}}
        W.notifications({"PR_1": pr}, [], [], notified, {PR["url"]: "2026-11-10T10:30:00Z"},
                        "2026-11-10T11:00:00Z", classify=lambda p, js: ("yellow", "x"))
        self.assertEqual(notified, {})


class QueryTest(unittest.TestCase):
    def test_names_go_in_as_variables(self):
        q, aliases, variables = gh.review_states_query(
            ["https://github.com/avetta/ui/pull/7/", "not a url"], "Josh")
        self.assertEqual(aliases, {"p0": "https://github.com/avetta/ui/pull/7"})
        self.assertEqual(variables, {"me": "Josh", "o0": "avetta", "r0": "ui", "n0": 7})
        self.assertNotIn("avetta", q)
        self.assertIn("reviews(last: 30, author: $me)", q)


if __name__ == "__main__":
    unittest.main()
