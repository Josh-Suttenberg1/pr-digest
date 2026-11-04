#!/usr/bin/env python3
"""SwiftBar plugin: the one PR menu. Shows what is your move, and nothing else counts.

Output follows the SwiftBar/xbar plugin protocol:
  * lines before the first `---` become the menu bar title
  * lines after it become the dropdown

Run by the launcher in swiftbar-plugins/prdigest.1h.sh (hourly + on open).

Sections, top to bottom:
  1. Waiting on you — the watcher's drafted replies (post / edit / open in Claude /
     dismiss) and your own PRs with a change request, an open thread, or a comment
     you haven't answered (triage/mine.py).
  2. Reviews you owe — triage/reviews.py's needs_you PRs outside the parked tier, in
     ranked order, each with its review status and a "Review with Claude" action
     (run through review-drafter/menubar.sh, which calls bin/review-pr).
  3. Everything else — a collapsed list of every other open PR the three GitHub
     searches (review requested, assigned, authored) or the triage turned up, each
     with the reason it isn't counted. It exists to check the sorting.

The menu-bar number counts sections 1 and 2 only. Other people's comments on PRs you
haven't engaged with never appear.

Cache-first, so the menu opens instantly. A render reads the last-known result from a
local cache and prints it immediately (no network), then — if the cache is older than
REFRESH_TTL — kicks a detached background fetch. That fetch runs the triage, updates
the cache and pokes SwiftBar to redraw. While a fetch is in flight the icon shows a
refresh glyph and the count stays visible. Review status (running / failed / draft)
is read from local files at render time, so it is always current.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
LAUNCHER = REPO_DIR / "swiftbar-plugins" / "prdigest.1h.sh"
PLUGIN_ID = "prdigest.1h.sh"  # unique id for swiftbar://refreshplugin
REVIEW_MENU = REPO_DIR / "review-drafter" / "menubar.sh"   # review actions backend
REVIEW_STATE = REPO_DIR / "review-drafter" / "state"       # inflight-/phase-/fail-<key>
REVIEW_INDEX = REPO_DIR / "state" / "reviews"              # <repo>#<n>.json per review
WATCHER = REPO_DIR / "bin" / "watcher"
TITLE_MAX = 60

# Refetch when the cached data is older than this (seconds). A full triage takes about
# a minute, so this also keeps a menu held open from re-running it back to back.
REFRESH_TTL = 300
# A fetch running longer than this is presumed dead (crashed child); its in-flight
# marker is cleared so the spinner can never wedge permanently.
MARKER_TTL = 600

VERDICT_MARKS = {"addressed": "✅", "partly": "🟡", "not_addressed": "🔴"}


# --------------------------------------------------------------------------- #
# State: a per-plugin dir holding cache.json and the in-flight marker.
# --------------------------------------------------------------------------- #
def state_dir() -> Path:
    base = os.environ.get("SWIFTBAR_PLUGIN_DATA_PATH", "").strip()
    directory = Path(base) if base else REPO_DIR / "state"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def load_cache() -> dict | None:
    path = state_dir() / "cache.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (ValueError, OSError):
        return None


def save_cache(payload: dict) -> None:
    payload = {**payload, "fetched_at": datetime.now(timezone.utc).isoformat()}
    try:
        tmp = state_dir() / "cache.json.tmp"
        tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
        os.replace(tmp, state_dir() / "cache.json")
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
        marker_path().write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
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
    if age > MARKER_TTL:
        clear_marker()
        return False
    return True


# --------------------------------------------------------------------------- #
# Fetching (network) — only in the detached --fetch child, or inline on the very
# first render when there's no cache yet.
# --------------------------------------------------------------------------- #
_HEAVY = ("timeline", "issue_comments", "author_reviews", "draft", "teams", "me")


def _slim(st: dict) -> dict:
    """A triage record without the raw GitHub payloads it was built from."""
    return {k: (str(v) if isinstance(v, Path) else v)
            for k, v in st.items() if k not in _HEAVY}


def review_drafts(org: str) -> list[dict]:
    """The local review drafts bin/review-pr has written, in the shape the triage
    takes: pr_url, path, draft_head, blocking."""
    out = []
    for path in sorted(REVIEW_INDEX.glob("*.json")):
        try:
            idx = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not (idx.get("repo") and idx.get("number")):
            continue
        out.append({
            "pr_url": f"https://github.com/{org}/{idx['repo']}/pull/{idx['number']}",
            "path": idx.get("story") or idx.get("draft") or "",
            "draft_head": idx.get("head") or "",
            "blocking": bool((idx.get("counts") or {}).get("blocking")),
        })
    return out


def collect() -> dict:
    """Everything the menu shows, from GitHub. Raises on failure."""
    import settings
    from gh_prs import collect_prs, get_token, load_env
    from triage import mine as M
    from triage import reviews as R

    cfg = settings.load()
    me = cfg.get("me") or R.me_login()
    if not me:
        raise RuntimeError("Set `me` in ~/.config/pr-digest/config.toml")
    bots = set((cfg.get("watcher") or {}).get("bots") or [])
    reviews = R.review_triage(me, review_drafts(cfg.get("org") or R.ORG))
    for st in reviews["needs_you"]:
        st.setdefault("tier", R.TIER_FIRST)
    ordered_needs = R.ranked(reviews["needs_you"]) if reviews["needs_you"] else []
    mine_rows = M.mine_triage(me, bots)
    load_env()
    legacy = collect_prs(get_token())
    return {
        "me": me,
        "reviews": {
            "needs_you": [_slim(st) for st in ordered_needs],
            **{b: [_slim(st) for st in reviews[b]]
               for b in ("author", "well_approved", "done", "unreadable")},
        },
        "mine": mine_rows,
        "legacy": legacy,
        "error": None,
    }


def write_status(data: dict) -> None:
    """Refresh output/status/ (see status_report.py) from freshly fetched data."""
    try:
        import status_report
        status_report.write(build_sections(data, _followup_jobs(), load_dismissed()), data)
    except Exception:  # noqa: BLE001 — the status files must never break the menu
        pass


def fetch_and_cache(trigger_redraw: bool) -> None:
    try:
        data = collect()
        save_cache(data)
        write_status(data)
    except Exception as exc:  # noqa: BLE001 — keep the last good data, show why
        prev = load_cache() or {}
        prev.pop("fetched_at", None)
        msg = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
        save_cache({**prev, "error": msg})
    finally:
        clear_marker()
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
# Sorting the fetched data into the three sections (pure: no I/O)
# --------------------------------------------------------------------------- #
PARKED_TIER = 2


def _key(url: str) -> str:
    """`processservice#833` from a PR url."""
    parts = url.rstrip("/").split("/")
    return f"{parts[-3]}#{parts[-1]}"


def _parked_reason(st: dict) -> str:
    parked = [t for t in st.get("tags") or []
              if t.startswith("CI failing") or "approvals already" in t]
    return "parked — " + ("; ".join(parked) or "your review can't unblock it yet")


def mine_activity(row: dict) -> str:
    """When someone last acted on your PR: the newest change request, open-thread
    comment, or unanswered comment (ISO timestamp, "" when none)."""
    stamps = [b.get("at") or "" for b in row.get("blockers") or []]
    stamps += [t.get("last_at") or "" for t in row.get("threads_waiting") or []]
    stamps += [c.get("at") or "" for c in row.get("unanswered_comments") or []]
    return max(stamps, default="")


def group_followups(jobs: list[dict]) -> list[dict]:
    """One group per PR: {"pr", "jobs", "acted_at"}, newest activity first. A group's
    `acted_at` is when the watcher saw the author's latest move on any of its threads."""
    groups: dict[str, dict] = {}
    for job in jobs:
        g = groups.setdefault(job["pr"]["id"], {"pr": job["pr"], "jobs": [], "acted_at": ""})
        g["jobs"].append(job)
        g["acted_at"] = max(g["acted_at"], job.get("created_at") or "")
    return sorted(groups.values(), key=lambda g: g["acted_at"], reverse=True)


def build_sections(data: dict, followup_jobs: list[dict],
                   dismissed: dict[str, str] | None = None) -> dict:
    """Sort cached data into the menu's sections.

    Returns {"waiting": {"followups": [groups], "mine": [...]}, "owed": [...],
    "other": [(reason, item), ...], "count": int}. `count` is what the menu bar shows:
    PRs with an actionable follow-up (ready or failed), your PRs with something
    waiting, and reviews you owe. `dismissed` maps one of your PR urls to the activity
    timestamp it was dismissed at; it stays hidden until newer activity arrives."""
    dismissed = dismissed or {}
    reviews = data.get("reviews") or {}
    followups = group_followups([j for j in followup_jobs
                                 if j.get("status") in ("ready", "running", "queued", "failed")])
    mine_waiting, mine_hidden = [], []
    for r in data.get("mine") or []:
        if not r.get("waiting"):
            continue
        hidden_at = dismissed.get(r["url"])
        (mine_hidden if hidden_at and mine_activity(r) <= hidden_at else mine_waiting).append(r)
    needs = reviews.get("needs_you") or []
    owed = [st for st in needs if st.get("tier") != PARKED_TIER]

    other: list[tuple[str, dict]] = []
    for st in needs:
        if st.get("tier") == PARKED_TIER:
            other.append((_parked_reason(st), st))
    for st in reviews.get("author") or []:
        other.append(("waiting on the author — " + st.get("reason", ""), st))
    for st in reviews.get("well_approved") or []:
        other.append(("enough approvals — " + st.get("reason", ""), st))
    for st in reviews.get("done") or []:
        other.append(("you approved — " + st.get("reason", ""), st))
    for st in reviews.get("unreadable") or []:
        other.append(("couldn't read it from GitHub; it will be retried", st))
    for row in data.get("mine") or []:
        if not row.get("waiting"):
            note = " (GitHub draft)" if row.get("isDraft") else ""
            pending = sorted({b["author"] for b in row.get("answered_change_requests") or []})
            if pending:
                reason = (f"your PR — you re-requested {', '.join(pending)} after their "
                          f"change request; waiting on their re-review{note}")
            else:
                reason = f"your PR, nothing waiting on you{note}"
            other.append((reason, {"pr_url": row["url"], "title": row["title"]}))
    for row in mine_hidden:
        other.append(("your PR, dismissed — comes back on new activity",
                      {"pr_url": row["url"], "title": row["title"]}))

    placed = {item.get("pr_url") for _, item in other}
    placed |= {st.get("pr_url") for st in owed}
    placed |= {r.get("url") for r in mine_waiting}
    placed |= {r.get("url") for r in mine_hidden}
    me = (data.get("me") or "").lower()
    for pr in data.get("legacy") or []:
        url = pr.get("url", "").rstrip("/")
        if url in placed:
            continue
        sources = ", ".join(s.lower() for s in pr.get("sources") or []) or "search"
        if (pr.get("author") or "").lower() == me:
            reason = "your PR, not triaged (check this)"
        elif pr.get("draft"):
            reason = "GitHub draft PR — not ready for review yet"
        elif "Review requested" in (pr.get("sources") or []):
            reason = f"review requested but not triaged (check this) — matched: {sources}"
        else:
            reason = f"not on you — matched only: {sources}"
        other.append((reason, {"pr_url": url, "title": pr.get("title", ""),
                               "author": pr.get("author", "")}))

    actionable = [g for g in followups
                  if any(j.get("status") in ("ready", "failed") for j in g["jobs"])]
    return {
        "waiting": {"followups": followups, "mine": mine_waiting},
        "owed": owed,
        "other": other,
        "count": len(actionable) + len(mine_waiting) + len(owed),
    }


# --------------------------------------------------------------------------- #
# Rendering (SwiftBar protocol)
# --------------------------------------------------------------------------- #
def sanitize(text: str) -> str:
    """SwiftBar uses `|` to separate text from params, and lines are records."""
    return " ".join(str(text).split()).replace("|", "¦")


def truncate(text: str, limit: int = TITLE_MAX) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def emit(text: str = "", **params: str) -> None:
    parts = []
    for key, value in params.items():
        value = str(value)
        if value == "":
            continue
        # SwiftBar splits params on spaces, so any value containing a space must be
        # quoted — otherwise it's cut at the first space.
        if " " in value or '"' in value:
            parts.append(f'{key}="{value.replace(chr(34), chr(39))}"')
        else:
            parts.append(f"{key}={value}")
    print(f"{text} | {' '.join(parts)}" if parts else text)


def fmt_dur(seconds: float) -> str:
    s = int(max(0, seconds))
    if s >= 3600:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    if s >= 60:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s}s"


def _title(item: dict) -> str:
    """A PR title without its leading ticket key."""
    return re.sub(r"^\[?[A-Z][A-Z0-9]+-\d+\]?\s*:?\s*", "", item.get("title") or "").strip()


def _label(item: dict) -> str:
    url = item.get("pr_url") or item.get("url") or ""
    tickets = " ".join(item.get("tickets") or re.findall(r"\b[A-Z][A-Z0-9]+-\d+",
                                                         item.get("title") or "")[:1])
    lead = f"{tickets} · {_key(url)}" if tickets else _key(url)
    return f"{lead} — {truncate(sanitize(_title(item)), 50)}"


def _pid_alive(path: Path) -> bool:
    try:
        pid = int(path.read_text().strip())
    except (OSError, ValueError):
        return True     # just created by --review, pid not written yet
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def review_status(key: str, head: str) -> dict:
    """Where the local review of one PR stands, from review-drafter/state and
    state/reviews: running (with step + elapsed), failed (with reason), and/or a
    draft (current or for an older commit)."""
    out: dict = {}
    now = datetime.now().timestamp()
    inflight = REVIEW_STATE / f"inflight-{key}"
    if inflight.exists():
        if _pid_alive(inflight):
            started = inflight.stat().st_mtime
            step = "working"
            phase = REVIEW_STATE / f"phase-{key}"
            if phase.exists():
                bits = phase.read_text().strip().split("\t", 1)
                if len(bits) == 2:
                    step = bits[1]
            out["running"] = {"elapsed": now - started, "step": step}
        else:
            for stale in (inflight, REVIEW_STATE / f"phase-{key}"):
                stale.unlink(missing_ok=True)
    fail = REVIEW_STATE / f"fail-{key}"
    if fail.exists():
        out["failed"] = (fail.read_text().strip().split("\t", 1) + [""])[1]
    idx = REVIEW_INDEX / f"{key}.json"
    if idx.exists():
        try:
            rec = json.loads(idx.read_text())
        except (OSError, ValueError):
            rec = {}
        if rec:
            counts = ", ".join(f"{v} {k}" for k, v in (rec.get("counts") or {}).items() if v)
            reviewed, live = (rec.get("head") or "").lower(), (head or "").lower()
            out["draft"] = {
                "current": bool(reviewed and live)
                and (live.startswith(reviewed) or reviewed.startswith(live)),
                "severity": rec.get("severity") or "",
                "counts": counts,
                "path": rec.get("story") or rec.get("draft") or "",
            }
    return out


def owed_block(st: dict) -> None:
    url = st["pr_url"]
    key = _key(url)
    status = review_status(key, st.get("head_sha") or "")
    mark = ""
    if "running" in status:
        mark = f" · 🔄 {fmt_dur(status['running']['elapsed'])}"
    elif status.get("draft", {}).get("current"):
        mark = " · 📝 draft ready"
    elif "draft" in status:
        mark = " · 📝 draft is for an older commit"
    elif "failed" in status:
        mark = " · ⚠️ review failed"
    emit(f"{_label(st)}{mark}", href=url, tooltip=sanitize(st.get("reason", "")))
    who = st.get("author", "?")
    emit(f"--from {who} · {sanitize(' · '.join(st.get('tags') or []))}", color="#888888")
    emit(f"--why: {sanitize(st.get('reason', ''))}", color="#888888")
    emit("--View on GitHub", href=url)
    if "running" in status:
        run = status["running"]
        emit(f"--🔄 {sanitize(run['step'])} · {fmt_dur(run['elapsed'])}", color="#d29922")
        emit("--✖ Cancel this review", bash=str(REVIEW_MENU), param1="--cancel",
             param2=key, terminal="false", refresh="true")
        return
    if "failed" in status:
        emit(f"--⚠️ last attempt failed: {sanitize(status['failed'])[:120]}", color="#d29922")
    draft = status.get("draft")
    if draft:
        if not draft["current"]:
            emit("--⚠️ new commits since this review", color="#d29922")
        summary = f"{draft['severity']}: {draft['counts']}" if draft["counts"] else draft["severity"]
        emit(f"--📝 {sanitize(summary)}")
        emit("--💬 Open in Claude", bash=str(REVIEW_MENU), param1="--open-claude",
             param2=key, terminal="false")
        if draft["path"]:
            _read_item("--📖 Read draft", draft["path"])
    label = "🔁 Re-run the review" if draft else "🤖 Review with Claude"
    emit(f"--{label}", bash=str(REVIEW_MENU), param1="--review", param2=key,
         terminal="false", refresh="true")


def mine_block(row: dict) -> None:
    url = row["url"]
    bits = []
    if row.get("blockers"):
        bits.append("changes requested")
    if row.get("threads_waiting"):
        n = len(row["threads_waiting"])
        bits.append(f"{n} open thread{'' if n == 1 else 's'}")
    if row.get("unanswered_comments"):
        n = len(row["unanswered_comments"])
        bits.append(f"{n} comment{'' if n == 1 else 's'}")
    label = {"pr_url": url, "title": row.get("title", "")}
    acted = mine_activity(row)
    date = when(acted)
    emit(f"Your PR {_label(label)} · {', '.join(bits)}" + (f" · {date}" if date else ""),
         href=url)
    _dismiss_line(_key(url), bash=str(LAUNCHER), param1="--dismiss-mine", param2=url,
                  param3=acted)
    for b in row.get("blockers") or []:
        emit(f"--⛔ {b['author']} requested changes", href=b.get("url") or url)
    for t in row.get("threads_waiting") or []:
        where = f" ({t['path']})" if t.get("path") else ""
        emit(f"--💬 thread{where} — last: {t['last_author']}", href=t.get("url") or url)
    for c in row.get("unanswered_comments") or []:
        emit(f"--🗨 {c['author']}: {truncate(sanitize(c.get('body', '')), 70)}",
             href=c.get("url") or url)
    _claude_item(f"Let's go over what's waiting on me on my PR {url}.", url)


def when(stamp) -> str:
    """'14:12' for today, 'Nov 3' otherwise, in local time. Takes an ISO string or
    epoch seconds; '' when unknown."""
    if not stamp:
        return ""
    try:
        if isinstance(stamp, (int, float)):
            dt = datetime.fromtimestamp(stamp).astimezone()
        else:
            dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone()
    except (ValueError, OSError):
        return ""
    if dt.date() == datetime.now().astimezone().date():
        return f"{dt:%H:%M}"
    return f"{dt:%b} {dt.day}"


def _claude_item(prompt: str, pr_url: str) -> None:
    """Open a Claude session primed with `prompt` plus the PR's status file, so it
    starts from the facts rather than re-deriving them."""
    import status_report
    status = status_report.path_for(re.sub(r"#.*$", "", pr_url))
    emit("--💬 Open in Claude", bash=str(REPO_DIR / ".venv" / "bin" / "python"),
         param1=str(REPO_DIR / "session.py"), param2="--prompt",
         param3=f"{prompt} The PR's current status is in {status}.", terminal="false")


def _read_item(text: str, md_path: str) -> None:
    """Open a draft or report as a rendered page in the browser (review/view_draft.py)."""
    emit(text, bash=str(REPO_DIR / ".venv" / "bin" / "python"),
         param1=str(REPO_DIR / "review" / "view_draft.py"), param2=md_path, terminal="false")


def _dismiss_line(label: str, **action: str) -> None:
    """The ⌥ alternate of the row just emitted: hold Option and the row becomes this."""
    emit(f"✕ Dismiss · {label}", alternate="true", terminal="false", refresh="true",
         sfimage="xmark.circle", **action)


def _job_detail(job: dict, who: str) -> None:
    """One thread's follow-up inside its PR's submenu. A ready job shows the verdict and
    the full drafted reply; "Post this reply" posts exactly that text (the click is your
    approval), "Edit first" opens the reply file, and posting then uses whatever the
    file holds."""
    where = f"{Path(job.get('path') or '').name}:{job.get('line')}" if job.get("path") else "thread"
    if job["status"] in ("running", "queued"):
        emit(f"--🔄 checking {who}'s commits · {where}", href=job["url"])
        return
    if job["status"] == "failed":
        emit(f"--⚠️ follow-up failed · {where}", href=job["url"], color="#d08770")
        emit(f"--{sanitize(job.get('error') or 'unknown error')}", color="#888888")
        emit("--Dismiss this one", bash=str(WATCHER), param1="dismiss", param2=job["id"],
             terminal="false", refresh="true")
        return
    verdict = job["verdict"].replace("_", " ")
    emit(f"--{VERDICT_MARKS.get(job['verdict'], '🔎')} {verdict} · {where}", href=job["url"])
    emit(f"--Your comment: {truncate(sanitize(job.get('my_comment') or ''), 80)}",
         color="#888888", tooltip=sanitize(job.get("my_comment") or ""))
    reply_file = Path(job["reply_file"])
    reply = reply_file.read_text() if reply_file.exists() else ""
    emit("--Drafted reply:", color="#888888")
    for line in reply.strip().splitlines():
        if line.strip():
            emit(f"--{truncate(sanitize(line), 90)}", tooltip=sanitize(line))
    emit("--✅ Post this reply", bash=str(WATCHER), param1="post", param2=job["id"],
         terminal="false", refresh="true")
    emit("--✏️ Edit first", bash="/usr/bin/open", param1="-t", param2=job["reply_file"],
         terminal="false")
    _claude_item(f"Let's go over the follow-up on my review comment: {job['report']}",
                 job["url"])
    _read_item("--📖 Read report", job["report"])
    emit("--Dismiss this one", bash=str(WATCHER), param1="dismiss", param2=job["id"],
         terminal="false", refresh="true")


def followup_group_block(group: dict, nick: dict[str, str]) -> None:
    """One row per PR for the follow-ups on your review comments there, dated by when
    the author last moved. Each thread's verdict and drafted reply sit in the submenu;
    holding ⌥ turns the row into Dismiss for all of them."""
    from watcher import render
    jobs = group["jobs"]
    who = nick.get(jobs[0]["actor"], jobs[0]["actor"])
    sub = render.subtitle(group["pr"])
    verdicts = [j.get("verdict") for j in jobs if j["status"] == "ready"]
    if any(j["status"] in ("running", "queued") for j in jobs):
        head = f"🔄 checking {who}'s commits"
    elif any(j["status"] == "failed" for j in jobs):
        head = "⚠️ follow-up failed"
    elif verdicts and all(v == "addressed" for v in verdicts):
        n = len(verdicts)
        which = {1: "your comment", 2: "both your comments"}.get(n, f"all {n} of your comments")
        head = f"✅ {who} addressed {which}"
    else:
        done = sum(1 for v in verdicts if v == "addressed")
        head = f"🟡 {who}: {done} of {len(verdicts)} comments addressed"
    date = when(group["acted_at"])
    emit(f"{head} · {sub}" + (f" · {date}" if date else ""), href=jobs[0]["url"])
    _dismiss_line(sub, bash=str(WATCHER), param1="dismiss",
                  param2=",".join(j["id"] for j in jobs))
    for i, job in enumerate(jobs):
        if i:
            emit("-----")
        _job_detail(job, who)


def render_menu(sections: dict, *, spinner: bool, error: str | None,
                have_data: bool, nick: dict[str, str]) -> None:
    count = sections["count"]
    waiting = sections["waiting"]
    urgent = bool(waiting["followups"] or waiting["mine"])

    # --- menu bar title (single line) ---
    if spinner:
        emit(str(count) if count else "", sfimage="arrow.triangle.2.circlepath")
    elif error and not have_data:
        emit(sfimage="exclamationmark.triangle", sfcolor="orange")
    elif count == 0:
        emit(sfimage="checkmark.circle", sfcolor="green")
    elif urgent:
        emit(str(count), sfimage="tray.full.fill", sfcolor="red")
    else:
        emit(str(count), sfimage="tray.full")

    print("---")

    if error:
        emit("Couldn't refresh from GitHub", sfimage="exclamationmark.triangle",
             sfcolor="orange", color="#d08770")
        emit(sanitize(error), color="#888888")
        emit("Retry", bash=str(LAUNCHER), param1="--fetch", terminal="false",
             sfimage="arrow.clockwise")
        print("---")

    if not have_data:
        emit("Sorting your PRs… (about a minute the first time)", color="#888888")
    else:
        n_wait = len(waiting["followups"]) + len(waiting["mine"])
        emit(f"Waiting on you ({n_wait}) — hold ⌥ to dismiss", color="#888888")
        for group in waiting["followups"]:
            followup_group_block(group, nick)
        for row in waiting["mine"]:
            mine_block(row)
        if not n_wait:
            emit("Nothing waiting on you", color="#6b6b70")

        print("---")
        emit(f"Reviews you owe ({len(sections['owed'])})", color="#888888")
        for st in sections["owed"]:
            owed_block(st)
        if not sections["owed"]:
            emit("No reviews owed", color="#6b6b70")

        print("---")
        other = sections["other"]
        emit(f"Everything else ({len(other)}) — not counted", color="#888888")
        emit("Why each PR isn't counted (for checking the sorting)", sfimage="list.bullet")
        for reason, item in sorted(other, key=lambda pair: pair[0]):
            url = item.get("pr_url") or item.get("url") or ""
            emit(f"--{_label(item)}", href=url, tooltip=sanitize(reason))
            emit(f"--  {truncate(sanitize(reason), 100)}", color="#888888", size="11")

    # --- footer actions ---
    print("---")
    emit("Refresh now", bash=str(LAUNCHER), param1="--fetch", terminal="false",
         sfimage="arrow.clockwise")
    emit(f"Updated {cache_label()}", color="#888888")
    emit(watcher_label(), color="#888888")


def _nicknames() -> dict[str, str]:
    try:
        import settings
        return settings.load().get("nicknames", {})
    except Exception:  # noqa: BLE001
        return {}


def load_dismissed() -> dict[str, str]:
    """Your PRs you dismissed from Waiting on you: url → the activity timestamp at the
    time. A PR comes back once something newer happens on it."""
    try:
        data = json.loads((state_dir() / "dismissed.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def dismiss_mine(url: str, acted_at: str) -> None:
    data = load_dismissed()
    data[url] = acted_at or datetime.now(timezone.utc).isoformat()
    try:
        (state_dir() / "dismissed.json").write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def _followup_jobs() -> list[dict]:
    try:
        from watcher import followup
        return followup.all_jobs()
    except Exception:  # noqa: BLE001 — the menu must render even if the watcher can't load
        return []


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


def demo_data() -> dict:
    """Fake triage output for screenshots/docs (PR_DIGEST_DEMO=1) — no token, no
    network."""
    now = datetime.now(timezone.utc)
    return {
        "me": "you",
        "reviews": {
            "needs_you": [
                {"pr_url": "https://github.com/acme/api/pull/88", "title": "ABC-12: tokenizer v3",
                 "tickets": ["ABC-12"], "author": "aturing", "tier": 1, "head_sha": "",
                 "reason": "not reviewed yet", "tags": ["asked you directly", "waiting 2d"]},
            ],
            "author": [
                {"pr_url": "https://github.com/acme/widgets/pull/137", "title": "Dark mode",
                 "author": "rsmith", "reason": "you commented; no new commits or replies since"},
            ],
            "well_approved": [], "done": [], "unreadable": [],
        },
        "mine": [
            {"repo": "widgets", "number": 142, "url": "https://github.com/acme/widgets/pull/142",
             "title": "ABC-9: fix flaky checkout total", "isDraft": False, "waiting": True,
             "blockers": [{"author": "mira", "url": ""}], "threads_waiting": [],
             "unanswered_comments": []},
        ],
        "legacy": [],
        "fetched_at": (now - timedelta(minutes=1)).isoformat(),
    }


# --------------------------------------------------------------------------- #
def main() -> int:
    args = sys.argv[1:]

    if os.environ.get("PR_DIGEST_DEMO"):
        sections = build_sections(demo_data(), [])
        render_menu(sections, spinner="--spinner" in args, error=None, have_data=True,
                    nick={})
        return 0

    if "--fetch" in args:
        # Mark in-flight and redraw first, so "Refresh now" / "Retry" also show the
        # spinner; fetch_and_cache clears the marker when done.
        set_marker()
        trigger_refresh()
        fetch_and_cache(trigger_redraw=True)
        return 0

    if "--dismiss-mine" in args:
        i = args.index("--dismiss-mine")
        rest = args[i + 1:] + ["", ""]
        if rest[0]:
            dismiss_mine(rest[0], rest[1])
        trigger_refresh()
        return 0

    # --- render mode: instant, from cache ---
    cache = load_cache() or {}
    have_data = "reviews" in cache
    age = cache_age(cache) if have_data else None

    fetching = is_fetching()
    if not fetching and (age is None or age > REFRESH_TTL):
        set_marker()
        kick_background_fetch()
        fetching = True

    sections = build_sections(cache if have_data else {}, _followup_jobs(), load_dismissed())
    render_menu(sections, spinner=fetching, error=cache.get("error"),
                have_data=have_data, nick=_nicknames())
    return 0


if __name__ == "__main__":
    sys.exit(main())
