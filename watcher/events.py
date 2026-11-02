"""Decide what changed since the last tick. Pure: no network, no clock, no files.

diff_pr(pr, prev, ctx) compares one PR's snapshot (see watcher/gh.py normalize_pr)
with that PR's saved state and returns (events, new_state).

Seen tracking is by id, never by timestamp: a comment inside a review that sits
pending for an hour carries a createdAt from when it was written, but only shows
up when the review is submitted, so a time watermark would skip it.

Event ids: rv:<review id>, rc:<review-comment id>, ic:<issue-comment id>.

Josh's own PRs ("mine"): every new human item is grouped by actor. An actor whose
new items include an APPROVED or CHANGES_REQUESTED review becomes one "decision"
event, with that review's inline comments folded in; anyone else becomes one
"comments" event.

Others' PRs: only review threads containing one of Josh's comments matter. A thread
is armed on Josh's latest comment in it and fires once, on whichever comes first:
  reply     a human other than Josh comments after it
  outdated  GitHub marks the thread outdated (a later commit changed its lines)
            when it wasn't outdated at arm time
  moved     the thread was already outdated when Josh commented, the head has moved
            since, and the commits since touch the thread's file (ctx.paths_changed)
A thread Josh resolves himself disarms silently; one resolved by someone else after
the head moved counts as "outdated". It re-arms only when Josh comments again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

DECISIONS = ("APPROVED", "CHANGES_REQUESTED")


@dataclass
class Ctx:
    me: str
    # Silent mode: record everything as seen and fire nothing (the first run).
    seeding: bool = False
    # For a PR seen for the first time after seeding: only items created at or after
    # this ISO time notify. None means "everything new notifies".
    cutoff: str | None = None
    # (repo, base_sha, head_sha) -> set of changed paths, or None when unknown.
    paths_changed: Callable[[str, str, str], set[str] | None] = field(
        default=lambda repo, base, head: None
    )


def _human(item: dict, me: str) -> bool:
    return not item["is_bot"] and item["author"] not in ("", me)


def _first_line(text: str, limit: int = 140) -> str:
    for line in (text or "").splitlines():
        line = line.strip().lstrip(">#*- ").strip()
        if line and not line.startswith("```"):
            return line[:limit]
    return ""


def _after_cutoff(created: str | None, cutoff: str | None) -> bool:
    return cutoff is None or (created or "") >= cutoff


# --- Josh's own PRs -------------------------------------------------------------


def _mine_items(pr: dict, me: str) -> dict[str, dict]:
    """Every notifiable item on the PR, keyed by event id."""
    items: dict[str, dict] = {}
    for r in pr["reviews"]:
        if r["state"] == "PENDING":
            continue
        items[f"rv:{r['id']}"] = {**r, "kind": "review", "created": r["submitted_at"]}
    for t in pr["threads"]:
        for c in t["comments"]:
            items[f"rc:{c['id']}"] = {**c, "kind": "review_comment", "created": c["created_at"]}
    for c in pr["issue_comments"]:
        items[f"ic:{c['id']}"] = {**c, "kind": "issue_comment", "created": c["created_at"]}
    return items


def _diff_mine(pr: dict, prev: dict | None, ctx: Ctx) -> tuple[list[dict], dict]:
    items = _mine_items(pr, ctx.me)
    seen = set((prev or {}).get("seen", []))
    first_sight = prev is None
    new = {
        k: v for k, v in items.items()
        if k not in seen and _human(v, ctx.me)
        and not ctx.seeding
        and (not first_sight or _after_cutoff(v["created"], ctx.cutoff))
    }
    state = {"kind": "mine", "seen": sorted(items)}
    if not new:
        return [], state

    by_actor: dict[str, list[dict]] = {}
    for item in sorted(new.values(), key=lambda i: i["created"] or ""):
        by_actor.setdefault(item["author"], []).append(item)

    approvals = len(pr["approved_by"])
    events = []
    for actor, its in by_actor.items():
        decision = next((i for i in reversed(its)
                         if i["kind"] == "review" and i["state"] in DECISIONS), None)
        comments = [i for i in its if i["kind"] != "review" or (i.get("body") or "").strip()]
        first = next((i for i in comments if (i.get("body") or "").strip()), None)
        base = {
            "pr": _pr_ref(pr), "actor": actor,
            "count": len(comments),
            "first_line": _first_line(first["body"]) if first else "",
            "url": (first or its[0]).get("url") or pr["url"],
        }
        if decision:
            # The decision's own body is the headline, not one of "its N comments".
            others = [i for i in comments if i is not decision]
            line = _first_line(decision.get("body") or "") or base["first_line"]
            events.append({**base, "type": "decision", "state": decision["state"],
                           "approvals": approvals, "count": len(others), "first_line": line,
                           "url": decision.get("url") or base["url"]})
        elif comments:
            events.append({**base, "type": "comments"})
    return events, state


# --- threads on others' PRs ------------------------------------------------------


def _my_last_index(thread: dict, me: str) -> int | None:
    idx = None
    for i, c in enumerate(thread["comments"]):
        if c["author"] == me:
            idx = i
    return idx


def _arm(thread: dict, i: int, pr: dict) -> dict:
    c = thread["comments"][i]
    return {
        "arm_id": c["id"],
        "arm_commit": c.get("original_commit") or c.get("commit") or "",
        "arm_head": pr["head"],
        "outdated_at_arm": thread["outdated"],
        "fired": None,
    }


def _thread_event(pr: dict, thread: dict, armed: dict, trigger: str,
                  actor: str, url: str, line: str, me: str) -> dict:
    root = thread["comments"][0]
    mine = next(c for c in thread["comments"] if c["id"] == armed["arm_id"])
    return {
        "type": "thread", "trigger": trigger, "pr": _pr_ref(pr), "actor": actor,
        "thread_id": thread["id"], "root_id": root["id"], "arm_id": armed["arm_id"],
        "arm_commit": armed["arm_commit"], "head": pr["head"],
        "path": thread["path"], "line": thread.get("line") or thread.get("original_line"),
        "my_comment": mine["body"], "my_comment_url": mine.get("url"),
        "thread": [{"author": c["author"], "body": c["body"]} for c in thread["comments"]],
        "url": url, "first_line": line,
    }


def _diff_threads(pr: dict, prev_threads: dict, first_sight: bool, ctx: Ctx
                  ) -> tuple[list[dict], dict]:
    events, out = [], {}
    for thread in pr["threads"]:
        mine_at = _my_last_index(thread, ctx.me)
        if mine_at is None:
            continue
        armed = prev_threads.get(thread["id"])
        rearm = armed is None or armed["arm_id"] != thread["comments"][mine_at]["id"]
        if rearm:
            armed = _arm(thread, mine_at, pr)
        out[thread["id"]] = armed
        if armed["fired"]:
            continue

        later = [c for c in thread["comments"][mine_at + 1:] if _human(c, ctx.me)]
        trigger = actor = url = line = None
        if later:
            c = later[0]
            if ctx.seeding or (first_sight and not _after_cutoff(c["created_at"], ctx.cutoff)):
                armed["fired"] = {"trigger": "reply", "silent": True}
                continue
            trigger, actor, url, line = "reply", c["author"], c.get("url"), _first_line(c["body"])
        elif thread["resolved"]:
            head_moved = pr["head"] != armed["arm_head"]
            if thread.get("resolved_by") == ctx.me or not head_moved or ctx.seeding:
                armed["fired"] = {"trigger": "resolved", "silent": True}
                continue
            trigger = "outdated"
        elif thread["outdated"] and not armed["outdated_at_arm"]:
            if ctx.seeding or first_sight:
                armed["fired"] = {"trigger": "outdated", "silent": True}
                continue
            trigger = "outdated"
        elif (armed["outdated_at_arm"] and pr["head"] != armed["arm_head"]
              and armed["arm_commit"] and not ctx.seeding and not first_sight):
            changed = ctx.paths_changed(pr["repo"], armed["arm_commit"], pr["head"])
            if changed and thread["path"] in changed:
                trigger = "moved"
        if not trigger:
            continue
        if trigger in ("outdated", "moved"):
            actor = pr["author"]
            url = thread["comments"][mine_at].get("url") or pr["url"]
            line = f"{thread['path']}" + (f":{thread.get('line')}" if thread.get("line") else "")
        ev = _thread_event(pr, thread, armed, trigger, actor, url, line, ctx.me)
        armed["fired"] = {"trigger": trigger, "silent": False}
        events.append(ev)
    return events, out


# --- entry point -----------------------------------------------------------------


def _pr_ref(pr: dict) -> dict:
    return {k: pr[k] for k in ("id", "repo", "number", "title", "url", "author", "head")}


def diff_pr(pr: dict, prev: dict | None, ctx: Ctx) -> tuple[list[dict], dict]:
    """Events for one PR and the PR's new saved state."""
    first_sight = prev is None
    if pr["kind"] == "mine":
        events, state = _diff_mine(pr, prev, ctx)
        state["threads"] = {}
    else:
        events, threads = _diff_threads(pr, (prev or {}).get("threads", {}), first_sight, ctx)
        state = {"kind": "others", "seen": [], "threads": threads}
    state.update({"repo": pr["repo"], "number": pr["number"], "updated_at": pr["updated_at"],
                  "head": pr["head"], "missing_ticks": 0})
    return events, state
