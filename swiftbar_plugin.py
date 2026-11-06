#!/usr/bin/env python3
"""SwiftBar plugin: the one PR menu. Each PR gets one row with a priority dot.

Output follows the SwiftBar/xbar plugin protocol:
  * lines before the first `---` become the menu bar title
  * lines after it become the dropdown

Run by the launcher in swiftbar-plugins/prdigest.1h.sh (hourly + on open).

Every PR is classified once by triage/priority.py, which gives it a section, a color
(🔴 needs you now, 🟡 waiting a while, 🟢 new, or no dot) and the reason in words.
Each row reads `<dot> <ticket> · <what happened> · <PR title>`; a grey legend at the
top explains the dots. Sections, top to bottom:
  1. My PRs — every open PR you author (triage/mine.py), under the sub-headers
     Needs you, Waiting on others and Ready. The submenu says why it has its dot,
     where review stands (approvals with dates, change requests and whose move
     each is, who hasn't reviewed), every comment to answer by kind and linked to its
     thread, and a Claude session to work through them. GitHub drafts with nothing to
     answer collapse into one submenu.
  2. Follow-ups and mentions — others' PRs you reviewed where someone responded (a
     re-request, or replies / code changes under your comments), and any PR where
     someone @mentioned you; the watcher drafts a reply to each. The submenu lists
     every response with its own drafted reply and actions, then Post the drafted
     replies and the review draft's actions.
  3. Reviews you owe — PRs you haven't reviewed (or whose review of yours was
     dismissed), with the review draft's status and actions (run through
     review-drafter/menubar.sh, which calls bin/review-pr).
  4. Non action requiring updates — ⚪ low priority: every other open PR the GitHub
     searches or the triage turned up, with what happened in words (still working,
     waiting on the author, parked, approved, drafts). Never counted.

Each PR appears in exactly one section. Inside a section rows go red, yellow, green,
then no dot. The menu-bar title counts PRs per color (`🔴2 🟡1 🟢2`), or shows ✓.
Holding ⌥ turns a colored row into Dismiss: it loses its dot until something newer
happens on the PR.

Cache-first, so the menu opens instantly. A render reads the last-known result from a
local cache and prints it immediately (no network), then — if the cache is older than
REFRESH_TTL — kicks a detached background fetch. That fetch runs the triage, updates
the cache and pokes SwiftBar to redraw. While a fetch is in flight the icon shows a
refresh glyph and the counts stay visible. Review status (running / failed / draft)
and follow-up jobs are read from local files at render time, so they are always
current.
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
sys.path.insert(0, str(REPO_DIR))

from triage import priority  # noqa: E402
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
        people = _people(fetch=True)
        status_report.write(build_sections(data, _followup_jobs(), load_dismissed(), required,
                                           people, prio=_priority_settings()),
                            data, people, required=required)
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
PARKED_TIER = priority.PARKED_TIER


def _key(url: str) -> str:
    """`processservice#833` from a PR url."""
    parts = url.rstrip("/").split("/")
    return f"{parts[-3]}#{parts[-1]}"


def mine_activity(row: dict) -> str:
    """When someone last acted on your PR: the newest change request, open-thread
    comment, or unanswered comment (ISO timestamp, "" when none)."""
    stamps = [b.get("at") or "" for b in row.get("blockers") or []]
    stamps += [t.get("last_at") or "" for t in row.get("threads_waiting") or []]
    stamps += [c.get("at") or "" for c in row.get("unanswered_comments") or []]
    return max(stamps, default="")


def _priority_settings() -> dict:
    try:
        import settings
        return settings.load()["priority"]
    except Exception:  # noqa: BLE001
        return {"stale_after_days": 2, "max_engaged_for_stale": 1}


def build_sections(data: dict, followup_jobs: list[dict],
                   dismissed: dict[str, str] | None = None, required: int = 2,
                   nick: dict[str, str] | None = None, now: datetime | None = None,
                   prio: dict | None = None) -> dict:
    """Sort cached data into the menu's sections through triage/priority.py.

    Returns {"mine", "mine_drafts", "followups", "owed", "other": [entry], "counts":
    {color: n}, "title": str, "count": int}. An entry is a
    priority.classify result (url, title, section, color, reason, why, waiting_since,
    activity_at, dismissed, row | st, jobs).

    Each PR appears in exactly one section. My PRs go by sub-header (Needs you,
    Waiting on others, Ready: priority.MINE_GROUPS), then as below. Inside a section
    rows go red, yellow, green, then no dot; inside a color the longest wait first,
    except Reviews you owe, which keeps reviews.ranked() order. GitHub drafts of yours
    with no dot collapse into `mine_drafts`. `dismissed` maps a PR url to the activity time it was
    dismissed at; the row loses its dot until something newer happens.

    `title` is the menu-bar title (priority.title) and `count` the PRs with a dot."""
    nick = nick or {}
    now = now or datetime.now(timezone.utc)
    prio = prio or {"stale_after_days": 2, "max_engaged_for_stale": 1}
    reviews = data.get("reviews") or {}
    entries = priority.classify(
        data.get("mine") or [], reviews, followup_jobs, nick=nick, now=now,
        required=required, stale_after_days=prio["stale_after_days"],
        max_engaged=prio["max_engaged_for_stale"], dismissed=dismissed or {})

    ranked = {st.get("pr_url"): i for i, st in enumerate(reviews.get("needs_you") or [])}
    mine_rows = [e for e in entries if e["section"] == priority.MINE]
    mine_drafts = [e for e in mine_rows if e["row"].get("isDraft") and not e["color"]
                   and not e["dismissed"]]
    mine_rows = [e for e in mine_rows if e not in mine_drafts]
    mine_rows.sort(key=lambda e: e["row"].get("updated_at") or "", reverse=True)
    mine_rows.sort(key=lambda e: (priority.MINE_GROUP_ORDER.get(e.get("group"), 9),
                                  *priority.sort_key(e)))
    followups = sorted((e for e in entries if e["section"] == priority.FOLLOWUP),
                       key=priority.sort_key)
    owed = sorted((e for e in entries if e["section"] == priority.OWED),
                  key=lambda e: priority.sort_key(e, ranked.get(e["url"], len(ranked))))
    other = [e for e in entries if e["section"] == priority.OTHER]
    placed = {e["url"] for e in entries}
    for pr in data.get("legacy") or []:
        url = pr.get("url", "").rstrip("/")
        if url and url not in placed:
            placed.add(url)
            other.append(priority.other_entry(pr, data.get("me") or ""))
    other.sort(key=lambda e: (e.get("order", 9), e.get("title") or ""))

    shown = mine_rows + followups + owed
    counts = priority.counts(shown)
    return {
        "mine": mine_rows,
        "mine_drafts": mine_drafts,
        "followups": followups,
        "owed": owed,
        "other": other,
        "counts": counts,
        "title": priority.title(shown),
        "count": sum(counts.values()),
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


ROW_MAX = 100
# Leading non-breaking spaces that indent a sub-header under its section name.
SUBHEAD_INDENT = "\u00a0\u00a0\u00a0"


def row_text(e: dict, limit: int = ROW_MAX) -> str:
    """'🔴 ABC-123 · Woods left 8 comments (3 suggestions, 5 nits) · Show the exact…':
    the ticket first so the row always says which PR it is, then what happened, then
    as much of the title as fits."""
    key = priority.ticket(e.get("title") or "", e["url"])
    d = priority.dot(e.get("color"))
    head = f"{d} {key}" if d else key
    head += f" · {sanitize(e.get('reason') or '')}"
    title = sanitize(_title({"title": e.get("title") or ""}))
    if not title:
        return head
    return f"{head} · {truncate(title, max(limit - len(head) - 3, 16))}"


def _pr_header(e: dict) -> None:
    """The submenu's first lines: repo#number with the full title, then why."""
    emit(f"--{_key(e['url'])} · {truncate(sanitize(_title(e)), 80)}", href=e["url"])
    if e.get("why"):
        emit(f"--{sanitize(e['why'])}", color="#888888")


def _draft_actions(key: str, head: str, url: str) -> None:
    """The review draft's actions: running (cancel), failed, Read draft, Re-run review
    or Review with Claude, and Open in Claude."""
    status = review_status(key, head)
    if "running" in status:
        run = status["running"]
        emit(f"--Review running: {sanitize(run['step'])} · {fmt_dur(run['elapsed'])}",
             color="#d29922")
        emit("--Cancel this review", bash=str(REVIEW_MENU), param1="--cancel",
             param2=key, terminal="false", refresh="true")
        return
    draft = status.get("draft")
    if draft and draft["path"]:
        _read_item("--Read draft", draft["path"])
    if _reviews_on():
        emit("--Re-run review" if draft else "--Review with Claude", bash=str(REVIEW_MENU),
             param1="--review", param2=key, terminal="false", refresh="true")
    if draft:
        emit("--Open in Claude", bash=str(REVIEW_MENU), param1="--open-claude",
             param2=key, terminal="false")
    else:
        _claude_item("--Open in Claude", _status_prompt(f"Let's look at the PR {url}.", url))


def _draft_line(key: str, head: str) -> str:
    """"Claude's draft: …" in words."""
    status = review_status(key, head)
    if "running" in status:
        return f"Claude's draft: running, {fmt_dur(status['running']['elapsed'])} in"
    draft = status.get("draft")
    if draft:
        summary = f" ({draft['severity']}: {draft['counts']})" if draft["counts"] else (
            f" ({draft['severity']})" if draft["severity"] else "")
        return ("Claude's draft: current" if draft["current"]
                else "Claude's draft: for an older commit") + summary
    if "failed" in status:
        return f"Claude's draft: the last attempt failed ({sanitize(status['failed'])[:80]})"
    return "Claude's draft: none yet"


def _dismiss(e: dict, jobs: list[dict] = ()) -> None:
    """⌥ Dismiss for a colored row (or one already dismissed): it loses its dot until
    newer activity. Goes after the row's submenu."""
    if not e.get("color"):
        return
    params = {"param1": "--dismiss", "param2": e["url"],
              "param3": e.get("activity_at") or ""}
    if jobs:
        params["param4"] = ",".join(j["id"] for j in jobs if j.get("id"))
    _dismiss_line(priority.ticket(e.get("title") or "", e["url"]), bash=str(LAUNCHER),
                  **params)


def owed_block(e: dict, nick: dict[str, str]) -> None:
    """A review you owe: the reason in words, then the review draft and its actions."""
    st = e["st"]
    url = e["url"]
    key = _key(url)
    emit(row_text(e), href=url)
    _pr_header(e)
    emit(f"--{st.get('size', '')} · by {_who(st.get('author', '?'), nick)}", color="#888888")
    if _reviews_on():
        emit(f"--{_draft_line(key, st.get('head_sha') or '')}", color="#888888")
    emit("-----")
    _draft_actions(key, st.get("head_sha") or "", url)
    emit("--Open on GitHub", href=url)
    _dismiss(e)


def _who(login: str, nick: dict[str, str]) -> str:
    return priority.who(login, nick)


def _names(logins, nick: dict[str, str]) -> str:
    return priority.names(logins, nick)


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


def review_lines(row: dict, nick: dict[str, str], required: int) -> list[tuple[str, str]]:
    """Where review of one of your PRs stands, as (text, link) lines: approvals with
    their dates, change requests and whose move each is, and who hasn't reviewed."""
    url = row["url"]
    requested = list(row.get("requested") or [])
    approvals = row.get("approvals") or []
    decided = {a["author"] for a in approvals}
    lines = []
    if approvals:
        each = ", ".join(
            f"{_who(a['author'], nick)} {when(a.get('at'))}".strip()
            + ("" if a.get("on_head", True) else " (before the latest push)")
            for a in approvals)
        lines.append((f"Approvals: {len(approvals)} of {required} ({each})",
                      approvals[-1].get("url") or url))
    else:
        lines.append((f"Approvals: 0 of {required}", ""))
    crs = []
    for b in row.get("blockers") or []:
        decided.add(b["author"])
        crs.append(f"{_who(b['author'], nick)} ({when(b.get('at'))}, answer, then re-request)")
    for b in row.get("answered_change_requests") or []:
        decided.add(b["author"])
        crs.append(f"{_who(b['author'], nick)} (you re-requested "
                   f"{when(b.get('rerequested_at'))}, waiting on them)")
    if crs:
        lines.append(("Change requests: " + "; ".join(crs), ""))
    pending = [r for r in requested if r not in decided]
    if pending:
        lines.append((f"Not reviewed yet: {_names(pending, nick)}", ""))
    elif not decided:
        lines.append(("No reviews yet and no one requested", ""))
    return lines


def mine_block(e: dict, nick: dict[str, str], required: int) -> None:
    """One of your PRs: the row, then a submenu with why it's red, where review
    stands, every comment to answer (by kind, linked to its thread), and a Claude
    session for working through them."""
    from triage import comment_kind as ck
    row = e["row"]
    url = row["url"]
    emit(row_text(e), href=url)
    _pr_header(e)
    for text, link in review_lines(row, nick, required):
        emit(f"--{sanitize(text)}", href=link, color="" if link else "#888888")
    asks = to_answer(row)
    if asks:
        emit("-----")
        emit(f"--To answer ({len(asks)})", color="#888888")
        for i in asks:
            where = f" · {Path(i['path']).name}" if i["path"] else ""
            emit(f"--{ck.WORDS.get(i['kind'], i['kind'])} · {_who(i['author'], nick)}{where} — "
                 f"{truncate(sanitize(i['summary']), 80)}",
                 href=i["url"] or url, tooltip=truncate(sanitize(i["body"]), 600))
    emit("-----")
    if asks or row.get("blockers"):
        _claude_item("--Go through these with Claude", _respond_prompt(url))
    else:
        _claude_item("--Open in Claude", _status_prompt(f"Let's look at my PR {url}.", url))
    emit("--Open on GitHub", href=url)
    # The ⌥ alternate goes after the submenu: SwiftBar hangs `--` lines on the item
    # just above them, so placing it first would give the submenu to the alternate.
    _dismiss(e)


def mine_rows(entries: list[dict], nick: dict[str, str], required: int) -> None:
    """Your PRs, each sub-header (priority.MINE_GROUPS) once above its rows. `entries`
    come sorted by sub-header (build_sections)."""
    group = None
    for e in entries:
        if e.get("group") != group:
            group = e.get("group")
            if group in priority.MINE_GROUPS:
                emit(SUBHEAD_INDENT + priority.MINE_GROUPS[group], color="#888888", size="12")
        mine_block(e, nick, required)


def mine_drafts_block(entries: list[dict]) -> None:
    emit(f"Your GitHub drafts ({len(entries)})")
    for e in entries:
        emit(f"--{row_text(e)}", href=e["url"])


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


def _response_text(job: dict, nick: dict[str, str]) -> str:
    """'Blocking · Types.elm:42 · Mykal replied: "Moved the check…" · addressed'."""
    from triage import comment_kind as ck
    where = (f"{Path(job.get('path') or '').name}" + (f":{job['line']}" if job.get("line") else "")
             if job.get("path") else "thread")
    actor = _who(job.get("actor") or "", nick)
    state = {"ready": (job.get("verdict") or "").replace("_", " "),
             "running": "drafting…" if job.get("trigger") == "mention" else "checking the commits…",
             "queued": "drafting…" if job.get("trigger") == "mention" else "checking the commits…",
             "failed": "follow-up failed"}.get(job["status"], job["status"])
    if job.get("trigger") == "mention":
        where = {"issue": "PR comment", "review": "review"}.get(job.get("where"), where)
        state = "reply drafted" if job["status"] == "ready" else state
        return f'Mention · {where} · {actor}: "{ck.summary(job.get("body") or "", 50)}" · {state}'
    kind = "blocking" if priority.job_blocking(job) else ck.kind(job.get("my_comment") or "")
    if job.get("trigger") == "reply":
        reply = next((c["body"] for c in reversed(job.get("thread") or [])
                      if c.get("author") == job.get("actor")), "")
        what = f'{actor} replied: "{ck.summary(reply, 50)}"' if reply else f"{actor} replied"
    else:
        what = "code changed under your comment"
    return f"{ck.WORDS.get(kind, kind)} · {where} · {what} · {state}"


def _response_detail(job: dict) -> None:
    """Third-level submenu of one response: your comment, Claude's drafted reply, and
    the per-reply actions. "Post this reply" posts exactly the reply file's text (the
    click is your approval); "Edit first" opens it, and posting then uses whatever
    the file holds."""
    emit("----Open the thread on GitHub", href=job["url"])
    if job.get("trigger") == "mention":
        emit(f"----Their comment: {truncate(sanitize(job.get('body') or ''), 80)}",
             color="#888888", tooltip=sanitize(job.get("body") or ""))
    else:
        emit(f"----Your comment: {truncate(sanitize(job.get('my_comment') or ''), 80)}",
             color="#888888", tooltip=sanitize(job.get("my_comment") or ""))
    if job["status"] == "failed":
        emit(f"----{sanitize(job.get('error') or 'unknown error')}", color="#888888")
    if job["status"] == "ready":
        reply_file = Path(job["reply_file"])
        reply = reply_file.read_text() if reply_file.exists() else ""
        emit("----Drafted reply:", color="#888888")
        for line in reply.strip().splitlines():
            if line.strip():
                emit(f"----{truncate(sanitize(line), 90)}", tooltip=sanitize(line))
        emit("----Post this reply", bash=str(WATCHER), param1="post", param2=job["id"],
             terminal="false", refresh="true")
        emit("----Edit first", bash="/usr/bin/open", param1="-t", param2=job["reply_file"],
             terminal="false")
        _read_item("----Read Claude's follow-up", job["report"])
    emit("----Dismiss this one", bash=str(WATCHER), param1="dismiss", param2=job["id"],
         terminal="false", refresh="true")


# Reasons that mean re-approval was never in play, so the menu doesn't mention them.
_NOT_IN_PLAY = ("auto re-approval is off", "your latest decision on the PR isn't an approval",
                "you already approved the current head")


def reapproval_hold(jobs: list[dict]) -> str:
    """'Not re-approving: <why>' from the latest checked job, when you approved the PR,
    automatic re-approval is on, and something holds it; '' otherwise."""
    checked = [j for j in jobs if j.get("status") == "ready" and j.get("reapproval")]
    if not checked:
        return ""
    why = max(checked, key=lambda j: j.get("finished_at") or 0)["reapproval"]
    return "" if why in _NOT_IN_PLAY else f"Not re-approving: {why}"


def followup_block(e: dict, nick: dict[str, str]) -> None:
    """Others' PR you reviewed, where someone responded: every response in one place,
    each with its own submenu, then the PR's actions."""
    url = e["url"]
    st = e.get("st") or {}
    jobs = e.get("jobs") or []
    emit(row_text(e), href=url)
    _pr_header(e)
    if st.get("rerequested_at"):
        emit(f"--{_who(st.get('author', ''), nick)} re-requested your review "
             f"{when(st['rerequested_at'])}", color="#888888")
    if jobs:
        emit(f"--Responses and mentions ({len(jobs)})"
             if any(j.get("trigger") == "mention" for j in jobs)
             else f"--Responses ({len(jobs)})", color="#888888")
        for job in jobs:
            emit(f"--{truncate(sanitize(_response_text(job, nick)), 110)}")
            _response_detail(job)
        ready = [j for j in jobs if j["status"] == "ready"]
        if ready and len(ready) == len(jobs):
            emit("--Claude drafted a reply to " + ("it" if len(jobs) == 1 else "each"),
                 color="#888888")
        held = reapproval_hold(jobs)
        if held:
            emit(f"--{truncate(sanitize(held), 110)}", color="#888888")
    emit("-----")
    ready = [j for j in jobs if j["status"] == "ready"]
    if len(ready) == 1:
        _read_item("--Read Claude's follow-up", ready[0]["report"])
    elif ready:
        emit("--Read Claude's follow-up")
        for j in ready:
            where = Path(j.get("path") or "").name + (f":{j['line']}" if j.get("line") else "")
            _read_item(f"----{where or 'thread'}", j["report"])
    if ready:
        emit("--Post the drafted replies", bash=str(WATCHER), param1="post",
             param2=",".join(j["id"] for j in ready), terminal="false", refresh="true")
    head = st.get("head_sha") or ((jobs[0].get("pr") or {}).get("head") if jobs else "") or ""
    _draft_actions(_key(url), head, url)
    emit("--Open on GitHub", href=url)
    _dismiss(e, jobs)


def other_block(e: dict, nick: dict[str, str]) -> None:
    """A ⚪ row: nothing for you to do; the submenu says why."""
    url = e["url"]
    emit(row_text(e), href=url)
    _pr_header(e)
    author = (e.get("st") or {}).get("author")
    if author:
        emit(f"--by {_who(author, nick)}", color="#888888")
    emit("-----")
    emit("--Open on GitHub", href=url)


def render_menu(sections: dict, *, spinner: bool, error: str | None,
                have_data: bool, nick: dict[str, str], required: int = 2) -> None:
    title = sections.get("title") or "✓"

    # --- menu bar title (single line) ---
    if spinner:
        emit("" if title == "✓" else title, sfimage="arrow.triangle.2.circlepath")
    elif error and not have_data:
        emit(sfimage="exclamationmark.triangle", sfcolor="orange")
    else:
        emit(title)

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
        emit(priority.LEGEND + " · hold ⌥ to dismiss", color="#888888", size="12")
        print("---")
        emit(priority.SECTION_NAMES[priority.MINE], color="#888888")
        mine_rows(sections["mine"], nick, required)
        if sections["mine_drafts"]:
            mine_drafts_block(sections["mine_drafts"])
        if not sections["mine"] and not sections["mine_drafts"]:
            emit("No open PRs", color="#6b6b70")

        if sections["followups"]:
            print("---")
            emit(priority.SECTION_NAMES[priority.FOLLOWUP], color="#888888")
            for e in sections["followups"]:
                followup_block(e, nick)

        print("---")
        emit("Reviews you owe", color="#888888")
        for e in sections["owed"]:
            owed_block(e, nick)
        if not sections["owed"]:
            emit("No reviews owed", color="#6b6b70")

        if sections["other"]:
            print("---")
            emit(priority.SECTION_NAMES[priority.OTHER], color="#888888")
            for e in sections["other"]:
                other_block(e, nick)

    # --- footer actions ---
    print("---")
    emit("Refresh now", bash=str(LAUNCHER), param1="--fetch", terminal="false",
         sfimage="arrow.clockwise")
    emit(f"Updated {cache_label()} · {watcher_label()}", color="#888888")


def _people(fetch: bool = False):
    """GitHub profile names (names.py); the instant render only reads the cache."""
    try:
        import names
        return names.book(fetch=fetch)
    except Exception:  # noqa: BLE001
        return {}


def _reviews_on() -> bool:
    """False when review.reviewer_agent is empty: the menu offers no reviews."""
    try:
        import settings
        return bool(settings.load()["review"]["reviewer_agent"])
    except Exception:  # noqa: BLE001
        return True


def _required_approvals() -> int:
    try:
        import settings
        return int(settings.load()["watcher"]["required_approvals"])
    except Exception:  # noqa: BLE001
        return 2


def load_dismissed() -> dict[str, str]:
    """PRs whose row you dismissed: url → the activity timestamp at the time. The
    row gets its dot back once something newer happens on it."""
    try:
        data = json.loads((state_dir() / "dismissed.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def dismiss(url: str, acted_at: str, job_ids: str = "") -> None:
    """Take a row's dot away until activity newer than `acted_at` arrives: record it
    for the menu, tell the watcher (so the next response notifies afresh), and
    dismiss the row's follow-up jobs."""
    data = load_dismissed()
    data[url] = acted_at or datetime.now(timezone.utc).isoformat()
    try:
        (state_dir() / "dismissed.json").write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass
    try:
        from watcher import followup, state
        state.mark_cleared(url, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        for jid in filter(None, job_ids.split(",")):
            followup.dismiss(jid)
    except Exception:  # noqa: BLE001 — the menu must keep working without the watcher
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

    for flag in ("--dismiss", "--dismiss-mine"):
        if flag in args:
            i = args.index(flag)
            rest = args[i + 1:] + ["", "", ""]
            if rest[0]:
                dismiss(rest[0], rest[1], rest[2])
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
    nick = _people()
    sections = build_sections(cache if have_data else {}, _followup_jobs(), load_dismissed(),
                              required, nick, prio=_priority_settings())
    render_menu(sections, spinner=fetching, error=cache.get("error"),
                have_data=have_data, nick=nick, required=required)
    return 0


if __name__ == "__main__":
    sys.exit(main())
