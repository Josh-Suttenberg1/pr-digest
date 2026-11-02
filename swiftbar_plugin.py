#!/usr/bin/env python3
"""SwiftBar plugin: render open GitHub PRs as a menu bar dropdown.

Output follows the SwiftBar/xbar plugin protocol:
  * lines before the first `---` become the menu bar title
  * lines after it become the dropdown

Run by the launcher in swiftbar-plugins/prdigest.1h.sh (hourly + on open).

Cache-first, so the menu opens instantly. A render reads the last-known PR list
from a local cache and prints it immediately (no network), then — if the cache
is stale or you just opened the menu — kicks a *detached* background fetch. That
fetch updates the cache and pokes SwiftBar to redraw via its URL scheme, so the
count/list refresh a beat later without ever blocking the menu. While a
user-opened fetch is in flight the menu bar icon shows a refresh glyph; the
count stays visible.

The "new" indicator (a red tray badge) lights up when a PR is brand-new or has
new activity (updated_at) since you last looked. It clears only when you
actually open the menu — SwiftBar sets SWIFTBAR_PLUGIN_REFRESH_REASON=MenuOpen
on click vs Schedule/Manual for the background timer.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gh_prs import (DigestError, collect_prs, enrich_prs, get_token,
                    humanize, load_env)

REPO_DIR = Path(__file__).resolve().parent
LAUNCHER = REPO_DIR / "swiftbar-plugins" / "prdigest.1h.sh"
PLUGIN_ID = "prdigest.1h.sh"  # unique id for swiftbar://refreshplugin
TITLE_MAX = 60

# Refetch when the cached data is older than this (seconds). A render kicks a
# background fetch past this age; below it, the data is "fresh enough" — which
# also debounces the rapid re-renders SwiftBar fires while a menu is held open.
REFRESH_TTL = 45
# A fetch running longer than this is presumed dead (crashed child); its
# in-flight marker is cleared so the spinner can never wedge permanently.
MARKER_TTL = 60

def demo_data() -> tuple[list[dict], dict[str, str]]:
    """Fake PRs + comments for screenshots/docs (PR_DIGEST_DEMO=1) — no token,
    no network. Timestamps are relative to now so previews read '2h', '1d'."""
    now = datetime.now(timezone.utc)

    def ago(**kw) -> str:
        return (now - timedelta(**kw)).isoformat()

    prs = [
        {"repo": "acme/widgets", "number": 142, "author": "jdoe", "age_days": 2,
         "title": "Fix flaky checkout total when the cart is empty",
         "url": "https://github.com/acme/widgets/pull/142",
         "updated_at": ago(hours=2), "comment_count": 7, "comments": [
            {"author": "dknight", "type": "reply", "created_at": ago(hours=2),
             "html_url": "https://github.com/acme/widgets/pull/142#c1",
             "body": "Can you rename this to total before we merge? Otherwise looks great."},
            {"author": "mira", "type": "review", "created_at": ago(days=1),
             "html_url": "https://github.com/acme/widgets/pull/142#c2",
             "body": "Why not reuse the existing helper here?"}]},
        {"repo": "acme/widgets", "number": 137, "author": "rsmith", "age_days": 5,
         "title": "Add dark mode to the settings page",
         "url": "https://github.com/acme/widgets/pull/137",
         "updated_at": ago(days=5), "comment_count": 0, "comments": []},
        {"repo": "acme/api", "number": 88, "author": "aturing", "age_days": 12,
         "title": "Migrate auth callers to tokenizer v3",
         "url": "https://github.com/acme/api/pull/88",
         "updated_at": ago(hours=5), "comment_count": 3, "comments": [
            {"author": "arancetto", "type": "review", "created_at": ago(hours=5),
             "html_url": "https://github.com/acme/api/pull/88#c1",
             "body": "LGTM, just a nit on the test name then I'll approve."},
            {"author": "bptest", "type": "reply", "created_at": ago(days=1),
             "html_url": "https://github.com/acme/api/pull/88#c2",
             "body": "CI is green now."}]},
    ]
    # #142 approved, #88 has merge conflicts, #137 is a draft.
    prs[0]["status"] = {"approved": True, "conflicts": False}
    prs[1]["status"] = {"draft": True}
    prs[2]["status"] = {"approved": False, "conflicts": True}
    for pr in prs:
        pr["latest"] = pr["comments"][0] if pr["comments"] else None
    # #137 already seen (calm); #142 & #88 have unread replies.
    seen = {"https://github.com/acme/widgets/pull/137": ago(days=5)}
    return prs, seen


# --------------------------------------------------------------------------- #
# State: a per-plugin dir holding seen.json (badge tracking) + cache.json.
# --------------------------------------------------------------------------- #
def state_dir() -> Path:
    base = os.environ.get("SWIFTBAR_PLUGIN_DATA_PATH", "").strip()
    directory = Path(base) if base else REPO_DIR / "state"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def load_seen() -> dict[str, str]:
    path = state_dir() / "seen.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def save_seen(prs: list[dict]) -> None:
    # Store the newest signal per PR — its own updated_at OR its latest comment,
    # whichever is later — so replies older than that read as "seen".
    seen: dict[str, str] = {}
    for pr in prs:
        ts = pr.get("updated_at", "")
        latest = pr.get("latest")
        if latest and latest.get("created_at", "") > ts:
            ts = latest["created_at"]
        seen[pr["url"]] = ts
    try:
        (state_dir() / "seen.json").write_text(json.dumps(seen), encoding="utf-8")
    except OSError:
        pass  # state is best-effort; never break the menu over it


def load_cache() -> dict | None:
    path = state_dir() / "cache.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (ValueError, OSError):
        return None


def save_cache(prs: list[dict], error: str | None) -> None:
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "prs": prs,
        "error": error,
    }
    try:
        (state_dir() / "cache.json").write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


def cache_age(cache: dict | None) -> float | None:
    if not cache or not cache.get("fetched_at"):
        return None
    try:
        when = datetime.fromisoformat(cache["fetched_at"])
        return (datetime.now(timezone.utc) - when).total_seconds()
    except ValueError:
        return None


# --- in-flight marker: the single source of truth for the spinner --------- #
def marker_path() -> Path:
    return state_dir() / "fetching"


def set_marker() -> None:
    try:
        marker_path().write_text(datetime.now(timezone.utc).isoformat(),
                                 encoding="utf-8")
    except OSError:
        pass


def clear_marker() -> None:
    try:
        marker_path().unlink(missing_ok=True)
    except OSError:
        pass


def is_fetching() -> bool:
    """True only while a fetch is actually running. Self-heals if it dies."""
    path = marker_path()
    if not path.exists():
        return False
    try:
        started = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
        age = (datetime.now(timezone.utc) - started).total_seconds()
    except (ValueError, OSError):
        clear_marker()
        return False
    if age > MARKER_TTL:  # presumed-dead fetch — clear so we can recover
        clear_marker()
        return False
    return True


# --------------------------------------------------------------------------- #
# Fetching (network) — only ever runs in the detached --fetch child or on the
# very first render when there's no cache yet.
# --------------------------------------------------------------------------- #
def fetch_and_cache(trigger_redraw: bool) -> None:
    load_env()
    try:
        token = get_token()
        prs = collect_prs(token)
        enrich_prs(token, prs)  # recent comments + review/merge status
        save_cache(prs, error=None)
    except DigestError as exc:
        # Keep the last-good list; attach a one-line error so the menu can warn.
        prev = load_cache() or {}
        save_cache(prev.get("prs", []), error=str(exc).splitlines()[0])
    finally:
        clear_marker()  # fetch is over (success or fail) → spinner must stop
    if trigger_redraw:
        trigger_refresh()


def trigger_refresh() -> None:
    try:
        subprocess.Popen(
            ["open", "-g", f"swiftbar://refreshplugin?plugin={PLUGIN_ID}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def kick_background_fetch() -> None:
    """Spawn a fully detached `--fetch` so the menu render returns now."""
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--fetch"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Rendering (SwiftBar protocol)
# --------------------------------------------------------------------------- #
def sanitize(text: str) -> str:
    """SwiftBar uses `|` to separate text from params, and lines are records."""
    return " ".join(text.split()).replace("|", "¦")


def clean_body(text: str) -> str:
    """Strip common Markdown from a comment so it reads as plain prose.
    Line-based rules run before sanitize() collapses whitespace."""
    t = text
    t = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", t)        # ![alt](url) -> alt
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)         # [text](url) -> text
    t = re.sub(r"```[^\n]*", "", t)                         # ``` fences
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", t)            # # headings
    t = re.sub(r"(?m)^\s{0,3}>\s?", "", t)                 # > blockquotes
    t = re.sub(r"(?m)^\s*[-*+]\s+", "• ", t)               # list bullets
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)               # **bold**
    t = re.sub(r"__([^_]+)__", r"\1", t)                   # __bold__
    t = re.sub(r"\*([^*\n]+)\*", r"\1", t)                 # *italic*
    t = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", r"\1", t)  # _italic_ (not snake_case)
    t = re.sub(r"~~([^~]+)~~", r"\1", t)                   # ~~strike~~
    t = t.replace("`", "")                                 # inline code ticks
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)           # html comments
    return sanitize(t)


def truncate(text: str, limit: int = TITLE_MAX) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def is_new(pr: dict, seen: dict[str, str]) -> bool:
    prev = seen.get(pr["url"])
    return prev is None or pr["updated_at"] > prev


def emit(text: str = "", **params: str) -> None:
    parts = []
    for key, value in params.items():
        value = str(value)
        if value == "":
            continue
        # SwiftBar splits params on spaces, so any value containing a space
        # must be quoted — otherwise it's cut at the first space (e.g. a
        # tooltip showing only the first word).
        if " " in value or '"' in value:
            parts.append(f'{key}="{value.replace(chr(34), chr(39))}"')
        else:
            parts.append(f"{key}={value}")
    print(f"{text} | {' '.join(parts)}" if parts else text)


def render_menu(prs: list[dict], seen: dict[str, str], *, spinner: bool,
                error: str | None) -> None:
    any_new = any(is_new(pr, seen) for pr in prs)
    count = len(prs)

    # --- menu bar title (single line) ---
    if spinner:
        emit(str(count) if count else "", sfimage="arrow.triangle.2.circlepath")
    elif error and not prs:
        emit(sfimage="exclamationmark.triangle", sfcolor="orange")
    elif count == 0:
        emit(sfimage="checkmark.circle", sfcolor="green")
    elif any_new:
        emit(str(count), sfimage="tray.full.fill", sfcolor="red")
    else:
        emit(str(count), sfimage="tray.full")

    print("---")

    if error:
        emit("Couldn't refresh GitHub", sfimage="exclamationmark.triangle",
             sfcolor="orange", color="#d08770")
        emit(sanitize(error), color="#888888")
        emit("Retry", bash=str(LAUNCHER), param1="--fetch", terminal="false",
             sfimage="arrow.clockwise")
        print("---")

    followups_block()

    if count == 0 and not error:
        emit("No PRs need your attention", color="#888888")
    elif count:
        emit(f"{count} PR(s) need your attention", color="#888888")
        by_repo: dict[str, list[dict]] = defaultdict(list)
        for pr in prs:
            by_repo[pr["repo"]].append(pr)
        for repo in sorted(by_repo):
            print("---")
            repo_prs = by_repo[repo]
            emit(f"{repo}  ({len(repo_prs)})", color="#888888")
            for pr in sorted(repo_prs, key=lambda p: p["age_days"], reverse=True):
                pr_block(pr, seen)

    # --- footer actions ---
    print("---")
    emit("Refresh now", bash=str(LAUNCHER), param1="--fetch", terminal="false",
         sfimage="arrow.clockwise")
    emit("Mark all as seen", bash=str(LAUNCHER), param1="--mark-seen",
         terminal="false", refresh="true", sfimage="eye")
    age = cache_label()
    emit(f"Updated {age}", color="#888888")
    emit(watcher_label(), color="#888888")


WATCHER = REPO_DIR / "bin" / "watcher"
VERDICT_MARKS = {"addressed": "✅", "partly": "🟡", "not_addressed": "🔴"}


def followups_block() -> None:
    """Follow-ups on Josh's review comments, from the watcher's job files.

    A ready job shows the verdict and the full drafted reply; "Post this reply"
    posts exactly that text (the click is Josh's approval), "Edit first" opens
    the reply file, and posting then uses whatever the file holds.
    """
    try:
        from watcher import followup, render
    except Exception:  # noqa: BLE001 — the digest must render even if the watcher can't load
        return
    jobs = [j for j in followup.all_jobs() if j["status"] in ("ready", "running", "queued", "failed")]
    if not jobs:
        return
    nick = _nicknames()
    emit(f"Follow-ups on your review comments ({len(jobs)})", color="#888888")
    for job in sorted(jobs, key=lambda j: j.get("finished_at") or 0, reverse=True):
        who = nick.get(job["actor"], job["actor"])
        sub = render.subtitle(job["pr"])
        if job["status"] in ("running", "queued"):
            emit(f"🔄 checking {who}'s commits · {sub}", href=job["url"])
            continue
        if job["status"] == "failed":
            emit(f"⚠️ follow-up failed · {sub}", href=job["url"], color="#d08770")
            emit(f"--{sanitize(job.get('error') or 'unknown error')}", color="#888888")
            emit("--Dismiss", bash=str(WATCHER), param1="dismiss", param2=job["id"],
                 terminal="false", refresh="true")
            continue
        verdict = job["verdict"].replace("_", " ")
        emit(f"{VERDICT_MARKS.get(job['verdict'], '🔎')} {who}: {verdict} · {sub}", href=job["url"])
        reply = Path(job["reply_file"]).read_text() if Path(job["reply_file"]).exists() else ""
        emit("--Drafted reply:", color="#888888")
        for line in reply.strip().splitlines():
            if line.strip():
                emit(f"--{truncate(sanitize(line), 90)}", tooltip=sanitize(line))
        emit("-----")
        emit("--✅ Post this reply", bash=str(WATCHER), param1="post", param2=job["id"],
             terminal="false", refresh="true")
        emit("--✏️ Edit first", bash="/usr/bin/open", param1="-t", param2=job["reply_file"],
             terminal="false")
        emit("--💬 Open in Claude", bash=str(REPO_DIR / ".venv" / "bin" / "python"),
             param1=str(REPO_DIR / "session.py"), param2="--prompt",
             param3=f"Let's go over the follow-up on my review comment: {job['report']}",
             terminal="false")
        emit("--📄 Open report", bash="/usr/bin/open", param1="-t", param2=job["report"],
             terminal="false")
        emit("--Dismiss", bash=str(WATCHER), param1="dismiss", param2=job["id"],
             terminal="false", refresh="true")
    print("---")


def _nicknames() -> dict[str, str]:
    try:
        import settings
        return settings.load().get("nicknames", {})
    except Exception:  # noqa: BLE001
        return {}


def watcher_label() -> str:
    """One footer line on the watcher's health, from its state file."""
    path = REPO_DIR / "state" / "watcher" / "state.json"
    try:
        st = json.loads(path.read_text())
    except (OSError, ValueError):
        return "Watcher: not running (bin/watcher install)"
    backoff = st.get("backoff") or {}
    if backoff.get("fails"):
        return f"Watcher: GitHub failing ({backoff['fails']}×) — {sanitize(backoff.get('last_error', ''))[:60]}"
    last = st.get("last_ok_at")
    if not last:
        return "Watcher: waiting for its first check"
    when = datetime.fromisoformat(last.replace("Z", "+00:00")).astimezone()
    return f"Watcher checked {when:%H:%M}"


def cache_label() -> str:
    cache = load_cache()
    if not cache or not cache.get("fetched_at"):
        return "—"
    try:
        when = datetime.fromisoformat(cache["fetched_at"]).astimezone()
        return f"{when:%H:%M}"
    except ValueError:
        return "—"


def pr_block(pr: dict, seen: dict[str, str]) -> None:
    """Version 3 'row summary': the PR row carries the latest replier + a
    comment count badge + unread dot; up to 2 recent comments follow, then
    '+n more' (or 'no replies yet')."""
    seen_ts = seen.get(pr["url"], "")
    comments = pr.get("comments", [])
    count = pr.get("comment_count", 0)
    latest = pr.get("latest")
    age = "today" if pr["age_days"] == 0 else f"{pr['age_days']}d"

    # --- the PR row ---
    # Just the PR author here — the latest replier already shows as the first
    # comment line below, so repeating it on the row is redundant. A leading
    # status glyph flags approved (✅) / merge conflicts (⚠️).
    status = pr.get("status") or {}
    marks = ""
    if status.get("draft"):
        marks += "🚧 "
    if status.get("approved"):
        marks += "✅ "
    if status.get("changes_requested"):
        marks += "✏️ "
    if status.get("conflicts"):
        marks += "⚠️ "
    label = f"#{pr['number']}  {marks}{truncate(sanitize(pr['title']), 46)}  ·  {pr['author']} · {age}"
    params: dict[str, str] = {"href": pr["url"]}
    if count:
        params["badge"] = str(count)
    if latest:
        params["tooltip"] = clean_body(latest["body"])
    if is_new(pr, seen):
        params["sfimage"], params["sfcolor"] = "circle.fill", "red"
    else:
        params["sfimage"], params["sfcolor"] = "circle", "#6b6b70"
    emit(label, **params)

    # --- up to 2 recent comments ---
    for c in comments[:2]:
        glyph = ("chevron.left.forwardslash.chevron.right"
                 if c["type"] == "review" else "arrowshape.turn.up.left")
        body = clean_body(c["body"])
        line = f"{c['author']}  {truncate(body, 46)} · {humanize(c['created_at'])}"
        cp: dict[str, str] = {"href": c["html_url"], "sfimage": glyph,
                              "tooltip": body}
        if c["created_at"] <= seen_ts:  # already seen → muted
            cp["color"] = "#8b8b90"
        emit(line, **cp)

    # --- tail ---
    if count > 2:
        emit(f"+{count - 2} more on GitHub", href=pr["url"],
             sfimage="arrow.up.right.square", color="#4aa3ff")
    elif count == 0:
        emit("no replies yet", color="#6b6b70")


# --------------------------------------------------------------------------- #
def main() -> int:
    args = sys.argv[1:]
    reason = os.environ.get("SWIFTBAR_PLUGIN_REFRESH_REASON", "")

    if os.environ.get("PR_DIGEST_DEMO"):
        demo_prs, demo_seen = demo_data()
        render_menu(demo_prs, demo_seen, spinner="--spinner" in args, error=None)
        return 0

    if "--fetch" in args:
        # Mark in-flight and redraw first, so manual "Refresh now" / "Retry"
        # also show the spinner; fetch_and_cache clears the marker when done.
        set_marker()
        trigger_refresh()
        fetch_and_cache(trigger_redraw=True)
        return 0

    if "--mark-seen" in args:
        cache = load_cache() or {}
        save_seen(cache.get("prs", []))
        return 0

    # --- render mode: instant, from cache ---
    cache = load_cache()
    if cache is None:
        # First ever run: nothing cached, so fetch inline this once.
        fetch_and_cache(trigger_redraw=False)
        cache = load_cache() or {"prs": [], "error": None}

    prs = cache.get("prs", [])
    error = cache.get("error")
    age = cache_age(cache)

    # The spinner reflects exactly one thing: is a fetch running right now?
    # We only start a new fetch when none is in flight and the data is stale.
    # No dependence on SWIFTBAR_PLUGIN_REFRESH_REASON (it sticks at "MenuOpen"
    # while the menu is held open, which previously re-armed the spinner).
    fetching = is_fetching()
    if not fetching and (age is None or age > REFRESH_TTL):
        set_marker()
        kick_background_fetch()
        fetching = True

    seen = load_seen()
    render_menu(prs, seen, spinner=fetching, error=error)

    # Clear the "new" badge only after you've actually opened the menu.
    if reason == "MenuOpen":
        save_seen(prs)

    return 0


if __name__ == "__main__":
    sys.exit(main())
