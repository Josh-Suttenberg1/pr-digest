"""Every PR popup still on screen or in Notification Center, so it can be taken back
once the thing it asked for is done.

A popup goes out through `send`, which records it in state/watcher/popups.json under
its notification group:

  group -> {kind, pr: {id, url, repo, number}, color, sent_at, since, job}

`kind` says what would resolve it (`resolution`):

  pr        a PR row's action (watcher/__main__.py deliver): answer the comments on
            your PR, or respond to a follow-up or @mention on someone else's. Resolved
            once the PR has no color left (triage/priority.py), or you dismissed its
            menu row. A stale wait (🟡, a cue to nudge) is a color, so its popup stays
            until the review arrives.
  review    a review you owe (review/auto_draft.py, review/review_pr.py --notify).
            Resolved once you review it after `since`, the moment the request was
            first seen, or are removed as a reviewer (`review_outcome`).
  autofix   a fix that escalated (watcher/autofix.py). Resolved once the job is no
            longer escalated: the thread was answered or resolved, or the job dismissed.
  fyi       something to know, nothing to do (watcher/fyi.py). Withdrawn by the menu
            once you've seen its row, never by the watcher.

Any kind but fyi is resolved once its PR merges or closes. Clicking a popup or a menu link
resolves nothing by itself.

The watcher re-checks every PR with a popup out on every tick (`watched_ids`,
`review_entries`), so a popup is withdrawn within one tick of the work being done,
whether it was done from the menu, a Claude session or github.com. A menu action
(Approve, Post, Dismiss) starts a tick at once (watcher/__main__.py `settle`), so its
popup goes within seconds of the click.

Several processes send popups (the watcher's tick, a draft run, an auto-fix run, a
manual review), so every read-modify-write holds an flock on popups.lock.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import notify  # noqa: E402
import settings  # noqa: E402

PATH = settings.STATE_DIR / "watcher" / "popups.json"
PR, REVIEW, AUTOFIX, FYI = "pr", "review", "autofix", "fyi"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(path: Path | None = None) -> dict[str, dict]:
    try:
        data = json.loads((path or PATH).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def group_for(kind: str, repo: str = "", number: int | str = "", job: str = "") -> str:
    """The notification group: one per PR for a PR row, one per PR for a review owed,
    one per job for an auto-fix, so resolving one never withdraws another."""
    if kind == AUTOFIX:
        return f"autofix-{job}"
    if kind == FYI:
        return f"fyi-{job}"
    return f"{'pr' if kind == PR else 'review'}-{repo}-{number}"


def record(group: str, entry: dict, path: Path | None = None) -> None:
    path = path or PATH
    with _locked(path):
        data = load(path)
        data[group] = {**entry, "sent_at": entry.get("sent_at") or _now()}
        _save(data, path)


def forget(groups, path: Path | None = None) -> None:
    path = path or PATH
    groups = set(groups)
    if not groups:
        return
    with _locked(path):
        data = load(path)
        for g in groups:
            data.pop(g, None)
        _save(data, path)


def send(kind: str, pr: dict, color: str | None, title: str, subtitle: str, body: str,
         cfg: dict, href: str | None = None, since: str | None = None, job: str | None = None,
         team: bool = False, run=subprocess.run, path: Path | None = None) -> dict:
    """Send a PR popup in its color's style (notify.popup) and record it. `pr` needs
    url, repo and number, and id for a PR row. `team` marks a review popup for a
    teammate's PR that doesn't request you (`review_outcome`). Returns the record,
    with `style`."""
    group = group_for(kind, pr.get("repo", ""), pr.get("number", ""), job or "")
    style = notify.popup(color, title, subtitle, body, cfg, href=href, group=group, run=run)
    entry = {"kind": kind, "color": color, "since": since, "job": job, "title": title,
             "pr": {k: pr.get(k) for k in ("id", "url", "repo", "number")}}
    if team:
        entry["team"] = True
    record(group, entry, path)
    return {**entry, "group": group, "style": style}


def withdraw(resolved: dict[str, str], run=subprocess.run, path: Path | None = None,
             log=None) -> None:
    """Withdraw each group's popup from Notification Center and forget it. `resolved`
    maps group -> why, which is logged."""
    entries = load(path)
    for group, why in resolved.items():
        notify.withdraw(group, run=run)
        if log:
            title = (entries.get(group) or {}).get("title") or group
            log(f"{_now()} withdrew popup: {title} ({why})")
    forget(resolved, path)


# --- what to re-check, and what's resolved (pure) ----------------------------------------


def watched_ids(entries: dict[str, dict]) -> set[str]:
    """PR node ids with a PR-row or auto-fix popup out: the watcher fetches each in
    detail on every tick, which is what clears their rows and jobs."""
    return {e["pr"]["id"] for e in entries.values()
            if e.get("kind") in (PR, AUTOFIX) and (e.get("pr") or {}).get("id")}


def review_entries(entries: dict[str, dict]) -> dict[str, dict]:
    return {g: e for g, e in entries.items() if e.get("kind") == REVIEW}


def resolved_rows(entries: dict[str, dict], checked: set[str], still_colored: set[str]) -> dict[str, str]:
    """PR-row popups to withdraw: their PR was classified this tick (`checked`, node
    ids) and has no color left (not in `still_colored`, the watcher's `notified`)."""
    return {g: "nothing left to do on it" for g, e in entries.items()
            if e.get("kind") == PR and e["pr"].get("id") in checked
            and e["pr"]["id"] not in still_colored}


def resolved_closed(entries: dict[str, dict], closed: dict[str, str]) -> dict[str, str]:
    """Popups of any kind but fyi whose PR merged or closed. `closed` maps a PR's node id or
    url to its state (MERGED or CLOSED)."""
    out = {}
    for g, e in entries.items():
        if e.get("kind") == FYI:
            continue
        pr = e.get("pr") or {}
        state = closed.get(pr.get("id") or "") or closed.get((pr.get("url") or "").rstrip("/"))
        if state:
            out[g] = f"the PR {state.lower()}"
    return out


def review_outcome(live: dict | None, since: str, team: bool = False) -> tuple[str, str] | None:
    """What became of a review you owed, from `live` (watcher/gh.py review_states):
    ("closed", "the PR merged"), ("reviewed", "you reviewed it"), ("removed", "you
    were removed as a reviewer"), or None while it's still owed or couldn't be read.

    A review after `since` counts when it approves, requests changes, or comments with
    a body, as in followup.cleared_jobs: a bare reply in someone's thread is recorded
    as an empty COMMENTED review and doesn't count, and a dismissed review is owed
    again. A review of inline comments alone also counts once GitHub has dropped your
    review request, which it does on a real review and never on a thread reply.

    `team`: the review is owed because a teammate opened the PR (triage/priority.py
    team_pr), not because GitHub requests you, so an absent request means nothing and
    only a review or the PR closing settles it here."""
    if not live:
        return None
    if live.get("state") in ("MERGED", "CLOSED"):
        return "closed", f"the PR {live['state'].lower()}"
    mine = [r for r in live.get("reviews") or [] if (r.get("submitted_at") or "") >= since]
    if any(r.get("state") in ("APPROVED", "CHANGES_REQUESTED")
           or (r.get("state") == "COMMENTED" and (r.get("body") or "").strip()) for r in mine):
        return "reviewed", "you reviewed it"
    if live.get("requested") is False and not team:
        if any(r.get("state") == "COMMENTED" for r in mine):
            return "reviewed", "you reviewed it"
        return "removed", "you were removed as a reviewer"
    return None


def review_resolution(entry: dict, live: dict | None, me: str,
                      owed: set[str] | None = None) -> str | None:
    """Why a review-owed popup is resolved (`review_outcome` from when the request was
    first seen, else from when the popup went out), or None. A teammate's PR (`team`
    on the entry) is also resolved once it's out of `owed`, the PR urls the menu last
    listed as reviews you owe: it got its approvals, a change request now waits on the
    author, or it went back to draft."""
    team = bool(entry.get("team"))
    out = review_outcome(live, entry.get("since") or entry.get("sent_at") or "", team)
    if out:
        return out[1]
    url = ((entry.get("pr") or {}).get("url") or "").rstrip("/")
    if team and owed is not None and url and url not in owed:
        return "it no longer waits on your review"
    return None


def autofix_resolution(entry: dict, job: dict | None) -> str | None:
    """Why an escalated auto-fix's popup is resolved, or None."""
    if job is None:
        return "the auto-fix is gone"
    if job.get("status") != "escalated":
        return f"the auto-fix is {job.get('status')}"
    return None
