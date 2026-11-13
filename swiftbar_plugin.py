#!/usr/bin/env python3
"""SwiftBar plugin: the one PR menu. Each PR gets one row with a priority dot.

Output follows the SwiftBar/xbar plugin protocol:
  * lines before the first `---` become the menu bar title
  * lines after it become the dropdown

Run by the launcher in swiftbar-plugins/prdigest.1h.sh (hourly + on open).

Every PR is classified once by triage/priority.py, which gives it a section, a color
(🔴 act now, 🟡 act soon, 🟢 new, ⚪ no action needed, or no dot) and the reason in
words. Each row reads `<dot> <ticket> · <what happened> · <PR title>`; a grey legend
at the top explains the dots. Sections, top to bottom:
  1. My PRs — every open PR you author (triage/mine.py), under the sub-headers
     Needs you, Waiting on others and Ready. The submenu says why it has its dot,
     where review stands (approvals with dates, change requests and whose move
     each is, who hasn't reviewed), every comment to answer by kind and linked to its
     thread, and a Claude session to work through them. GitHub drafts with nothing to
     answer collapse into one submenu.
  2. Other PRs — everyone else's PRs, under two sub-headers and a collapsed submenu:
     - Needs you: follow-ups (others' PRs you reviewed where someone responded — a
       re-request, or replies / code changes under your comments — and any PR where
       someone @mentioned you; the watcher drafts a reply to each, and the submenu
       lists every response with its own drafted reply and actions) together with
       reviews you owe (PRs you haven't reviewed, or whose review of yours was
       dismissed, with the review draft's status and actions, run through
       review-drafter/menubar.sh, which calls bin/review-pr), most severe first.
     - Waiting on others: PRs you left comments on whose author hasn't responded.
     - Done for you: ✅, what pr-digest did on its own today (auto-fixes it pushed,
       watcher/autofix.py, and automatic re-approvals), each with its proof in the
       submenu. Never counted; ⌥ dismisses it, and it's gone the next day.
     - FYI: ⚪, things to know that ask nothing of you (watcher/fyi.py): an approval
       with no comment or only praise, a PR of yours that became Ready, a review you
       owed that's no longer needed. Never counted; gone the next time the menu
       opens after you've seen it.
     - Nothing needed (N): ⚪, every other open PR the GitHub searches or the triage
       turned up, with what happened in words (parked, approved, drafts). Never
       counted.
  A PR of yours with auto-fixes in flight, stopped or dry-run lists them in its
  submenu under Auto-fix, with each step's outcome.

Each PR appears in exactly one place. Inside a sub-header rows go red, yellow, green,
then no dot. The menu-bar title counts red and yellow PRs (`🔴2 🟡1`), or shows ✓.
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
from watcher import approval  # noqa: E402
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
    from triage import jira
    from triage import priority as P
    titles = [r.get("title") or "" for r in mine_rows]
    titles += [st.get("title") or "" for bucket in reviews.values() for st in bucket]
    keys = {m.group(0) for t in titles for m in [P.TICKET_RE.search(t)] if m}
    return {
        "me": me,
        "reviews": {
            "needs_you": [_slim(st) for st in ordered_needs],
            **{b: [_slim(st) for st in reviews[b]]
               for b in ("author", "well_approved", "done", "unreadable")},
        },
        "mine": mine_rows,
        "legacy": legacy,
        "groups": jira.groups(keys, cfg),
        "error": None,
    }


def write_status(data: dict) -> None:
    """Refresh output/status/ (see status_report.py) from freshly fetched data."""
    try:
        import status_report
        required = _required_approvals()
        people = _people(fetch=True)
        status_report.write(build_sections(data, _followup_jobs(), load_dismissed(), required,
                                           people, prio=_priority_settings(),
                                           autofix_jobs=_autofix_jobs(),
                                           drafts=_draft_states(data),
                                           approval_needs=_approval_needs()),
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
    """[priority], plus `pod` from [triage]: whose PRs are red reviews from the start."""
    try:
        import settings
        cfg = settings.load()
        return {**cfg["priority"], "pod": list(cfg["triage"].get("pod") or [])}
    except Exception:  # noqa: BLE001
        return {"stale_after_days": 2, "max_engaged_for_stale": 1, "pod": []}


def _group_key(e: dict, groups: dict[str, dict]) -> str:
    key = priority.ticket(e.get("title") or "", e["url"])
    return (groups.get(key) or {}).get("key") or key


def grouped(entries: list[dict], groups: dict[str, dict]) -> list[dict]:
    """`entries` (already in menu order) with every set of two or more PRs that share a
    group key (their ticket's Jira group, or the ticket itself) replaced by one group
    entry at the place of its first, most urgent, child: {"kind": "group", "key",
    "summary", "label" ("story · 2 sub-tasks", or "2 PRs" for one ticket across
    repos), "children" (each with its `tree` marker, ├ or └), "color" (the most severe
    child's), "group" (the first child's sub-header), "url", "title",
    "waiting_since"}."""
    buckets: dict[str, list[dict]] = {}
    for e in entries:
        buckets.setdefault(_group_key(e, groups), []).append(e)
    out, seen = [], set()
    for e in entries:
        g = _group_key(e, groups)
        if g in seen:
            continue
        seen.add(g)
        kids = buckets[g]
        if len(kids) == 1:
            out.append(e)
            continue
        first = priority.ticket(kids[0].get("title") or "", kids[0]["url"])
        info = groups.get(first) or {}
        summary = info.get("summary") or _title(kids[0])
        # A parent of sub-tasks, or one ticket whose PRs span several repos.
        parent = any(priority.ticket(k.get("title") or "", k["url"]) != g for k in kids)
        label = (f"{info.get('type') or 'ticket'} · {len(kids)} sub-tasks" if parent
                 else f"{len(kids)} PRs")
        out.append({"kind": "group", "key": g, "summary": summary, "label": label,
                    "children": [{**k, "tree": "└ " if i == len(kids) - 1 else "├ "}
                                 for i, k in enumerate(kids)],
                    "color": max((k.get("color") for k in kids), key=priority.rank),
                    "group": kids[0].get("group"), "url": kids[0]["url"],
                    "title": f"{g} {summary}",
                    "waiting_since": min((k.get("waiting_since") or "~" for k in kids))})
    return out


def build_sections(data: dict, followup_jobs: list[dict],
                   dismissed: dict[str, str] | None = None, required: int = 2,
                   nick: dict[str, str] | None = None, now: datetime | None = None,
                   prio: dict | None = None, autofix_jobs: list[dict] | None = None,
                   drafts: dict[str, dict] | None = None,
                   approval_needs: dict[str, dict] | None = None) -> dict:
    """Sort cached data into the menu's sections through triage/priority.py.

    Returns {"mine", "mine_drafts", "followups", "owed", "other", "others_needs",
    "others_waiting", "nothing", "done": [entry], "counts": {color: n}, "title": str,
    "count": int}. `done` is Done for you (priority.done_for_you): today's auto-fixes
    (`autofix_jobs`, watcher/autofix.py) and automatic re-approvals, never counted.
    Your PRs' entries carry their auto-fix jobs. `others_needs` is the follow-ups and
    reviews you owe together (Other PRs →
    Needs you); `others_waiting` and `nothing` split `other` by its group. The `_view`
    lists are what the menu draws: the same rows, with PRs that share a Jira group
    (data["groups"], triage/jira.py) folded into one group entry (`grouped`). An entry is a
    priority.classify result (url, title, section, color, reason, why, waiting_since,
    activity_at, dismissed, row | st, jobs).

    Each PR appears in exactly one section. My PRs go by sub-header (Needs you,
    Waiting on others, Ready: priority.MINE_GROUPS), then as below. Inside a section
    rows go red, yellow, green, then no dot; inside a color the longest wait first,
    except Reviews you owe, which keeps reviews.ranked() order. GitHub drafts of yours
    with no dot collapse into `mine_drafts`. `dismissed` maps a PR url to the activity
    time it was dismissed at; the row loses its dot until something newer happens.

    `drafts` (PR url → {"state", "tooltip"}) is given while automatic review drafts
    are on (`_draft_states`): a review you owe shows its draft's state, and stays off
    the menu until its draft is ready (priority.classify).

    `approval_needs` (PR url → watcher/approval.py `needs`, saved by the watcher;
    `_approval_needs`) is held against `followup_jobs` here, so a follow-up's entry
    says "Ready to approve" as soon as Claude's last verdict is in.

    `title` is the menu-bar title (priority.title) and `count` the PRs it counts."""
    nick = nick or {}
    now = now or datetime.now(timezone.utc)
    prio = prio or {"stale_after_days": 2, "max_engaged_for_stale": 1}
    reviews = data.get("reviews") or {}
    entries = priority.classify(
        data.get("mine") or [], reviews, followup_jobs, nick=nick, now=now,
        required=required, stale_after_days=prio["stale_after_days"],
        max_engaged=prio["max_engaged_for_stale"], dismissed=dismissed or {},
        autofix=autofix_jobs or [], pod=frozenset(prio.get("pod") or ()), drafts=drafts,
        approvals={url: approval.status(n, followup_jobs)
                   for url, n in (approval_needs or {}).items()})

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
    # Other PRs → Needs you: follow-ups and reviews you owe together, most severe first;
    # inside a color follow-ups lead, then reviews in ranked order.
    others_needs = sorted(followups + owed, key=lambda e: (
        -priority.rank(e.get("color")), e["section"] != priority.FOLLOWUP,
        ranked.get(e["url"], len(ranked)), e.get("waiting_since") or "~"))
    others_waiting = sorted((e for e in other if e.get("group") == priority.WAITING),
                            key=lambda e: e.get("waiting_since") or "~")
    nothing = [e for e in other if e.get("group") != priority.WAITING]

    groups = data.get("groups") or {}
    others = grouped(others_needs + others_waiting, groups)

    shown = mine_rows + followups + owed
    counts = priority.counts(shown)
    return {
        "mine_view": grouped(mine_rows, groups),
        "others_needs_view": [e for e in others if e.get("group") == priority.NEEDS],
        "others_waiting_view": [e for e in others if e.get("group") != priority.NEEDS],
        "mine": mine_rows,
        "mine_drafts": mine_drafts,
        "followups": followups,
        "owed": owed,
        "other": other,
        "others_needs": others_needs,
        "others_waiting": others_waiting,
        "nothing": nothing,
        "done": priority.done_for_you(autofix_jobs or [], followup_jobs, nick, now),
        "counts": counts,
        "title": priority.title(shown),
        "count": sum(counts[c] for c in priority.COUNTED),
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
# Indents a story's PRs under it. Blank Braille cells are printable characters, not
# whitespace, so a leading run of them isn't trimmed away the way spaces are.
TREE_INDENT = "\u2800\u2800"


def subhead(name: str) -> None:
    """A sub-header inside a section: '── Needs you ──'."""
    emit(f"── {name} ──", color="#888888", size="12")


def row_text(e: dict, limit: int = ROW_MAX) -> str:
    """'🔴 ABC-123 · Woods left 8 comments (3 suggestions, 5 nits) · Show the exact…':
    the ticket first so the row always says which PR it is, then what happened, then
    as much of the title as fits. A PR under a story starts with its tree marker and
    names its repo: '⠀⠀├ 🟡 ABC-2 processservice · Review it · …'."""
    key = priority.ticket(e.get("title") or "", e["url"])
    tree = e.get("tree") or ""
    if tree:
        key += f" {e['url'].rstrip('/').split('/')[-3]}"
    d = priority.dot(e.get("color"))
    head = f"{d} {key}" if d else key
    if tree:
        head = TREE_INDENT + tree + head
    head += f" · {sanitize(e.get('reason') or '')}"
    title = sanitize(_title({"title": e.get("title") or ""}))
    if not title:
        return head
    return f"{head} · {truncate(title, max(limit - len(head) - 3, 16))}"


def _pr_header(e: dict, sub: str = "--") -> None:
    """The submenu's first lines: repo#number with the full title, then why. `sub` is
    the submenu's prefix ("----" for a row nested one level down)."""
    emit(f"{sub}{_key(e['url'])} · {truncate(sanitize(_title(e)), 80)}", href=e["url"])
    if e.get("why"):
        emit(f"{sub}{sanitize(e['why'])}", color="#888888")


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
    emit(row_text(e), href=url, tooltip=sanitize(e.get("tooltip") or ""))
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
                      "path": t.get("path") or "", "url": t.get("url") or "", "body": body,
                      "thread_id": t.get("thread_id") or ""})
    for c in row.get("unanswered_comments") or []:
        items.append({"kind": c.get("kind") or ck.kind(c.get("body", "")),
                      "author": c.get("author", "?"),
                      "summary": c.get("summary") or ck.summary(c.get("body", "")),
                      "path": "", "url": c.get("url") or "", "body": c.get("body", ""),
                      "thread_id": ""})
    items.sort(key=lambda i: ck.sort_key(i["kind"]))
    return items


def answer_drafts(row: dict, jobs: list[dict]) -> dict[str, dict]:
    """Comment url -> the answer job drafting your reply to it (watcher/followup.py),
    for every comment still waiting on you on this PR that has one."""
    try:
        from watcher import followup
    except Exception:  # noqa: BLE001 — the menu must keep working without the watcher
        return {}
    by_id = {j["id"]: j for j in jobs if j.get("status") in ("queued", "running", "ready", "failed")}
    out = {}
    for ask in followup.answer_asks(row):
        job = by_id.get(followup.answer_job_id(ask))
        if job and ask.get("url"):
            out[ask["url"]] = job
    return out


def draft_note(job: dict) -> str:
    """A drafted reply's state, for its comment's line in the menu."""
    if job["status"] in ("queued", "running"):
        return "Claude is drafting a reply"
    if job["status"] == "failed":
        return "draft failed"
    from watcher import followup
    return f"reply drafted, {followup.STANCE_WORDS.get(job.get('verdict'), 'ready')}"


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
    fixes = shown_fixes(e.get("autofix") or [])
    by_thread = {j.get("thread_id"): j for j in fixes}
    drafts = answer_drafts(row, e.get("answers") or [])
    if asks:
        emit("-----")
        emit(f"--To answer ({len(asks)})", color="#888888")
        for i in asks:
            where = f" · {Path(i['path']).name}" if i["path"] else ""
            job = by_thread.get(i["thread_id"]) if i["thread_id"] else None
            draft = drafts.get(i["url"])
            note = f" · {fix_state(job)}" if job else (f" · {draft_note(draft)}" if draft else "")
            emit(f"--{ck.WORDS.get(i['kind'], i['kind'])} · {_who(i['author'], nick)}{where} — "
                 f"{truncate(sanitize(i['summary']), 80)}{sanitize(note)}",
                 href=i["url"] or url, tooltip=truncate(sanitize(i["body"]), 600))
            if draft and draft["status"] in ("ready", "failed"):
                _response_detail(draft)
    for b in row.get("blockers") or []:
        draft = drafts.get(b.get("url") or "")
        if draft:
            emit(f"--Change request · {_who(b['author'], nick)} — "
                 f"{truncate(sanitize(ck.summary(b.get('body') or '')), 70)} · {draft_note(draft)}",
                 href=b.get("url") or url)
            if draft["status"] in ("ready", "failed"):
                _response_detail(draft)
    ready = [j for j in drafts.values() if j["status"] == "ready"]
    if ready:
        emit("-----")
        if len(ready) == 1:
            _read_item("--Read Claude's drafted reply", ready[0]["report"])
        else:
            emit(f"--Read Claude's drafted replies ({len(ready)})")
            for j in ready:
                _read_item(f"----{_who(j.get('actor') or '', nick)} · "
                           f"{truncate(sanitize(ck.summary(j.get('body') or '')), 60)}", j["report"])
        emit("--Post the drafted replies" if len(ready) > 1 else "--Post the drafted reply",
             bash=str(WATCHER), param1="post", param2=",".join(j["id"] for j in ready),
             terminal="false", refresh="true")
    if fixes:
        autofix_lines(fixes, nick)
    emit("-----")
    if asks or row.get("blockers"):
        _claude_item("--Go through these with Claude", _respond_prompt(url))
    else:
        _claude_item("--Open in Claude", _status_prompt(f"Let's look at my PR {url}.", url))
    emit("--Open on GitHub", href=url)
    # The ⌥ alternate goes after the submenu: SwiftBar hangs `--` lines on the item
    # just above them, so placing it first would give the submenu to the alternate.
    _dismiss(e)


# Auto-fix job statuses (watcher/autofix.py) shown in the PR's submenu; a finished fix
# shows under Done for you instead, and a cleared one nowhere.
SHOWN_FIXES = ("queued", "running", "escalated", "dry_run")


def shown_fixes(jobs: list[dict]) -> list[dict]:
    return [j for j in jobs if j.get("status") in SHOWN_FIXES]


def fix_state(job: dict) -> str:
    """One auto-fix job's state in a few words."""
    status, dry = job.get("status"), job.get("dry_run")
    reason = job.get("reason") or ""
    if status in ("queued", "running"):
        return "auto-fix dry run running" if dry else "Claude is fixing it"
    if status == "escalated":
        return f"{'dry run stopped' if dry else 'couldn’t auto-fix'}: {reason}"
    if status == "dry_run":
        return f"dry run: would commit “{job.get('summary') or ''}”"
    return status or ""


def autofix_lines(fixes: list[dict], nick: dict[str, str]) -> None:
    """The PR's auto-fixes that aren't finished, each with a submenu of the steps it
    went through, where it stopped and, for a dry run, the commit it would make."""
    from triage import comment_kind as ck
    emit("-----")
    emit(f"--Auto-fix ({len(fixes)})", color="#888888")
    for j in fixes:
        where = Path(j.get("path") or "").name + (f":{j['line']}" if j.get("line") else "")
        text = (f"{ck.WORDS.get(j.get('kind'), j.get('kind') or '')} · "
                f"{_who(j.get('reviewer') or '', nick)} · {where} · {fix_state(j)}")
        emit(f"--{truncate(sanitize(text), 110)}", tooltip=sanitize(j.get("reason") or ""))
        if j.get("comment_url"):
            emit("----Open the comment on GitHub", href=j["comment_url"])
        for s in j.get("steps") or []:
            detail = f": {s['detail']}" if s.get("detail") else ""
            emit(f"----{'✓' if s.get('ok') else '✗'} {s.get('step')}{truncate(sanitize(detail), 90)}",
                 color="#888888", tooltip=sanitize(s.get("detail") or ""))
        if j.get("detail"):
            emit(f"----{truncate(sanitize(j['detail']), 90)}", color="#888888",
                 tooltip=truncate(sanitize(j["detail"]), 600))
        if j.get("would_commit"):
            emit("----Would commit:", color="#888888")
            for line in j["would_commit"].strip().splitlines():
                if line.strip():
                    emit(f"----{truncate(sanitize(line), 90)}", color="#888888")


def done_block(e: dict, nick: dict[str, str], org: str = "avetta") -> None:
    """One PR in Done for you: what was done and when, and a submenu with the proof
    for each fix (its commit, the reply as posted, the resolved thread) and each
    re-approval, Open in Claude, and ⌥ Dismiss."""
    from triage import comment_kind as ck
    url = e["url"]
    emit(row_text({**e, "reason": f"{e.get('reason') or ''} · {when(e.get('done_at'))}"}),
         href=url)
    _pr_header(e)
    repo = url.rstrip("/").split("/")[-3]
    for j in e.get("fixes") or []:
        where = Path(j.get("path") or "").name
        emit(f"--Fixed {ck.WORDS.get(j.get('kind'), j.get('kind') or '').lower()} · "
             f"{_who(j.get('reviewer') or '', nick)}" + (f" · {where}" if where else "")
             + f" — {truncate(sanitize(j.get('summary') or ''), 70)} · {when(j.get('done_at'))}")
        sha = j.get("commit_sha") or ""
        if sha:
            emit(f"----Commit {sha[:7]}", href=f"https://github.com/{org}/{repo}/commit/{sha}")
        if j.get("reply_text"):
            emit("----Reply as posted:", color="#888888")
            for line in j["reply_text"].strip().splitlines():
                if line.strip():
                    emit(f"----{truncate(sanitize(line), 90)}", tooltip=sanitize(line))
        thread = j.get("reply_url") or j.get("comment_url") or url
        emit("----Open the resolved thread" if j.get("resolved")
             else "----Open the thread (not resolved)", href=thread)
        if j.get("rerequested"):
            emit(f"----Re-requested {_who(j.get('reviewer') or '', nick)}", color="#888888")
    for j in e.get("reapprovals") or []:
        emit(f"--Re-approved: {_who(j.get('actor') or '', nick)} did what you asked · "
             f"{when(j.get('reapproved_at') or j.get('finished_at'))}",
             href=j.get("posted_url") or url)
    emit("-----")
    _claude_item("--Open in Claude", _status_prompt(f"Let's look at my PR {url}.", url))
    emit("--Open on GitHub", href=url)
    ids = [f"a:{j['id']}" for j in e.get("fixes") or []]
    ids += [f"f:{j['id']}" for j in e.get("reapprovals") or []]
    _dismiss_line(priority.ticket(e.get("title") or "", url), bash=str(LAUNCHER),
                  param1="--dismiss-done", param2=",".join(ids))


def done_rows(entries: list[dict], nick: dict[str, str]) -> None:
    """Done for you: what pr-digest did on its own today, newest first."""
    emit(priority.SECTION_NAMES[priority.DONE_SECTION], color="#888888")
    org = _org()
    for e in entries:
        done_block(e, nick, org)


def fyi_rows(items: list[dict]) -> None:
    """FYI: ⚪ things to know that ask nothing of you (watcher/fyi.py), oldest first.
    Each row opens its `href` (a Jira ticket the watcher moved), else its PR; it's gone
    the next time the menu opens."""
    emit("FYI", color="#888888")
    for it in items:
        pr = it.get("pr") or {}
        if not pr.get("url"):
            continue
        emit(row_text({"color": priority.LOW, "title": pr.get("title") or "", "url": pr["url"],
                       "reason": f"{it.get('row') or it.get('headline') or ''} · {when(it.get('at'))}"}),
             href=it.get("href") or pr["url"], tooltip=sanitize(it.get("headline") or ""))


def fyi_items() -> list[dict]:
    """The FYI rows for this render, marking them seen when the render comes from
    opening the menu (watcher/fyi.py on_render). Never breaks the menu."""
    try:
        from watcher import fyi
        return fyi.on_render(os.environ.get("SWIFTBAR_PLUGIN_REFRESH_REASON", ""))
    except Exception:  # noqa: BLE001
        return []


def dismiss_done(spec: str) -> None:
    """Take Done for you rows away: `spec` is a:<id> for auto-fixes and f:<id> for
    re-approvals, comma-separated."""
    try:
        from watcher import autofix, followup
    except Exception:  # noqa: BLE001 — the menu must keep working without the watcher
        return
    for item in filter(None, spec.split(",")):
        kind, _, jid = item.partition(":")
        try:
            (autofix.dismiss_done if kind == "a" else followup.dismiss_done)(jid)
        except (OSError, ValueError, KeyError):
            continue


def mine_rows(entries: list[dict], nick: dict[str, str], required: int) -> None:
    """Your PRs, each sub-header (priority.MINE_GROUPS) once above its rows. `entries`
    come sorted by sub-header (build_sections)."""
    group = None
    for e in entries:
        if e.get("group") != group:
            group = e.get("group")
            if group in priority.MINE_GROUPS:
                subhead(priority.MINE_GROUPS[group])
        if e.get("kind") == "group":
            group_block(e, lambda kid: mine_block(kid, nick, required))
        else:
            mine_block(e, nick, required)


def group_block(e: dict, child) -> None:
    """A Jira story and its PRs, as a tree: a header row for the story (no dot, linked
    to the ticket when `[jira] browse_url` is set), then each PR drawn by `child` as an
    ordinary row with its tree marker, so each keeps its own dot and submenu."""
    title = truncate(sanitize(e.get("summary") or ""), 70)
    emit(f"📘 {e['key']} {title} · {e.get('label') or ''}".rstrip(" ·"),
         href=_jira_url(e["key"]))
    for kid in e["children"]:
        child(kid)


def _jira_url(key: str) -> str:
    try:
        import settings
        base = settings.load()["jira"]["browse_url"]
    except Exception:  # noqa: BLE001
        return ""
    return f"{base}{key}" if base else ""


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
    if job.get("trigger") == "change_request":
        said = next((r.get("body") for r in job.get("responses") or []
                     if r.get("trigger") != "pushed" and r.get("body")), "")
        what = (f'{actor}: "{ck.summary(said, 50)}"' if said else f"{actor} pushed commits")
        if said and "pushed" in (job.get("triggers") or []):
            what += " + commits"
        return f"Change request · {what} · {state}"
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
    emit("----Open on GitHub" if job.get("trigger") == "change_request"
         else "----Open the thread on GitHub", href=job["url"])
    if job.get("trigger") in ("mention", "answer"):
        emit(f"----Their comment: {truncate(sanitize(job.get('body') or ''), 80)}",
             color="#888888", tooltip=sanitize(job.get("body") or ""))
    if job.get("trigger") == "answer" and job["status"] == "ready" and job.get("note"):
        emit(f"----Claude: {truncate(sanitize(job['note']), 100)}", color="#888888",
             tooltip=sanitize(job["note"]))
    else:
        mine = "Your change request" if job.get("trigger") == "change_request" else "Your comment"
        emit(f"----{mine}: {truncate(sanitize(job.get('my_comment') or ''), 80)}",
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
        _read_item("----Read Claude's draft" if job.get("trigger") == "answer"
                   else "----Read Claude's follow-up", job["report"])
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


def approval_text(ok: dict) -> str:
    """approval.body's text as the menu shows it: links reduced to their short sha."""
    text = approval.body(ok["repo"], ok["head"], ok["verb"], _org())
    return sanitize(re.sub(r"\[([0-9a-f]+)\]\([^)]*\)", r"\1", text))


def followup_block(e: dict, nick: dict[str, str]) -> None:
    """Others' PR you reviewed, where someone responded: every response in one place,
    each with its own submenu, then the PR's actions. When Claude found everything your
    review asked for addressed (the entry's `approval`, watcher/approval.py), the
    actions lead with "Approve", which posts `approval_text` (the click is your
    approval), and the row's why says so; otherwise a grey line says what holds an
    approval back."""
    url = e["url"]
    st = e.get("st") or {}
    jobs = e.get("jobs") or []
    ok = e.get("approval") or {}
    emit(row_text(e), href=url)
    _pr_header(e)
    if ok.get("why"):
        emit(f"--Not ready to approve: {truncate(sanitize(ok['why']), 100)}",
             color="#888888", tooltip=sanitize(ok["why"]))
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
    if ok.get("ready"):
        text = approval_text(ok)
        emit("--Re-approve" if ok["verb"] == "reapprove" else "--Approve", bash=str(WATCHER),
             param1="approve", param2=ok["pr_id"], param3=ok["head"], terminal="false",
             refresh="true", sfimage="checkmark.seal", tooltip=text)
        emit(f"--Posts: {truncate(text, 100)}", color="#888888", tooltip=text)
    ready = [j for j in jobs if j["status"] == "ready"]
    if len(ready) == 1:
        _read_item("--Read Claude's follow-up", ready[0]["report"])
    elif ready:
        emit("--Read Claude's follow-up")
        for j in ready:
            where = Path(j.get("path") or "").name + (f":{j['line']}" if j.get("line") else "")
            if j.get("trigger") == "change_request":
                where = "your change request"
            _read_item(f"----{where or 'thread'}", j["report"])
    if ready:
        emit("--Post the drafted replies", bash=str(WATCHER), param1="post",
             param2=",".join(j["id"] for j in ready), terminal="false", refresh="true")
    head = st.get("head_sha") or ((jobs[0].get("pr") or {}).get("head") if jobs else "") or ""
    _draft_actions(_key(url), head, url)
    emit("--Open on GitHub", href=url)
    _dismiss(e, jobs)


def other_block(e: dict, nick: dict[str, str], depth: int = 0) -> None:
    """A row with nothing for you to do; the submenu says why. `depth` 1 nests it in
    the Nothing needed submenu."""
    url = e["url"]
    top, sub = "--" * depth, "--" * (depth + 1)
    emit(top + row_text(e), href=url)
    _pr_header(e, sub)
    author = (e.get("st") or {}).get("author")
    if author:
        emit(f"{sub}by {_who(author, nick)}", color="#888888")
    emit(sub + "---")
    emit(f"{sub}Open on GitHub", href=url)


def others_rows(sections: dict, nick: dict[str, str]) -> None:
    """Other PRs: Needs you (follow-ups and reviews you owe), Waiting on others, then
    everything else collapsed into Nothing needed (N)."""
    emit(priority.OTHERS_NAME, color="#888888")
    groups = priority.MINE_GROUPS
    def one(e: dict) -> None:
        if e.get("section") == priority.FOLLOWUP:
            followup_block(e, nick)
        elif e.get("section") == priority.OWED:
            owed_block(e, nick)
        else:
            other_block(e, nick)

    def row(e: dict) -> None:
        if e.get("kind") == "group":
            group_block(e, one)
        else:
            one(e)

    needs = sections.get("others_needs_view", sections["others_needs"])
    waiting = sections.get("others_waiting_view", sections["others_waiting"])
    if needs:
        subhead(groups[priority.NEEDS])
        for e in needs:
            row(e)
    if waiting:
        subhead(groups[priority.WAITING])
        for e in waiting:
            row(e)
    if not needs and not waiting:
        emit("Nothing waiting on you", color="#6b6b70")
    if sections.get("done"):
        print("---")
        done_rows(sections["done"], nick)
    if sections.get("fyi"):
        print("---")
        fyi_rows(sections["fyi"])
    if sections["nothing"]:
        emit(f"{priority.NOTHING_NAME} ({len(sections['nothing'])})", color="#888888")
        for e in sections["nothing"]:
            other_block(e, nick, depth=1)


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
        mine_rows(sections.get("mine_view", sections["mine"]), nick, required)
        if sections["mine_drafts"]:
            mine_drafts_block(sections["mine_drafts"])
        if not sections["mine"] and not sections["mine_drafts"]:
            emit("No open PRs", color="#6b6b70")

        print("---")
        others_rows(sections, nick)

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


def _draft_states(data: dict) -> dict[str, dict] | None:
    """Each review candidate's automatic draft state (review/auto_draft.py states), or
    None while `[review] auto_draft` is off or the states can't be read, which leaves
    the review rows as they are without automatic drafts."""
    try:
        import time

        import settings
        from review import auto_draft
        cfg = settings.load()
        if not auto_draft.enabled(cfg):
            return None
        sts = auto_draft.candidates(data)
        return auto_draft.states(sts, auto_draft.all_jobs(), auto_draft.records_for(sts),
                                 time.time(), cfg["review"]["fallback_minutes"], menu=True)
    except Exception:  # noqa: BLE001 — the menu must render
        return None


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


def _settle_soon() -> None:
    """Have the watcher run a tick now (watcher/__main__.py `settle`), so a dismissed
    row's popup is withdrawn within seconds."""
    try:
        from watcher import __main__ as watcher_main
        watcher_main.settle_soon()
    except Exception:  # noqa: BLE001 — the menu must keep working without the watcher
        pass


def _followup_jobs() -> list[dict]:
    try:
        from watcher import followup
        return followup.all_jobs()
    except Exception:  # noqa: BLE001 — the menu must render even if the watcher can't load
        return []


def _approval_needs() -> dict[str, dict]:
    """PR url → what your approval waits on (watcher/approval.py `needs`), from the
    watcher's state file; {} when it can't be read."""
    try:
        prs = json.loads((REPO_DIR / "state" / "watcher" / "state.json").read_text())["prs"]
        return {n["url"].rstrip("/"): n for n in (v.get("approval") for v in prs.values()) if n}
    except Exception:  # noqa: BLE001 — the menu must render even if the state can't load
        return {}


def _autofix_jobs() -> list[dict]:
    try:
        from watcher import autofix
        return autofix.all_jobs()
    except Exception:  # noqa: BLE001 — the menu must render even if the watcher can't load
        return []


def _org() -> str:
    try:
        import settings
        return settings.load()["org"]
    except Exception:  # noqa: BLE001
        return "avetta"


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

    if "--dismiss-done" in args:
        rest = args[args.index("--dismiss-done") + 1:] + [""]
        dismiss_done(rest[0])
        trigger_refresh()
        return 0

    for flag in ("--dismiss", "--dismiss-mine"):
        if flag in args:
            i = args.index(flag)
            rest = args[i + 1:] + ["", "", ""]
            if rest[0]:
                dismiss(rest[0], rest[1], rest[2])
                _settle_soon()
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
                              required, nick, prio=_priority_settings(),
                              autofix_jobs=_autofix_jobs(),
                              drafts=_draft_states(cache) if have_data else None,
                              approval_needs=_approval_needs())
    sections["fyi"] = fyi_items()
    render_menu(sections, spinner=fetching, error=cache.get("error"),
                have_data=have_data, nick=nick, required=required)
    return 0


if __name__ == "__main__":
    sys.exit(main())
