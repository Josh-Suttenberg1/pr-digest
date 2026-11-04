#!/usr/bin/env python3
"""SwiftBar plugin: the one PR menu. Shows what is your move, and nothing else counts.

Output follows the SwiftBar/xbar plugin protocol:
  * lines before the first `---` become the menu bar title
  * lines after it become the dropdown

Run by the launcher in swiftbar-plugins/prdigest.1h.sh (hourly + on open).

Sections, top to bottom:
  1. Your PRs — every open PR you author (triage/mine.py), your move first. Each row
     leads with its state (💬 something to answer, ✏️ a change request waiting on the
     reviewer, ⏳ waiting on reviews, ✅ approved), and a grey line under it gives the
     comments to answer by kind, the approval count, and change requests. The submenu
     has each reviewer's decision (✅ Woods approved · 2/2), every comment to answer
     tagged by kind and linked to its thread, and a Claude session to work through
     them. GitHub drafts with nothing to answer collapse into one submenu.
  2. Follow-ups on your review comments — the watcher's drafted replies (post / edit /
     open in Claude / dismiss), shown only when there are some.
  3. Reviews you owe — triage/reviews.py's needs_you PRs outside the parked tier, in
     ranked order, each with its review status and a "Review with Claude" action
     (run through review-drafter/menubar.sh, which calls bin/review-pr).
  4. Everything else — a collapsed list of every other open PR the three GitHub
     searches (review requested, assigned, authored) or the triage turned up, each
     with the reason it isn't counted. It exists to check the sorting.

The menu-bar number counts your PRs with something to answer, actionable follow-ups,
and reviews you owe. Other people's comments on PRs you haven't engaged with never
appear.

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
        required = _required_approvals()
        status_report.write(build_sections(data, _followup_jobs(), load_dismissed(), required),
                            data, _nicknames(), required=required)
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


# Where one of your PRs stands, in the order the Your PRs section lists them.
MINE_STATES = ("move", "changes", "waiting", "approved")
MINE_ICONS = {"move": "💬", "changes": "✏️", "waiting": "⏳", "approved": "✅"}


def mine_state(row: dict, required: int) -> str:
    """move: something on it is waiting on you (and you haven't dismissed it).
    changes: a change request stands and the next move is the reviewer's.
    approved: enough approvals and no change request. waiting: everything else."""
    if row.get("waiting") and not row.get("dismissed"):
        return "move"
    if row.get("blockers") or row.get("answered_change_requests"):
        return "changes"
    if len(row.get("approvals") or []) >= required:
        return "approved"
    return "waiting"


def build_sections(data: dict, followup_jobs: list[dict],
                   dismissed: dict[str, str] | None = None, required: int = 2) -> dict:
    """Sort cached data into the menu's sections.

    Returns {"mine": [...], "mine_drafts": [...], "followups": [groups], "owed": [...],
    "other": [(reason, item), ...], "count": int}.

    `mine` is every open PR you author that isn't a GitHub draft, each copied with
    `state` (MINE_STATES) and `dismissed` set: your move first (newest activity
    first), then change requests, then those waiting on reviews, then approved ones.
    A GitHub draft with something waiting on you is listed there too; the rest are
    in `mine_drafts`.

    `count` is what the menu bar shows: your PRs in the "move" state, PRs with an
    actionable follow-up (ready or failed), and reviews you owe. `dismissed` maps one
    of your PR urls to the activity timestamp it was dismissed at; it stops counting
    until newer activity arrives."""
    dismissed = dismissed or {}
    reviews = data.get("reviews") or {}
    followups = group_followups([j for j in followup_jobs
                                 if j.get("status") in ("ready", "running", "queued", "failed")])
    mine_rows, mine_drafts = [], []
    for r in data.get("mine") or []:
        hidden_at = dismissed.get(r["url"])
        row = {**r, "dismissed": bool(r.get("waiting") and hidden_at
                                      and mine_activity(r) <= hidden_at)}
        row["state"] = mine_state(row, required)
        (mine_drafts if r.get("isDraft") and row["state"] != "move" else mine_rows).append(row)
    mine_rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    mine_rows.sort(key=lambda r: mine_activity(r) if r["state"] == "move" else "", reverse=True)
    mine_rows.sort(key=lambda r: MINE_STATES.index(r["state"]))
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

    placed = {item.get("pr_url") for _, item in other}
    placed |= {st.get("pr_url") for st in owed}
    placed |= {r.get("url") for r in mine_rows + mine_drafts}
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
    yours = [r for r in mine_rows if r["state"] == "move"]
    return {
        "mine": mine_rows,
        "mine_drafts": mine_drafts,
        "followups": followups,
        "owed": owed,
        "other": other,
        "count": len(yours) + len(actionable) + len(owed),
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


def _who(login: str, nick: dict[str, str]) -> str:
    if login.startswith("team:"):
        return login[len("team:"):]
    return nick.get(login, login)


def _names(logins, nick: dict[str, str]) -> str:
    names = [_who(x, nick) for x in logins]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def to_answer(row: dict) -> list[dict]:
    """Every comment waiting on you on one of your PRs, most decision-heavy first:
    {kind, author, summary, path, url, body}."""
    from triage import comment_kind as ck
    items = []
    for t in row.get("threads_waiting") or []:
        body = next((b["body"] for b in t.get("bodies") or []
                     if b["author"] == t.get("author")), "")
        items.append({"kind": t.get("kind") or ck.kind(body),
                      "author": t.get("author") or t.get("last_author", "?"),
                      "summary": t.get("summary") or ck.summary(body),
                      "path": t.get("path") or "", "url": t.get("url") or "", "body": body})
    for c in row.get("unanswered_comments") or []:
        items.append({"kind": c.get("kind") or ck.kind(c.get("body", "")),
                      "author": c.get("author", "?"),
                      "summary": c.get("summary") or ck.summary(c.get("body", "")),
                      "path": "", "url": c.get("url") or "", "body": c.get("body", "")})
    items.sort(key=lambda i: ck.sort_key(i["kind"]))
    return items


def mine_status_line(row: dict, nick: dict[str, str], required: int) -> str:
    """One line under the PR: what's yours to answer, the approval count, and any
    change request with whose move it is."""
    from triage import comment_kind as ck
    parts = []
    asks = to_answer(row)
    if asks:
        parts.append(f"💬 {len(asks)} to answer ({ck.phrase(i['kind'] for i in asks)})")
    for b in row.get("blockers") or []:
        parts.append(f"✏️ {_who(b['author'], nick)} requested changes, re-request once answered")
    n = len(row.get("approvals") or [])
    parts.append(f"✅ {n}/{required} approved" if n else f"0/{required} approved")
    waiting_on = [b["author"] for b in row.get("answered_change_requests") or []]
    if waiting_on:
        parts.append(f"✏️ {_names(waiting_on, nick)} requested changes, re-requested")
    if row.get("dismissed"):
        parts.append("dismissed")
    return " · ".join(parts)


def _reviews_submenu(row: dict, nick: dict[str, str], required: int) -> None:
    url = row["url"]
    requested = list(row.get("requested") or [])
    decided = set()
    emit("--Reviews", color="#888888")
    for a in row.get("approvals") or []:
        decided.add(a["author"])
        again = " · re-requested" if a["author"] in requested else ""
        emit(f"--✅ {_who(a['author'], nick)} approved · {a['ordinal']}/{required} · "
             f"{when(a.get('at'))}{again}", href=a.get("url") or url)
    for b in row.get("blockers") or []:
        decided.add(b["author"])
        emit(f"--✏️ {_who(b['author'], nick)} requested changes · {when(b.get('at'))} · "
             f"answer, then re-request", href=b.get("url") or url)
    for b in row.get("answered_change_requests") or []:
        decided.add(b["author"])
        emit(f"--✏️ {_who(b['author'], nick)} requested changes · {when(b.get('at'))} · "
             f"you re-requested {when(b.get('rerequested_at'))}, waiting on them",
             href=b.get("url") or url)
    pending = [r for r in requested if r not in decided]
    if pending:
        emit(f"--⏳ No review yet: {_names(pending, nick)}", color="#888888")
    if not decided and not pending:
        emit("--⏳ No reviews yet and no one requested", color="#888888")


def mine_block(row: dict, nick: dict[str, str], required: int) -> None:
    """One of your PRs: a status icon and the PR, a status line under it, and a
    submenu with each reviewer's decision, every comment to answer (linked to its
    thread), and a Claude session for working through them."""
    url = row["url"]
    from triage import comment_kind as ck
    acted = mine_activity(row)
    date = when(acted) if row["state"] == "move" else ""
    emit(f"{MINE_ICONS[row['state']]} {_label(row)}" + (f" · {date}" if date else ""),
         href=url)
    _reviews_submenu(row, nick, required)
    asks = to_answer(row)
    if asks:
        emit("-----")
        emit(f"--To answer ({len(asks)}): {ck.phrase(i['kind'] for i in asks)}",
             color="#888888")
        for i in asks:
            where = f" · {Path(i['path']).name}" if i["path"] else ""
            emit(f"--{ck.LABELS.get(i['kind'], i['kind'])} · {_who(i['author'], nick)}{where} — "
                 f"{truncate(sanitize(i['summary']), 80)}",
                 href=i["url"] or url, tooltip=truncate(sanitize(i["body"]), 600))
    emit("-----")
    if asks or row.get("blockers"):
        _claude_item("--🤖 Go through these with Claude", _respond_prompt(url))
    else:
        _claude_item("--🤖 Open in Claude", _status_prompt(f"Let's look at my PR {url}.", url))
    emit("--🌐 Open on GitHub", href=url)
    # The ⌥ alternate goes after the submenu: SwiftBar hangs `--` lines on the item
    # just above them, so placing it first would give the submenu to the alternate.
    if row["state"] == "move":
        _dismiss_line(_key(url), bash=str(LAUNCHER), param1="--dismiss-mine", param2=url,
                      param3=acted)
    emit(f"    {mine_status_line(row, nick, required)}", color="#888888", size="12")


def mine_drafts_block(rows: list[dict]) -> None:
    emit(f"📝 Your GitHub drafts ({len(rows)})", color="#888888")
    for row in rows:
        emit(f"--{_label(row)}", href=row["url"])


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


def _status_prompt(prompt: str, pr_url: str) -> str:
    """`prompt` plus the PR's status file, so the session starts from the facts
    rather than re-deriving them."""
    import status_report
    status = status_report.path_for(re.sub(r"#.*$", "", pr_url))
    return f"{prompt} The PR's current status is in {status}."


def _respond_prompt(pr_url: str) -> str:
    import session
    return session.respond_prompt(pr_url)


def _claude_item(text: str, prompt: str) -> None:
    """A menu item that opens a Claude session in iTerm primed with `prompt`."""
    emit(text, bash=str(REPO_DIR / ".venv" / "bin" / "python"),
         param1=str(REPO_DIR / "session.py"), param2="--prompt", param3=prompt,
         terminal="false")


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
    _claude_item("--💬 Open in Claude", _status_prompt(
        f"Let's go over the follow-up on my review comment: {job['report']}", job["url"]))
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
    for i, job in enumerate(jobs):
        if i:
            emit("-----")
        _job_detail(job, who)
    # After the submenu, so the submenu stays on the row and not on its ⌥ alternate.
    _dismiss_line(sub, bash=str(WATCHER), param1="dismiss",
                  param2=",".join(j["id"] for j in jobs))


def render_menu(sections: dict, *, spinner: bool, error: str | None,
                have_data: bool, nick: dict[str, str], required: int = 2) -> None:
    count = sections["count"]
    yours = [r for r in sections["mine"] if r["state"] == "move"]
    urgent = bool(sections["followups"] or yours)

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
        mine = sections["mine"]
        if yours:
            head = (f"Your PRs ({len(mine)}) — {len(yours)} "
                    f"need{'s' if len(yours) == 1 else ''} you · hold ⌥ to dismiss")
        else:
            head = f"Your PRs ({len(mine)}) — nothing to answer"
        emit(head, color="#888888")
        for row in mine:
            mine_block(row, nick, required)
        if sections["mine_drafts"]:
            mine_drafts_block(sections["mine_drafts"])
        if not mine and not sections["mine_drafts"]:
            emit("No open PRs", color="#6b6b70")

        if sections["followups"]:
            print("---")
            emit(f"Follow-ups on your review comments ({len(sections['followups'])}) "
                 f"— hold ⌥ to dismiss", color="#888888")
            for group in sections["followups"]:
                followup_group_block(group, nick)

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


def _required_approvals() -> int:
    try:
        import settings
        return int(settings.load()["watcher"]["required_approvals"])
    except Exception:  # noqa: BLE001
        return 2


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
             "blockers": [], "unanswered_comments": [],
             "answered_change_requests": [
                 {"author": "mira", "at": (now - timedelta(days=5)).isoformat(), "url": "",
                  "rerequested_at": (now - timedelta(days=4)).isoformat()}],
             "approvals": [
                 {"author": "aturing", "at": (now - timedelta(days=6)).isoformat(),
                  "url": "", "ordinal": 1},
                 {"author": "rsmith", "at": (now - timedelta(hours=2)).isoformat(),
                  "url": "", "ordinal": 2}],
             "requested": ["mira"],
             "threads_waiting": [
                 {"path": "src/total.py", "last_author": "rsmith", "author": "rsmith",
                  "last_at": (now - timedelta(hours=2)).isoformat(), "url": "",
                  "kind": "suggestion", "summary": "Add a test for an empty cart.",
                  "bodies": [{"author": "rsmith", "body": "**Suggestion:** Add a test for an empty cart."}]},
                 {"path": "src/total.py", "last_author": "rsmith", "author": "rsmith",
                  "last_at": (now - timedelta(hours=2)).isoformat(), "url": "",
                  "kind": "nit", "summary": "Rename `t` to `total`.",
                  "bodies": [{"author": "rsmith", "body": "**Nit:** Rename `t` to `total`."}]}]},
            {"repo": "api", "number": 90, "url": "https://github.com/acme/api/pull/90",
             "title": "ABC-14: paginate the orders list", "isDraft": False, "waiting": False,
             "blockers": [], "threads_waiting": [], "unanswered_comments": [],
             "answered_change_requests": [], "requested": ["aturing", "mira"],
             "approvals": [{"author": "rsmith", "at": (now - timedelta(days=1)).isoformat(),
                            "url": "", "ordinal": 1}]},
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

    required = _required_approvals()
    sections = build_sections(cache if have_data else {}, _followup_jobs(), load_dismissed(),
                              required)
    render_menu(sections, spinner=fetching, error=cache.get("error"),
                have_data=have_data, nick=_nicknames(), required=required)
    return 0


if __name__ == "__main__":
    sys.exit(main())
