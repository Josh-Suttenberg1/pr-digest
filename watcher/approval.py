"""Ready to approve: when Claude's follow-ups find that the author did everything your
review asked, your approval of their PR is one click in the menu.

Two pure halves:

- `needs(snap, me)` reads a fresh watcher snapshot (watcher/gh.py normalize_pr) and
  says what your approval waits on: your standing change request, and each unresolved
  thread holding a comment of yours. The watcher saves it on the PR's state each time it
  fetches the PR (watcher/__main__.py tick), so the menu can read it without GitHub.
- `status(needs, jobs)` holds that against the PR's follow-up jobs (watcher/followup.py)
  and says whether everything is addressed. The menu and the popup call it with the jobs
  as they are now, so a follow-up that finishes between ticks counts at once.

What has to be addressed: your standing change request, judged on the PR's current
head (its follow-up covers every commit since it), and each blocking thread of yours,
judged on your latest comment there. Comments that aren't blocking don't hold an
approval back; when there's nothing blocking at all, every thread follow-up there is
must have found its comment addressed, and there must be at least one.

The click runs `watcher approve <PR id> <head>` (followup.approve), which re-reads the
PR and posts only if it's still ready on the head the menu showed. The posted text is
exactly `body()`.
"""

from __future__ import annotations

from pathlib import Path

FOOTER = "Drafted with Claude 🤖"
DONE = ("ready", "posted", "reapproved")
PENDING = ("queued", "running")


def needs(snap: dict, me: str) -> dict | None:
    """What your approval of someone else's open PR waits on. None when there's nothing
    to approve: the PR is yours or not open, you've neither decided on it nor left an
    unresolved comment, or your latest decision is an approval of the current head.

    {"pr_id", "url", "repo", "number", "head", "verb": "approve" | "reapprove",
     "change_request": your standing change request's review id, or None,
     "threads": [{"id", "arm_id", "blocking", "where"}]}, one per unresolved thread
    with a comment of yours, `arm_id` being your latest comment there."""
    from triage import comment_kind, priority

    if snap.get("kind") == "mine" or snap.get("state") != "OPEN" or snap.get("author") == me:
        return None
    decisions = [r for r in sorted(snap["reviews"], key=lambda r: r.get("submitted_at") or "")
                 if r["author"] == me and r["state"] in ("APPROVED", "CHANGES_REQUESTED",
                                                         "DISMISSED")]
    latest = decisions[-1] if decisions else None
    if latest and latest["state"] == "APPROVED" and latest.get("commit") == snap["head"]:
        return None
    threads = []
    for t in snap["threads"]:
        mine = [c for c in t["comments"] if c["author"] == me]
        if t["resolved"] or not mine:
            continue
        where = Path(t.get("path") or "").name + (f":{t['line']}" if t.get("line") else "")
        threads.append({
            "id": t["id"], "arm_id": mine[-1]["id"], "where": where or "a thread",
            "blocking": any(priority.is_blocking(comment_kind.kind(c.get("body") or ""),
                                                 c.get("review_state")) for c in mine)})
    cr = latest["id"] if latest and latest["state"] == "CHANGES_REQUESTED" else None
    if not cr and not threads:
        return None
    return {"pr_id": snap["id"], "url": snap["url"], "repo": snap["repo"],
            "number": snap["number"], "head": snap["head"],
            "verb": "reapprove" if latest and latest["state"] == "APPROVED" else "approve",
            "change_request": cr, "threads": threads}


WORDS = {"partly": "partly addressed", "not_addressed": "not addressed"}


def _verdict(job: dict) -> str:
    return job.get("verdict") or ""


def _words(job: dict) -> str:
    v = _verdict(job)
    return WORDS.get(v, v.replace("_", " ") or "unclear")


def status(needs_: dict | None, jobs: list[dict]) -> dict | None:
    """Whether your approval is one click away. Pure. None when `needs_` is None, or when
    no follow-up has answered anything it asks for yet (there's nothing to say).
    Otherwise {"ready", "verb", "head", "pr_id", "repo", "why", "covered", "label", "what"}:
    `why` names what holds it back ('' when ready); `covered` is {"change_request": bool,
    "blocking": n, "comments": n}, what the follow-ups found addressed, which `what`
    says in words; `label` is "Ready to approve" or "Ready to re-approve"."""
    if not needs_:
        return None
    pr_jobs = [j for j in jobs if (j.get("pr") or {}).get("id") == needs_["pr_id"]
               and j.get("trigger") not in ("mention", "answer")]
    if not pr_jobs:
        return None
    why: list[str] = []
    covered = {"change_request": False, "blocking": 0, "comments": 0}

    if any(j.get("status") in PENDING for j in pr_jobs):
        why.append("Claude is still checking the author's changes")

    if needs_["change_request"]:
        crs = [j for j in pr_jobs if j.get("trigger") == "change_request"
               and j.get("review_id") == needs_["change_request"] and j.get("status") in DONE]
        cr = max(crs, key=lambda j: j.get("created_at") or "", default=None)
        if cr is None:
            why.append("the author hasn't answered your change request yet")
        elif cr.get("head") != needs_["head"]:
            why.append("the author pushed after Claude checked your change request")
        elif _verdict(cr) != "addressed":
            why.append(f"Claude found your change request {_words(cr)}")
        else:
            covered["change_request"] = True

    def thread_job(t: dict) -> dict | None:
        found = [j for j in pr_jobs if j.get("thread_id") == t["id"]
                 and j.get("arm_id") == t["arm_id"] and j.get("status") in DONE]
        return max(found, key=lambda j: j.get("created_at") or "", default=None)

    blocking = [t for t in needs_["threads"] if t["blocking"]]
    gate = blocking if (blocking or needs_["change_request"]) else [
        t for t in needs_["threads"] if thread_job(t)]
    for t in gate:
        j = thread_job(t)
        if j is None:
            why.append(f"your comment on {t['where']} has no answer yet")
        elif _verdict(j) != "addressed":
            why.append(f"Claude found your comment on {t['where']} {_words(j)}")
        elif t["blocking"]:
            covered["blocking"] += 1
        else:
            covered["comments"] += 1
    if not (covered["change_request"] or covered["blocking"] or covered["comments"]) and not why:
        return None
    return {"ready": not why, "verb": needs_["verb"], "head": needs_["head"],
            "pr_id": needs_["pr_id"], "repo": needs_["repo"], "why": "; ".join(why),
            "covered": covered,
            "label": label(needs_["verb"]), "what": what(covered)}


def what(covered: dict) -> str:
    """'your blocking comment and change request', 'your 2 blocking comments',
    'your change request', 'your comments'."""
    n = covered["blocking"]
    parts = []
    if n:
        parts.append("blocking comment" if n == 1 else f"{n} blocking comments")
    if covered["change_request"]:
        parts.append("change request")
    if not parts:
        return "your comment" if covered["comments"] == 1 else "your comments"
    return "your " + " and ".join(parts)


def label(verb: str) -> str:
    """'Ready to approve' or 'Ready to re-approve'."""
    return "Ready to re-approve" if verb == "reapprove" else "Ready to approve"


def body(repo: str, head: str, verb: str, org: str = "avetta") -> str:
    """The approval's exact text: 'Approving, my review comments are addressed as of
    <linked short sha>.' plus the footer."""
    link = f"[{head[:7]}](https://github.com/{org}/{repo}/commit/{head})"
    lead = "Reapproving" if verb == "reapprove" else "Approving"
    return f"{lead}, my review comments are addressed as of {link}.\n\n{FOOTER}"
