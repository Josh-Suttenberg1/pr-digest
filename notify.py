"""macOS notifications in two styles.

macOS decides alert vs banner per sending app, not per notification, so the two
styles go out through two apps:

- alert(): persistent, for things that need a decision (approvals, change
  requests, review verdicts). Sent through SwiftBar's notify URL; SwiftBar is
  set to the Alerts style in System Settings. Falls back to terminal-notifier
  when SwiftBar isn't running.
- banner(): slides away, for activity (comments, replies). Sent through
  terminal-notifier, which is set to the Banners style.

A PR notification also plays its priority's sound through `afplay` (`play`): a
distinct chime for red, a softer one for yellow. Those alerts ask SwiftBar for a
silent notification so the sound isn't doubled.

Every function takes an injectable `run` so tests can capture the argv.
"""

from __future__ import annotations

import subprocess
from typing import Callable
from urllib.parse import quote

Runner = Callable[..., subprocess.CompletedProcess]

TERMINAL_NOTIFIER = "/opt/homebrew/bin/terminal-notifier"


def swiftbar_running(run: Runner = subprocess.run) -> bool:
    return run(["pgrep", "-x", "SwiftBar"], capture_output=True).returncode == 0


def swiftbar_notify_url(
    plugin: str, title: str, subtitle: str, body: str, href: str | None = None,
    silent: bool = False,
) -> str:
    params = [("plugin", plugin), ("title", title), ("subtitle", subtitle), ("body", body)]
    if href:
        params.append(("href", href))
    if silent:
        params.append(("silent", "true"))
    query = "&".join(f"{k}={quote(v, safe='')}" for k, v in params)
    return f"swiftbar://notify?{query}"


def terminal_notifier_argv(
    title: str,
    subtitle: str,
    body: str,
    href: str | None = None,
    execute: str | None = None,
    group: str | None = None,
    sound: str | None = None,
) -> list[str]:
    argv = [TERMINAL_NOTIFIER, "-title", title, "-subtitle", subtitle, "-message", body or " "]
    if execute:
        argv += ["-execute", execute]
    elif href:
        argv += ["-open", href]
    if group:
        argv += ["-group", group]
    if sound:
        argv += ["-sound", sound]
    return argv


def alert(
    title: str,
    subtitle: str,
    body: str,
    href: str | None = None,
    plugin: str = "prdigest.1h.sh",
    sound: str = "Glass",
    run: Runner = subprocess.run,
    sound_file: str | None = None,
    group: str | None = None,
) -> None:
    """A persistent alert. With `sound_file`, that file plays through afplay instead
    of the notification's own sound."""
    if swiftbar_running(run):
        url = swiftbar_notify_url(plugin, title, subtitle, body, href, silent=bool(sound_file))
        run(["open", "-g", url], capture_output=True)
    else:
        run(terminal_notifier_argv(title, subtitle, body, href=href, group=group,
                                   sound=None if sound_file else sound), capture_output=True)
    if sound_file:
        play(sound_file, run)


def play(sound_file: str, run: Runner = subprocess.run) -> None:
    """Play a sound file and wait for it (about a second)."""
    run(["/usr/bin/afplay", sound_file], capture_output=True)


def banner(
    title: str,
    subtitle: str,
    body: str,
    href: str | None = None,
    execute: str | None = None,
    group: str | None = None,
    run: Runner = subprocess.run,
) -> None:
    run(
        terminal_notifier_argv(title, subtitle, body, href=href, execute=execute, group=group),
        capture_output=True,
    )
