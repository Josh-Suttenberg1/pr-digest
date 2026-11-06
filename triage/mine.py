"""Your own open PRs: what on them is waiting on you.

For each open PR you author it surfaces:

  1. Unresolved review threads whose last human comment is from someone other than
     you: a discussion sitting on your side of the court.
  2. Change-request reviews that are still the reviewer's latest review and that you
     haven't re-requested that reviewer after. Once you re-request them, the PR is in
     their queue, so the change request moves to `answered_change_requests` and no
     longer counts as waiting on you.
  3. Top-level PR comments from others newer than your most recent activity (comment
     or commit).

Bots never count: a comment from a GitHub App (`__typename` Bot), a login ending in
`[bot]`, or a login in the configured bot list is dropped before any of the above, so
a Copilot thread or a Snyk status comment never puts a PR on you.

Alongside what's waiting, it reports where review stands: `approvals` (each standing
approval with its place in line, so the second approver shows as 2 of 2, the commit it
was given on, and `on_head`, whether that commit is the PR's current head),
`requested` (reviewers asked for a review who haven't given one since) with
`requested_since` (when each was last asked), `head_oid` and `head_at` (the head
commit and its commit time), and for every comment to answer its `kind`
(triage/comment_kind.py) and a one-line `summary`.

A reviewer's standing decision is their latest APPROVED, CHANGES_REQUESTED or
DISMISSED review, as GitHub counts it: a later plain comment doesn't withdraw an
approval or a change request.

"Responded to" is judged structurally: a thread is handled once it is resolved or your
reply is its last human comment; a change request is handled once that reviewer's
standing decision is something else. Pushing code without replying leaves a thread
open, which is the honest default.

Read-only; uses `gh`, which must be authenticated.
"""

from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor

from triage import comment_kind

DETAIL_QUERY = """
query($owner:String!,$name:String!,$num:Int!){
  repository(owner:$owner,name:$name){
    pullRequest(number:$num){
      isDraft
      reviewDecision
      headRefOid
      commits(last:1){nodes{commit{oid committedDate author{user{login}}}}}
      reviewThreads(first:100){nodes{
        isResolved
        isOutdated
        comments(first:100){nodes{author{login __typename} createdAt path body url}}
      }}
      reviews(first:100){nodes{author{login __typename} state submittedAt body url commit{oid}}}
      comments(first:100){nodes{author{login __typename} createdAt body url}}
      timelineItems(last:100,itemTypes:[REVIEW_REQUESTED_EVENT]){nodes{
        ... on ReviewRequestedEvent{createdAt requestedReviewer{... on User{login}}}
      }}
      reviewRequests(first:30){nodes{requestedReviewer{
        ... on User{login} ... on Team{name}
      }}}
    }
  }
}
"""

WORKERS = 6


class GhError(RuntimeError):
    """A gh call failed; the message is gh's stderr."""


def _gh(args: list[str]) -> str:
    r = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise GhError(r.stderr.strip() or f"gh exited {r.returncode}")
    return r.stdout


def my_open_prs() -> list[dict]:
    return json.loads(_gh([
        "gh", "search", "prs", "--author=@me", "--state=open",
        "--json", "repository,number,title,url,updatedAt", "--limit", "100",
    ]))


def detail(owner: str, name: str, num: int) -> dict:
    out = _gh([
        "gh", "api", "graphql", "-f", f"query={DETAIL_QUERY}",
        "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"num={num}",
    ])
    return json.loads(out)["data"]["repository"]["pullRequest"]


def _login(node: dict) -> str:
    return ((node or {}).get("author") or {}).get("login") or "?"


def is_bot(node: dict, bots: set[str]) -> bool:
    author = (node or {}).get("author") or {}
    login = (author.get("login") or "").lower()
    return (author.get("__typename") == "Bot" or login.endswith("[bot]")
            or login in bots)


def analyze(pr: dict, me: str, bots: set[str] | frozenset = frozenset()) -> dict:
    """What's waiting on you for one PR (a `detail()` result)."""
    bots = {b.lower() for b in bots}
    threads_waiting = []
    for t in pr["reviewThreads"]["nodes"]:
        if t["isResolved"]:
            continue
        comments = [c for c in t["comments"]["nodes"] if not is_bot(c, bots)]
        if not comments:
            continue
        # A thread with no comment from anyone but you is your own note -> skip.
        if all(_login(c) == me for c in comments):
            continue
        last = comments[-1]
        if _login(last) != me:
            # The comment to answer: the thread's opener when that's someone else,
            # otherwise the reply that put it back on you.
            ask = comments[0] if _login(comments[0]) != me else last
            threads_waiting.append({
                "path": comments[0].get("path"),
                "last_author": _login(last),
                "last_at": last["createdAt"],
                "url": last.get("url", ""),
                "outdated": t["isOutdated"],
                "bodies": [{"author": _login(c), "body": c["body"]} for c in comments],
                "author": _login(ask),
                "kind": comment_kind.kind(ask.get("body") or ""),
                "summary": comment_kind.summary(ask.get("body") or ""),
            })

    changes_requested = pr["reviewDecision"] == "CHANGES_REQUESTED"
    # Each reviewer's standing decision: their latest opinionated review.
    latest_by_author: dict[str, dict] = {}
    for rv in pr["reviews"]["nodes"]:
        if is_bot(rv, bots) or _login(rv) == me:
            continue
        if rv.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest_by_author[_login(rv)] = rv  # reviews come in chronological order
    head_oid = pr.get("headRefOid") or ""
    approvals = [
        {"author": a, "at": rv.get("submittedAt") or "", "url": rv.get("url", ""),
         "commit": (rv.get("commit") or {}).get("oid") or "",
         # Unknown when either oid is missing; that counts as on the head, so a
         # snapshot without oids reads the way it did before they were fetched.
         "on_head": _on_head((rv.get("commit") or {}).get("oid") or "", head_oid)}
        for a, rv in latest_by_author.items() if rv["state"] == "APPROVED"
    ]
    approvals.sort(key=lambda x: x["at"])
    for i, appr in enumerate(approvals, 1):
        appr["ordinal"] = i
    # When you last (re-)requested each reviewer.
    rerequested: dict[str, str] = {}
    for ev in ((pr.get("timelineItems") or {}).get("nodes") or []):
        login = ((ev or {}).get("requestedReviewer") or {}).get("login")
        if login:
            rerequested[login] = max(rerequested.get(login, ""), ev.get("createdAt") or "")
    change_requests = sorted(
        (
            {"author": a, "body": rv.get("body") or "", "at": rv["submittedAt"],
             "url": rv.get("url", ""),
             "rerequested_at": rerequested.get(a, "") if rerequested.get(a, "") > rv["submittedAt"] else ""}
            for a, rv in latest_by_author.items()
            if a != me and rv["state"] == "CHANGES_REQUESTED"
        ),
        key=lambda b: b["author"],
    )
    # A change request is on you until you re-request that reviewer after it: the
    # re-request is what puts the PR back in their queue.
    blockers = [b for b in change_requests if not b["rerequested_at"]]
    answered = [b for b in change_requests if b["rerequested_at"]]

    # Your most recent activity timestamp (last comment by you, or last commit by you).
    my_times = [c["createdAt"] for c in pr["comments"]["nodes"] if _login(c) == me]
    for c in pr["commits"]["nodes"]:
        commit = c["commit"]
        u = (commit.get("author") or {}).get("user")
        if u and u.get("login") == me:
            my_times.append(commit["committedDate"])
    last_mine = max(my_times) if my_times else ""

    unanswered_comments = [
        {"author": _login(c), "at": c["createdAt"], "body": c.get("body", ""),
         "url": c.get("url", ""), "kind": comment_kind.kind(c.get("body") or ""),
         "summary": comment_kind.summary(c.get("body") or "")}
        for c in pr["comments"]["nodes"]
        if c.get("author") and not is_bot(c, bots)
        and _login(c) != me and c["createdAt"] > last_mine
    ]

    # Reviewers with a pending request. GitHub drops a reviewer from this list when
    # they submit a review, so anyone here owes one (again, if they decided before).
    requested = []
    for n in ((pr.get("reviewRequests") or {}).get("nodes") or []):
        who = (n or {}).get("requestedReviewer") or {}
        name = who.get("login") or (f"team:{who['name']}" if who.get("name") else "")
        if name and name.lower() not in bots:
            requested.append(name)

    head_at = ""
    for c in pr["commits"]["nodes"]:
        head_at = max(head_at, (c.get("commit") or {}).get("committedDate") or "")

    return {
        "threads_waiting": threads_waiting,
        "changes_requested": changes_requested,
        "blockers": blockers,
        "answered_change_requests": answered,
        "unanswered_comments": unanswered_comments,
        "approvals": approvals,
        "requested": requested,
        # When each pending reviewer was last asked, for "waiting since".
        "requested_since": {r: rerequested[r] for r in requested if rerequested.get(r)},
        "head_oid": head_oid,
        "head_at": head_at,
    }


def _on_head(commit: str, head: str) -> bool:
    return not commit or not head or commit == head


def build_row(p: dict, d: dict, me: str, bots: set[str] | frozenset = frozenset()) -> dict:
    """One PR's row from its search hit `p` (repository.name, number, title, url,
    updatedAt) and its `detail()`, with `waiting` set when anything in `analyze` is
    non-empty."""
    a = analyze(d, me, bots)
    return {
        "repo": p["repository"]["name"],
        "number": p["number"],
        "title": p["title"],
        "url": p["url"],
        "isDraft": d["isDraft"],
        "updated_at": p.get("updatedAt") or "",
        "waiting": bool(a["threads_waiting"] or a["blockers"]
                        or a["unanswered_comments"]),
        **a,
    }


def row_for(owner: str, name: str, num: int, title: str, url: str, me: str,
            bots: set[str] | frozenset = frozenset()) -> dict:
    """The row for one PR, fetched on its own (the watcher's notifications use it)."""
    hit = {"repository": {"name": name}, "number": num, "title": title, "url": url}
    return build_row(hit, detail(owner, name, num), me, bots)


def mine_triage(me: str, bots: set[str] | frozenset = frozenset()) -> list[dict]:
    """One row per open PR you author, in search order (see `build_row`)."""
    prs = my_open_prs()

    def row(p: dict) -> dict:
        owner, name = p["repository"]["nameWithOwner"].split("/")
        return build_row(p, detail(owner, name, p["number"]), me, bots)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return list(pool.map(row, prs))
