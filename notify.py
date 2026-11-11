"""macOS notifications in two styles.

macOS decides alert vs banner per sending app, not per notification, so the two
styles go out through two apps:

- alert(): persistent, for what needs Josh now (a 🔴 PR, a failure to act on).
  Sent through the alert app, a copy of terminal-notifier with its own bundle id
  (ALERT_APP, built by `install_alert_app`), which defaults to the Alerts style.
  Without that app it goes through SwiftBar's notify URL (SwiftBar set to Alerts),
  and without SwiftBar through plain terminal-notifier.
- banner(): slides away, for activity (comments, replies). Sent through
  terminal-notifier, which is set to the Banners style.

`popup` picks the style from a PR's color: red is an alert, everything else a
banner. A notification sent through either terminal-notifier app with a `group`
can be taken back out of Notification Center (`withdraw`); one sent through
SwiftBar cannot, which is why the alert app exists.

A PR notification also plays its priority's sound through `afplay` (`play`): a
distinct chime for red, a softer one for yellow. Those alerts ask SwiftBar for a
silent notification so the sound isn't doubled.

Every function takes an injectable `run` so tests can capture the argv.
"""

from __future__ import annotations

import plistlib
import shutil
import subprocess
from pathlib import Path
from typing import Callable
from urllib.parse import quote

Runner = Callable[..., subprocess.CompletedProcess]

TERMINAL_NOTIFIER = "/opt/homebrew/bin/terminal-notifier"
# The alert app: terminal-notifier under its own bundle id, so macOS keeps a separate
# notification style for it (Alerts) and its alerts can be withdrawn by group.
ALERT_APP = Path("~/Applications/PR Digest Alerts.app").expanduser()
ALERT_BUNDLE_ID = "com.josh.pr-digest.alerts"


def alert_notifier() -> str | None:
    """The alert app's executable, or None when it isn't installed."""
    exe = ALERT_APP / "Contents" / "MacOS" / "terminal-notifier"
    return str(exe) if exe.exists() else None


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
    exe: str = TERMINAL_NOTIFIER,
) -> list[str]:
    argv = [exe, "-title", title, "-subtitle", subtitle, "-message", body or " "]
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
    silent: bool = False,
) -> None:
    """A persistent alert. With `sound_file`, that file plays through afplay instead
    of the notification's own sound; `silent` plays nothing at all. Through the alert
    app when it's installed, so a `group` can be withdrawn later; when the app can't
    deliver it (its notifications aren't allowed yet), through SwiftBar instead, so a
    red alert is never lost."""
    quiet = silent or bool(sound_file)
    app = alert_notifier()
    sent = app and run(terminal_notifier_argv(title, subtitle, body, href=href, group=group,
                                              sound=None if quiet else sound, exe=app),
                       capture_output=True).returncode == 0
    if sent:
        pass
    elif swiftbar_running(run):
        url = swiftbar_notify_url(plugin, title, subtitle, body, href, silent=quiet)
        run(["open", "-g", url], capture_output=True)
    else:
        run(terminal_notifier_argv(title, subtitle, body, href=href, group=group,
                                   sound=None if quiet else sound), capture_output=True)
    if sound_file and not silent:
        play(sound_file, run)


def sound_options(cfg: dict, color: str | None = None) -> dict:
    """alert() keyword arguments for the user's sound settings: nothing when
    notify.sounds is off, a PR color's file for a PR, else the alert sound."""
    n = cfg["notify"]
    if not n.get("sounds", True):
        return {"silent": True}
    if color:
        return {"sound_file": n.get(f"{color}_sound") or None}
    return {"sound": n["alert_sound"]}


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


def popup(
    color: str | None,
    title: str,
    subtitle: str,
    body: str,
    cfg: dict,
    href: str | None = None,
    group: str | None = None,
    run: Runner = subprocess.run,
) -> str:
    """A PR's notification in its color's style: red stays on screen (alert), every
    other color slides away (banner); each plays its color's sound, and "done" is the
    quiet one. With a `group`, the same group's earlier popup from the other app is
    withdrawn first, so a PR whose color rose shows one popup, not two. Returns the
    style used."""
    opts = sound_options(cfg, color)
    if color == "red":
        if group:
            run([TERMINAL_NOTIFIER, "-remove", group], capture_output=True)
        alert(title, subtitle, body, href=href, plugin=cfg["notify"]["swiftbar_plugin"],
              group=group, run=run, **opts)
        return "alert"
    if group and alert_notifier():
        run([alert_notifier(), "-remove", group], capture_output=True)
    banner(title, subtitle, body, href=href, group=group, run=run)
    if opts.get("sound_file") and not opts.get("silent"):
        play(opts["sound_file"], run)
    return "banner"


def withdraw(group: str, run: Runner = subprocess.run) -> None:
    """Take a group's notification out of Notification Center, from both apps. A
    group that was never sent, or was already closed, is a no-op."""
    for exe in (TERMINAL_NOTIFIER, alert_notifier()):
        if exe:
            run([exe, "-remove", group], capture_output=True)


def install_alert_app(source: Path | None = None, run: Runner = subprocess.run) -> Path:
    """Build ALERT_APP: a copy of Homebrew's terminal-notifier.app with its own bundle
    id and name, defaulting to the Alerts style, ad-hoc signed again since the
    Info.plist changed. Replaces an earlier copy. macOS asks once to allow its
    notifications, on the first one it sends."""
    if source is None:
        real = Path(TERMINAL_NOTIFIER).resolve()        # …/Cellar/terminal-notifier/<v>/bin/…
        source = real.parent.parent / "terminal-notifier.app"
    if ALERT_APP.exists():
        shutil.rmtree(ALERT_APP)
    ALERT_APP.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, ALERT_APP, symlinks=True)
    info_path = ALERT_APP / "Contents" / "Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    info.update(CFBundleIdentifier=ALERT_BUNDLE_ID, CFBundleName="PR Digest Alerts",
                CFBundleDisplayName="PR Digest Alerts", NSUserNotificationAlertStyle="alert")
    info_path.write_bytes(plistlib.dumps(info))
    proc = run(["codesign", "--force", "--deep", "--sign", "-", str(ALERT_APP)],
               capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"codesign failed: {proc.stderr.strip()[:200]}")
    run(["/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework"
         "/Support/lsregister", "-f", str(ALERT_APP)], capture_output=True)
    return ALERT_APP
