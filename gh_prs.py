"""Shared GitHub PR fetching logic.

Used by both the CLI (pr_digest.py) and the SwiftBar menu bar plugin
(swiftbar_plugin.py). Holds auth, the search-API calls, error handling, and
result normalization in one place so the two front-ends stay in sync.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv

API_ROOT = "https://api.github.com"
SEARCH_URL = f"{API_ROOT}/search/issues"
TIMEOUT = 30

# This file lives in the repo root; .env sits next to it.
REPO_DIR = Path(__file__).resolve().parent
import settings  # noqa: E402

ENV_PATH = settings.DATA_DIR / ".env"

# The two searches that define "needs your attention".
QUERIES = {
    "Review requested": "is:open is:pr review-requested:@me",
    "Assigned to you": "is:open is:pr assignee:@me",
    "Opened by you": "is:open is:pr author:@me",
}


class DigestError(Exception):
    """A user-facing error with a clean message (no traceback needed)."""


def load_env() -> None:
    """Load GITHUB_TOKEN from the repo's .env (real env still wins).

    Uses an absolute path so it works no matter the caller's working
    directory — SwiftBar runs plugins from an arbitrary cwd.
    """
    load_dotenv(dotenv_path=ENV_PATH)


def get_token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise DigestError(
            "GITHUB_TOKEN is not set.\n"
            "  Set it in your environment or in a .env file (see .env.example).\n"
            "  The token needs read-only access to pull requests:\n"
            "    - fine-grained PAT: 'Pull requests: read'\n"
            "    - classic PAT:      'repo' scope"
        )
    return token


def search_prs(query: str, token: str) -> list[dict]:
    """Run one search query, following pagination, returning all items."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    items: list[dict] = []
    page = 1
    while True:
        url = f"{SEARCH_URL}?q={quote(query)}&per_page=100&page={page}"
        try:
            resp = requests.get(url, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise DigestError(f"Network error talking to GitHub: {exc}") from exc

        _raise_for_api_errors(resp)

        payload = resp.json()
        batch = payload.get("items", [])
        items.extend(batch)

        # Search API caps total results at 1000; stop when we've drained the set.
        total = payload.get("total_count", 0)
        if len(batch) < 100 or len(items) >= min(total, 1000):
            break
        page += 1
    return items


def _raise_for_api_errors(resp: requests.Response) -> None:
    if resp.status_code == 200:
        return

    if resp.status_code == 401:
        raise DigestError(
            "Authentication failed (401). Your GITHUB_TOKEN is invalid or expired.\n"
            "  Generate a new token with read-only pull request access and update .env."
        )

    # Rate limiting shows up as 403 (or 429) with a depleted remaining count.
    remaining = resp.headers.get("X-RateLimit-Remaining")
    if resp.status_code in (403, 429) and remaining == "0":
        reset = resp.headers.get("X-RateLimit-Reset")
        when = ""
        if reset and reset.isdigit():
            reset_dt = datetime.fromtimestamp(int(reset), tz=timezone.utc).astimezone()
            when = f" Try again after {reset_dt:%Y-%m-%d %H:%M:%S %Z}."
        raise DigestError(f"GitHub rate limit exceeded.{when}")

    if resp.status_code == 403:
        raise DigestError(
            "Access forbidden (403). Your token may lack the required scope.\n"
            "  Needs read-only pull request access (fine-grained 'Pull requests: read'\n"
            "  or classic 'repo' scope)."
        )

    # Anything else: surface GitHub's own message if it gave one.
    try:
        message = resp.json().get("message", resp.text)
    except ValueError:
        message = resp.text
    raise DigestError(f"GitHub API error ({resp.status_code}): {message}")


def normalize(item: dict) -> dict:
    """Pull the fields we care about out of a search result item."""
    # repository_url looks like https://api.github.com/repos/<owner>/<repo>
    repo = item.get("repository_url", "").split("/repos/", 1)[-1] or "unknown/unknown"
    created = item.get("created_at")
    age_days = 0
    if created:
        created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - created_dt).days
    return {
        "repo": repo,
        "number": item.get("number"),
        "title": (item.get("title") or "").strip(),
        "author": (item.get("user") or {}).get("login", "unknown"),
        "url": item.get("html_url", ""),
        "age_days": age_days,
        "draft": bool(item.get("draft")),
        # Raw ISO string; used to detect "updated since you last looked".
        "updated_at": item.get("updated_at", ""),
    }


def _only_orgs() -> set[str]:
    """Restrict the digest to these repo owners, from PR_DIGEST_ONLY_ORGS
    (comma-separated, case-insensitive). Empty = no restriction (show all)."""
    raw = os.environ.get("PR_DIGEST_ONLY_ORGS", "")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def collect_prs(token: str) -> list[dict]:
    """Run every search and de-duplicate by PR URL. If PR_DIGEST_ONLY_ORGS is
    set, keep only PRs whose repo owner is in that allowlist."""
    by_url: dict[str, dict] = {}
    for label, query in QUERIES.items():
        for item in search_prs(query, token):
            pr = normalize(item)
            by_url.setdefault(pr["url"], pr).setdefault("sources", []).append(label)
    prs = list(by_url.values())
    orgs = _only_orgs()
    if orgs:
        prs = [pr for pr in prs if pr["repo"].split("/", 1)[0].lower() in orgs]
    return prs


def humanize(iso: str) -> str:
    """Compact relative time from an ISO timestamp: 'just now', '5m', '2h', '3d'."""
    if not iso:
        return ""
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    secs = (datetime.now(timezone.utc) - when).total_seconds()
    if secs < 60:
        return "just now"
    mins = secs / 60
    if mins < 60:
        return f"{int(mins)}m"
    if mins < 1440:
        return f"{int(mins // 60)}h"
    if mins < 10080:
        return f"{int(mins // 1440)}d"
    return f"{int(mins // 10080)}w"


def _auth_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get(url: str, token: str):
    """GET a JSON endpoint with the shared error handling; returns parsed JSON."""
    try:
        resp = requests.get(url, headers=_auth_headers(token), timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise DigestError(f"Network error talking to GitHub: {exc}") from exc
    _raise_for_api_errors(resp)
    return resp.json()


def _get_list(url: str, token: str) -> list[dict]:
    payload = _get(url, token)
    return payload if isinstance(payload, list) else []


def _ignored_logins() -> set[str]:
    """Extra bot logins to skip, from PR_DIGEST_IGNORE_LOGINS (comma-separated).
    For named bots that aren't flagged as type=Bot (e.g. a custom org bot)."""
    raw = os.environ.get("PR_DIGEST_IGNORE_LOGINS", "")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def _is_bot(user: dict, ignored: set[str]) -> bool:
    user = user or {}
    if user.get("type") == "Bot":  # GitHub's own flag (Copilot, *[bot], …)
        return True
    login = user.get("login", "").lower()
    return login.endswith("[bot]") or login in ignored


def _normalize_comment(item: dict, kind: str) -> dict:
    return {
        "author": (item.get("user") or {}).get("login", "unknown"),
        "body": (item.get("body") or "").strip(),
        "html_url": item.get("html_url", ""),
        "created_at": item.get("created_at", ""),
        "type": kind,  # "reply" (conversation) or "review" (inline code comment)
    }


def fetch_comments(token: str, repo: str, number: int) -> dict:
    """Recent human conversation replies + inline review comments for one PR.
    Bot comments are filtered out so the preview reflects who actually replied.

    Returns {"comments": <up to 5, newest first>, "count": <human total>}.
    """
    ignored = _ignored_logins()
    issues = _get_list(
        f"{API_ROOT}/repos/{repo}/issues/{number}/comments?per_page=100", token)
    reviews = _get_list(
        f"{API_ROOT}/repos/{repo}/pulls/{number}/comments?per_page=100", token)
    merged = [
        _normalize_comment(c, kind)
        for items, kind in ((issues, "reply"), (reviews, "review"))
        for c in items
        if not _is_bot(c.get("user") or {}, ignored)
    ]
    merged.sort(key=lambda c: c["created_at"], reverse=True)
    return {"comments": merged[:5], "count": len(merged)}


def fetch_pr_status(token: str, repo: str, number: int) -> dict:
    """Review + mergeability status for one PR:
    {approved, changes_requested, conflicts, draft}."""
    obj = _get(f"{API_ROOT}/repos/{repo}/pulls/{number}", token)
    obj = obj if isinstance(obj, dict) else {}
    # mergeable is computed async; None = unknown → don't claim a conflict.
    conflicts = obj.get("mergeable") is False or obj.get("mergeable_state") == "dirty"

    reviews = _get_list(
        f"{API_ROOT}/repos/{repo}/pulls/{number}/reviews?per_page=100", token)
    # Keep the last decisive review per reviewer (reviews come chronologically).
    latest: dict[str, str] = {}
    for r in reviews:
        state = r.get("state")
        if state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[(r.get("user") or {}).get("login", "")] = state
    states = set(latest.values())
    return {
        "approved": "APPROVED" in states and "CHANGES_REQUESTED" not in states,
        "changes_requested": "CHANGES_REQUESTED" in states,
        "conflicts": bool(conflicts),
        "draft": bool(obj.get("draft")),
    }


def enrich_prs(token: str, prs: list[dict]) -> list[dict]:
    """Enrich each PR in place with recent comments + review/merge status.
    Per-PR errors are swallowed so one bad PR can't blank the whole digest."""
    for pr in prs:
        try:
            data = fetch_comments(token, pr["repo"], pr["number"])
        except DigestError:
            data = {"comments": [], "count": 0}
        pr["comments"] = data["comments"]
        pr["comment_count"] = data["count"]
        pr["latest"] = data["comments"][0] if data["comments"] else None
        try:
            pr["status"] = fetch_pr_status(token, pr["repo"], pr["number"])
        except DigestError:
            pr["status"] = {}
    return prs
