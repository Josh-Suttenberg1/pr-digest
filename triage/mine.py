"""Your own open PRs: what on them is waiting on you.

For each open PR you author it surfaces:

  1. Unresolved review threads whose last human comment is from someone other than
     you: a discussion sitting on your side of the court.
  2. Change-request reviews that are still the reviewer's latest review.
  3. Top-level PR comments from others newer than your most recent activity (comment
     or commit).

Bots never count: a comment from a GitHub App (`__typename` Bot), a login ending in
`[bot]`, or a login in the configured bot list is dropped before any of the above, so
a Copilot thread or a Snyk status comment never puts a PR on you.

"Responded to" is judged structurally: a thread is handled once it is resolved or your
reply is its last human comment; a change request is handled once that reviewer's
latest review is something else. Pushing code without replying leaves a thread open,
which is the honest default.

Read-only; uses `gh`, which must be authenticated.
"""

from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor

DETAIL_QUERY = """
query($owner:String!,$name:String!,$num:Int!){
  repository(owner:$owner,name:$name){
    pullRequest(number:$num){
      isDraft
      reviewDecision
      commits(last:1){nodes{commit{committedDate author{user{login}}}}}
      reviewThreads(first:100){nodes{
        isResolved
        isOutdated
        comments(first:100){nodes{author{login __typename} createdAt path body url}}
      }}
      reviews(first:100){nodes{author{login __typename} state submittedAt body url}}
      comments(first:100){nodes{author{login __typename} createdAt body url}}
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
            threads_waiting.append({
                "path": comments[0].get("path"),
                "last_author": _login(last),
                "last_at": last["createdAt"],
                "url": last.get("url", ""),
                "outdated": t["isOutdated"],
                "bodies": [{"author": _login(c), "body": c["body"]} for c in comments],
            })

    changes_requested = pr["reviewDecision"] == "CHANGES_REQUESTED"
    # Who currently requests changes (latest review per author == CHANGES_REQUESTED).
    latest_by_author: dict[str, dict] = {}
    for rv in pr["reviews"]["nodes"]:
        if is_bot(rv, bots):
            continue
        latest_by_author[_login(rv)] = rv  # reviews come in chronological order
    blockers = sorted(
        (
            {"author": a, "body": rv.get("body") or "", "at": rv["submittedAt"],
             "url": rv.get("url", "")}
            for a, rv in latest_by_author.items()
            if a != me and rv["state"] == "CHANGES_REQUESTED"
        ),
        key=lambda b: b["author"],
    )

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
         "url": c.get("url", "")}
        for c in pr["comments"]["nodes"]
        if c.get("author") and not is_bot(c, bots)
        and _login(c) != me and c["createdAt"] > last_mine
    ]

    return {
        "threads_waiting": threads_waiting,
        "changes_requested": changes_requested,
        "blockers": blockers,
        "unanswered_comments": unanswered_comments,
    }


def mine_triage(me: str, bots: set[str] | frozenset = frozenset()) -> list[dict]:
    """One row per open PR you author, in search order, each with `waiting` set when
    anything in `analyze` is non-empty."""
    prs = my_open_prs()

    def row(p: dict) -> dict:
        owner, name = p["repository"]["nameWithOwner"].split("/")
        d = detail(owner, name, p["number"])
        a = analyze(d, me, bots)
        return {
            "repo": p["repository"]["name"],
            "number": p["number"],
            "title": p["title"],
            "url": p["url"],
            "isDraft": d["isDraft"],
            "waiting": bool(a["threads_waiting"] or a["blockers"]
                            or a["unanswered_comments"]),
            **a,
        }

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return list(pool.map(row, prs))
