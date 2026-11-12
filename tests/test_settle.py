"""A menu action runs a tick right away (watcher/__main__.py settle), so the popup for
what you just did is withdrawn within seconds instead of at the next scheduled tick."""

from __future__ import annotations

import unittest
from unittest import mock

import swiftbar_plugin as menu
from watcher import __main__ as W

CFG = {"me": "Josh-Suttenberg1", "notify": {"swiftbar_plugin": "prdigest.1h.sh"}}


class SettleTest(unittest.TestCase):
    def test_waits_for_a_running_tick_then_ticks(self):
        held = mock.Mock()
        locks = iter([None, None, held])
        sleeps = []
        with mock.patch.object(W.state, "lock", lambda: next(locks)), \
                mock.patch.object(W, "tick", return_value=0) as tick:
            self.assertEqual(W.settle(CFG, sleep=sleeps.append), 0)
        self.assertEqual(sleeps, [1, 1])
        held.close.assert_called_once()
        tick.assert_called_once_with(CFG)

    def test_gives_up_when_the_tick_never_ends(self):
        with mock.patch.object(W.state, "lock", return_value=None), \
                mock.patch.object(W, "tick") as tick:
            self.assertEqual(W.settle(CFG, wait=0, sleep=lambda s: None), 0)
        tick.assert_not_called()

    def test_settle_soon_starts_it_detached(self):
        popen = mock.Mock()
        with mock.patch.object(W, "LOG", mock.MagicMock()):
            W.settle_soon(popen=popen)
        args, kw = popen.call_args
        self.assertEqual(args[0][1:], ["-m", "watcher", "settle"])
        self.assertTrue(kw["start_new_session"])


class MenuActionsSettleTest(unittest.TestCase):
    def run_main(self, argv, **patches):
        with mock.patch.object(W.settings, "load", return_value=CFG), \
                mock.patch.object(W, "_refresh_menu"), \
                mock.patch.object(W, "settle_soon") as soon:
            for target, value in patches.items():
                mock.patch.object(W.followup, target, value).start()
            try:
                W.main(argv)
            finally:
                mock.patch.stopall()
        return soon

    def test_approve(self):
        soon = self.run_main(["approve", "PR870", "c2"], approve=lambda pr, head, cfg: 0)
        soon.assert_called_once()

    def test_post(self):
        soon = self.run_main(["post", "a,b"], post=lambda jid: 0)
        soon.assert_called_once()

    def test_dismiss(self):
        soon = self.run_main(["dismiss", "a"], dismiss=lambda jid: 0)
        soon.assert_called_once()

    def test_a_failed_action_still_settles(self):
        # A refused approval may have cleared nothing, but a tick is harmless and keeps
        # the menu honest.
        soon = self.run_main(["approve", "PR870", "c2"], approve=lambda pr, head, cfg: 1)
        soon.assert_called_once()

    def test_dismissing_a_menu_row(self):
        with mock.patch.object(menu, "dismiss") as dismiss, \
                mock.patch.object(menu, "trigger_refresh"), \
                mock.patch.object(W, "settle_soon") as soon, \
                mock.patch.object(menu.sys, "argv", ["x", "--dismiss", "https://pr/1", "t", "j1"]):
            menu.main()
        dismiss.assert_called_once_with("https://pr/1", "t", "j1")
        soon.assert_called_once()


class WithdrawnOnTheSettleTickTest(unittest.TestCase):
    """What the settle tick withdraws: a PR-row popup whose PR has no color left, and
    nothing while something is still on you."""

    def test_no_color_left_withdraws(self):
        entries = {"pr-processservice-870": {"kind": "pr", "pr": {"id": "PR870"}}}
        self.assertEqual(W.popups.resolved_rows(entries, {"PR870"}, set()),
                         {"pr-processservice-870": "nothing left to do on it"})

    def test_still_red_keeps_it(self):
        entries = {"pr-processservice-870": {"kind": "pr", "pr": {"id": "PR870"}}}
        self.assertEqual(W.popups.resolved_rows(entries, {"PR870"}, {"PR870"}), {})


if __name__ == "__main__":
    unittest.main()
