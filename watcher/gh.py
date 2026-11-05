"""GitHub GraphQL for the watcher: the cheap search, the per-PR detail, normalization.

Auth reuses the repo's .env GITHUB_TOKEN (gh_prs.load_env / get_token). Requests run
one at a time. Each call returns the server's Date header too, so the watcher's
first-sight cutoff uses GitHub's clock rather than the laptop's.
"""

from __future__ import annotations

import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gh_prs  # noqa: E402

GRAPHQL_URL = f"{gh_prs.API_ROOT}/graphql"
DETAIL_BATCH = 5

SEARCH_QUERY = """
query($mine: String!, $others: String!, $mentions: String!) {
  viewer { login }
  rateLimit { cost remaining resetAt }
  mine: search(type: ISSUE, query: $mine, first: 50) {
    nodes { ... on PullRequest { id updatedAt headRefOid } } }
  others: search(type: ISSUE, query: $others, first: 50) {
    nodes { ... on PullRequest { id updatedAt headRefOid } } }
  mentions: search(type: ISSUE, query: $mentions, first: 50) {
    nodes { ... on PullRequest { id updatedAt headRefOid } } }
}
"""

COMMENT_FIELDS = """
  databaseId url createdAt body
  author { login __typename }
  commit { oid } originalCommit { oid }
  pullRequestReview { databaseId state }
"""

DETAIL_QUERY = """
query($ids: [ID!]!) {
  rateLimit { cost remaining }
  nodes(ids: $ids) { ... on PullRequest {
    id number title url state updatedAt headRefOid
    author { login }
    repository { name }
    latestOpinionatedReviews(first: 20, writersOnly: true) {
      nodes { state author { login __typename } } }
    reviews(last: 100) { nodes {
      databaseId state submittedAt body url author { login __typename } } }
    comments(last: 100) { nodes {
      databaseId createdAt body url author { login __typename } } }
    timelineItems(last: 30, itemTypes: [REVIEW_REQUESTED_EVENT]) { nodes {
      ... on ReviewRequestedEvent { id createdAt
        actor { login } requestedReviewer { ... on User { login } } } } }
    reviewThreads(first: 100) {
      pageInfo { hasNextPage endCursor }
      nodes { id isResolved isOutdated path line originalLine resolvedBy { login }
        comments(first: 50) { nodes { %s } } } }
  } }
}
""" % COMMENT_FIELDS

THREADS_PAGE_QUERY = """
query($id: ID!, $after: String) {
  node(id: $id) { ... on PullRequest {
    reviewThreads(first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { id isResolved isOutdated path line originalLine resolvedBy { login }
        comments(first: 50) { nodes { %s } } } } } }
}
""" % COMMENT_FIELDS

STATE_QUERY = """
query($ids: [ID!]!) { nodes(ids: $ids) { ... on PullRequest { id state } } }
"""


class GhError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def graphql(query: str, variables: dict, token: str) -> tuple[dict, str | None]:
    """POST one GraphQL query. Returns (data, server Date header)."""
    try:
        resp = requests.post(GRAPHQL_URL, json={"query": query, "variables": variables},
                             headers=gh_prs._auth_headers(token), timeout=gh_prs.TIMEOUT)
    except requests.RequestException as exc:
        raise GhError(f"network error: {exc}") from exc
    if resp.status_code != 200:
        raise GhError(f"HTTP {resp.status_code}: {resp.text[:200]}", resp.status_code)
    payload = resp.json()
    if payload.get("errors") and not payload.get("data"):
        raise GhError(f"GraphQL error: {payload['errors'][0].get('message', '')[:200]}")
    return payload.get("data") or {}, resp.headers.get("Date")


def token() -> str:
    gh_prs.load_env()
    return gh_prs.get_token()


# --- normalization -----------------------------------------------------------------


def _is_bot(author: dict | None, bots: set[str]) -> bool:
    if not author:
        return True
    login = author.get("login") or ""
    return author.get("__typename") == "Bot" or login.endswith("[bot]") or login in bots


def _comment(node: dict, bots: set[str]) -> dict:
    author = node.get("author")
    return {
        "id": node["databaseId"],
        "author": (author or {}).get("login", ""),
        "is_bot": _is_bot(author, bots),
        "created_at": node.get("createdAt"),
        "url": node.get("url"),
        "body": node.get("body") or "",
        "commit": (node.get("commit") or {}).get("oid"),
        "original_commit": (node.get("originalCommit") or {}).get("oid"),
        "review_id": (node.get("pullRequestReview") or {}).get("databaseId"),
        "review_state": (node.get("pullRequestReview") or {}).get("state"),
    }


def normalize_thread(node: dict, bots: set[str]) -> dict:
    return {
        "id": node["id"],
        "resolved": bool(node.get("isResolved")),
        "outdated": bool(node.get("isOutdated")),
        "resolved_by": (node.get("resolvedBy") or {}).get("login"),
        "path": node.get("path"),
        "line": node.get("line"),
        "original_line": node.get("originalLine"),
        "comments": [_comment(c, bots) for c in (node.get("comments") or {}).get("nodes") or []],
    }


def normalize_pr(node: dict, kind: str, bots: set[str], extra_threads: list[dict] = ()) -> dict:
    reviews = []
    for r in (node.get("reviews") or {}).get("nodes") or []:
        author = r.get("author")
        reviews.append({
            "id": r["databaseId"], "state": r["state"], "submitted_at": r.get("submittedAt"),
            "body": r.get("body") or "", "url": r.get("url"),
            "author": (author or {}).get("login", ""), "is_bot": _is_bot(author, bots),
        })
    opinions = [n for n in (node.get("latestOpinionatedReviews") or {}).get("nodes") or []
                if not _is_bot(n.get("author"), bots)]
    approved_by = [(n.get("author") or {}).get("login") for n in opinions
                   if n.get("state") == "APPROVED"]
    changes_requested_by = [(n.get("author") or {}).get("login") for n in opinions
                            if n.get("state") == "CHANGES_REQUESTED"]
    threads = [normalize_thread(t, bots)
               for t in ((node.get("reviewThreads") or {}).get("nodes") or []) + list(extra_threads)]
    return {
        "id": node["id"], "kind": kind,
        "repo": node["repository"]["name"], "number": node["number"],
        "title": node.get("title") or "", "url": node["url"], "state": node.get("state"),
        "author": (node.get("author") or {}).get("login", ""),
        "head": node.get("headRefOid"), "updated_at": node.get("updatedAt"),
        "approved_by": approved_by,
        "changes_requested_by": changes_requested_by,
        "reviews": reviews,
        "issue_comments": [_comment(c, bots) for c in (node.get("comments") or {}).get("nodes") or []],
        "threads": threads,
        # Direct review requests (team requests carry no login), oldest first.
        "review_requests": [
            {"id": n["id"], "at": n.get("createdAt"),
             "by": (n.get("actor") or {}).get("login", ""),
             "reviewer": (n.get("requestedReviewer") or {}).get("login", "")}
            for n in (node.get("timelineItems") or {}).get("nodes") or []
            if n and n.get("id") and (n.get("requestedReviewer") or {}).get("login")],
    }


# --- fetches -----------------------------------------------------------------------------


def search(me: str, org: str, tok: str) -> tuple[dict, str | None]:
    """{"viewer", "rate", "prs": {node_id: {kind, updated_at, head}}}, Date header."""
    data, date = graphql(SEARCH_QUERY, {
        "mine": f"is:pr is:open org:{org} author:{me}",
        "others": f"is:pr is:open org:{org} reviewed-by:{me} -author:{me}",
        # Others' PRs where someone @mentioned you, reviewed or not.
        "mentions": f"is:pr is:open org:{org} mentions:{me} -author:{me}",
    }, tok)
    prs = {}
    for group, kind in (("mine", "mine"), ("others", "others"), ("mentions", "others")):
        for n in (data.get(group) or {}).get("nodes") or []:
            if n and n.get("id") and n["id"] not in prs:
                prs[n["id"]] = {"kind": kind, "updated_at": n.get("updatedAt"), "head": n.get("headRefOid")}
    return {"viewer": (data.get("viewer") or {}).get("login"),
            "rate": data.get("rateLimit") or {}, "prs": prs}, date


def details(ids_kinds: dict[str, str], bots: set[str], tok: str) -> dict[str, dict]:
    """Normalized detail for each PR id, in batches of DETAIL_BATCH."""
    out = {}
    ids = list(ids_kinds)
    for i in range(0, len(ids), DETAIL_BATCH):
        data, _ = graphql(DETAIL_QUERY, {"ids": ids[i:i + DETAIL_BATCH]}, tok)
        for node in data.get("nodes") or []:
            if not node:
                continue
            extra = []
            page = (node.get("reviewThreads") or {}).get("pageInfo") or {}
            after = page.get("endCursor") if page.get("hasNextPage") else None
            while after:
                more, _ = graphql(THREADS_PAGE_QUERY, {"id": node["id"], "after": after}, tok)
                rt = ((more.get("node") or {}).get("reviewThreads")) or {}
                extra += rt.get("nodes") or []
                info = rt.get("pageInfo") or {}
                after = info.get("endCursor") if info.get("hasNextPage") else None
            out[node["id"]] = normalize_pr(node, ids_kinds[node["id"]], bots, extra)
    return out


def states(ids: list[str], tok: str) -> dict[str, str]:
    data, _ = graphql(STATE_QUERY, {"ids": ids}, tok)
    return {n["id"]: n.get("state") for n in data.get("nodes") or [] if n}
