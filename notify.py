"""macOS notifications in two styles.

macOS decides alert vs banner per sending app, not per notification, so the two
styles go out through two apps:

- alert(): persistent, for things that need a decision (approvals, change
  requests, review verdicts). Sent through SwiftBar's notify URL; SwiftBar is
  set to the Alerts style in System Settings. Falls back to terminal-notifier
  when SwiftBar isn't running.
- banner(): slides away, for activity (comments, replies). Sent through
  terminal-notifier, which is set to the Banners style.

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
    plugin: str, title: str, subtitle: str, body: str, href: str | None = None
) -> str:
    params = [("plugin", plugin), ("title", title), ("subtitle", subtitle), ("body", body)]
    if href:
        params.append(("href", href))
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
) -> None:
    if swiftbar_running(run):
        url = swiftbar_notify_url(plugin, title, subtitle, body, href)
        run(["open", "-g", url], capture_output=True)
        return
    run(terminal_notifier_argv(title, subtitle, body, href=href, sound=sound), capture_output=True)


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
