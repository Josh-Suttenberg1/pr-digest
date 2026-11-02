from __future__ import annotations

import subprocess
import unittest

import notify
import session


class Recorder:
    def __init__(self, swiftbar_up: bool):
        self.calls: list[list[str]] = []
        self.swiftbar_up = swiftbar_up

    def __call__(self, argv, **_):
        self.calls.append(argv)
        code = 0 if (argv[0] != "pgrep" or self.swiftbar_up) else 1
        return subprocess.CompletedProcess(argv, code, "", "")


class NotifyTests(unittest.TestCase):
    def test_swiftbar_url_encodes_every_value(self):
        url = notify.swiftbar_notify_url("p.1h.sh", "A & B", "x/y", "50% done", "https://g.com/a?b=1")
        self.assertEqual(
            url,
            "swiftbar://notify?plugin=p.1h.sh&title=A%20%26%20B&subtitle=x%2Fy"
            "&body=50%25%20done&href=https%3A%2F%2Fg.com%2Fa%3Fb%3D1",
        )

    def test_alert_uses_swiftbar_when_running(self):
        rec = Recorder(swiftbar_up=True)
        notify.alert("t", "s", "b", href="https://x", run=rec)
        self.assertEqual(rec.calls[-1][:2], ["open", "-g"])
        self.assertTrue(rec.calls[-1][2].startswith("swiftbar://notify?"))

    def test_alert_falls_back_to_terminal_notifier(self):
        rec = Recorder(swiftbar_up=False)
        notify.alert("t", "s", "b", href="https://x", run=rec)
        self.assertEqual(rec.calls[-1][0], notify.TERMINAL_NOTIFIER)
        self.assertIn("-open", rec.calls[-1])

    def test_banner_execute_wins_over_href(self):
        argv = notify.terminal_notifier_argv("t", "s", "b", href="https://x", execute="cmd", group="g")
        self.assertIn("-execute", argv)
        self.assertNotIn("-open", argv)
        self.assertEqual(argv[-2:], ["-group", "g"])


class SessionTests(unittest.TestCase):
    def test_command_quotes_prompt(self):
        cmd = session.claude_command("Let's review ABC-1 \"now\"", "/Users/x/avetta")
        self.assertEqual(cmd, "cd /Users/x/avetta && claude 'Let'\"'\"'s review ABC-1 \"now\"'")

    def test_applescript_escapes_quotes(self):
        script = session.iterm_script('claude "hi"')
        self.assertIn('write text "claude \\"hi\\""', script)


if __name__ == "__main__":
    unittest.main()
