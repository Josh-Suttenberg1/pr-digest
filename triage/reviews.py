"""Reviews you owe: a live GitHub triage of every PR that could be on your plate.

Candidates are every open, non-draft PR (not your own) that requests your review —
directly or via one of your teams — that a teammate (`[triage] team`) opened, or that
you have reviewed before. A teammate's PR counts as asking for your review whether or
not GitHub requests you (`team_ask` on its state). For each, every
reviewer's standing decision is their latest APPROVED / CHANGES_REQUESTED / DISMISSED
review; a COMMENTED review never overrides an approval. Then, in this order:

  * WELL_APPROVED+ approvals, no change
    requests                          → well_approved (hidden). Outranks everything,
                                        including "you approved and it moved since".
  * you approved, and your review was
    not explicitly re-requested since → done (hidden), however much code landed after
                                        it. Only a re-request puts an approved PR back.
  * a change request (yours or anyone's) stands against the current code, with nothing
    pushed since it                   → author. For your own change request, an author
                                        reply on your thread or a re-request also
                                        brings it back to you.
  * you commented without a decision, and since your last review or comment there are
    no real commits, no author reply on your threads, no author top-level comment or
    review body, and no re-request   → author.
  * everything else                   → needs_you: not reviewed yet, review dismissed,
                                        or real commits / author replies since your
                                        comment, approval or change request.

needs_you PRs are then ranked into tiers (re-review, first review, parked) by `_rank`.
Parked means your review can't unblock the PR yet: it already has WELL_APPROVED
approvals and waits on someone else's change request, or CI is failing.

"Real commits" excludes merges of the base branch that only combine the two sides, and
commits that touch nothing but blank lines or trailing whitespace (a formatter pass). A
merge whose resolution writes or drops its own code still counts. Telling those apart
needs the repo cloned under `repos_root`; without a clone every commit counts.

Configuration comes from settings (`org`, `repos_root`, `watcher.required_approvals`,
`triage.pod`, `triage.team`, `triage.team_tickets`, `triage.ci_ignored`) through
`configure()`, which runs at
import. Read-only against GitHub; it may fetch a PR's commit objects into the local
clone's object store (no branch, index, worktree or FETCH_HEAD change).
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import names
import settings
from triage import priority

ORG = "avetta"
REPOS_ROOT = Path.home()
# At or above this many distinct approvals, with no change request standing, a PR is
# hidden from the review list.
WELL_APPROVED = 2
# Authors whose PRs rank higher: same-pod work ships together, so a review stalled on
# you stalls your own pod.
POD: set[str] = set()
# Teammates whose PRs count as asking for your review (priority.team_pr), and the Jira
# project keys such a PR's title must carry (none: any title).
TEAM: set[str] = set()
TEAM_TICKETS: tuple[str, ...] = ()
# Checks whose failure says nothing about whether the author is done (matched as a
# case-insensitive substring of the check name).
CI_IGNORED: tuple[str, ...] = ()
# GitHub login -> the name shown for that person (names.py).
NAMES = {}
# Logins that never count as a person engaging with a PR (lower-case). GitHub Apps and
# `[bot]` logins are recognized on their own.
BOTS: set[str] = set()
# Days of waiting that still raise a PR's rank; past this, age stops counting.
MAX_WAIT_DAYS = 6
# A diff at or under this many changed lines is a quick review and ranks a bit higher.
QUICK_LINES = 50
# PRs triaged at once. Each one is a handful of sequential gh calls.
WORKERS = 8

TIER_REREVIEW, TIER_FIRST, TIER_PARKED = 0, 1, 2

# One lock per local clone, so parallel triage never runs two fetches into one repo.
_FETCH_LOCKS: dict[str, threading.Lock] = {}
_FETCH_LOCKS_GUARD = threading.Lock()


def configure(cfg: dict | None = None) -> None:
    """Set the module's knobs from a settings dict (default: settings.load())."""
    global ORG, REPOS_ROOT, WELL_APPROVED, POD, TEAM, TEAM_TICKETS, CI_IGNORED, NAMES, BOTS
    cfg = cfg if cfg is not None else settings.load()
    ORG = cfg.get("org") or ORG
    REPOS_ROOT = settings.path_of(cfg.get("repos_root") or cfg.get("workspace") or str(REPOS_ROOT))
    WELL_APPROVED = int((cfg.get("watcher") or {}).get("required_approvals", WELL_APPROVED))
    tri = cfg.get("triage") or {}
    POD = set(tri.get("pod") or [])
    TEAM = set(tri.get("team") or [])
    TEAM_TICKETS = tuple(tri.get("team_tickets") or ())
    CI_IGNORED = tuple(s.lower() for s in tri.get("ci_ignored") or [])
    NAMES = names.book(fetch=True)
    BOTS = {b.lower() for b in (cfg.get("watcher") or {}).get("bots") or []}


configure()


def _gh_json(args: list[str]) -> list | dict | None:
    """Run a gh command and parse JSON, or None if gh/network/auth is unavailable."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout or "null")
    except ValueError:
        return None


def _is_after(later: str | None, earlier: str | None) -> bool:
    """True when `later` is strictly after `earlier`; unknown timestamps → False."""
    def _parse(stamp):
        if not stamp:
            return None
        try:
            out = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            return None
        return out.replace(tzinfo=timezone.utc) if out.tzinfo is None else out
    a, b = _parse(later), _parse(earlier)
    return bool(a and b and a > b)


def me_login(fallback: str = "") -> str:
    who = _gh_json(["gh", "api", "user", "--jq", ".login"])
    if isinstance(who, str) and who.strip():
        return who.strip()
    obj = _gh_json(["gh", "api", "user"])
    if isinstance(obj, dict) and obj.get("login"):
        return obj["login"]
    return fallback


def my_teams() -> list[str]:
    """Your team slugs in ORG. A PR can request review from a team rather than the
    individual, and `--review-requested=@me` does not match those, so the candidate
    search also checks `team-review-requested:<org>/<slug>` per team."""
    teams = _gh_json(["gh", "api", "user/teams", "--jq",
                      f'[.[] | select(.organization.login=="{ORG}") | .slug]'])
    return [t for t in teams if isinstance(t, str)] if isinstance(teams, list) else []


def draft_freshness(url: str, head: str, drafts: list[dict]) -> tuple[Path | None, str]:
    """Match an open PR to its local review draft (if any) and judge whether that
    draft still describes the current head. `head` is the live head sha; each draft
    is a dict with `pr_url`, `path` and `draft_head` (the sha it was written against).

    Equal ⇒ current, different ⇒ stale, unknown ⇒ can't tell (say so rather than
    implying fresh). Returns (draft path or None, freshness label)."""
    draft = next((d for d in drafts if d["pr_url"] == url), None)
    if draft is None:
        return None, "no draft — kick one off"
    reviewed = (draft.get("draft_head") or "").lower()
    head = (head or "").lower()
    if reviewed and head:
        if head.startswith(reviewed) or reviewed.startswith(head):
            return draft["path"], "draft current ✓"
        return draft["path"], f"draft STALE (against {reviewed[:8]}, head {head[:8]}) — refresh"
    return draft["path"], "draft freshness unknown — verify/refresh"


def _merge_adds_content(cc_diff: str, n_parents: int) -> bool:
    """Does a merge's combined diff (`git show --cc --format=`) contain a line that
    came from *neither* parent?

    Each hunk line carries one prefix column per parent. A line added relative to
    every parent (all `+`, e.g. `++`) is content the author wrote during resolution; a
    line removed relative to every parent (all `-`) is content both sides kept that
    the resolution dropped. Anything else — `+`/`-` in only some columns — is a line
    taken verbatim from one side. So a hunk made only of partial-column lines is a
    union of the two sides (e.g. main's import rewrite next to the branch's new
    import): a textual conflict with no decision in it. Unresolved `<<<<<<<` markers
    are all-`+` lines and so count as added content."""
    added, removed = "+" * n_parents, "-" * n_parents
    in_hunk = False
    for line in cc_diff.splitlines():
        if line.startswith("diff --"):
            in_hunk = False
        elif line.startswith("@" * (n_parents + 1) + " "):
            in_hunk = True
        elif in_hunk and (line.startswith(added) or line.startswith(removed)):
            return True
    return False


def _commit_has_content(git, sha: str) -> bool:
    """Does a non-merge commit change anything beyond blank lines and trailing
    whitespace? A formatter pass that only drops a blank line between import groups
    leaves no hunk under `--ignore-blank-lines --ignore-space-at-eol`, so it is not
    something to re-review. Leading whitespace still counts — Scala 3 and YAML
    indentation is semantic. A binary change has no hunk but is content. Unreadable
    ⇒ True, so a real change is never hidden."""
    shown = git("show", "--ignore-blank-lines", "--ignore-space-at-eol", "--format=", sha)
    if shown is None or shown.returncode != 0:
        return True
    return any(ln.startswith("@@") or ln.startswith("Binary files")
               for ln in shown.stdout.splitlines())


def _fetch_lock(repo: Path) -> threading.Lock:
    with _FETCH_LOCKS_GUARD:
        return _FETCH_LOCKS.setdefault(str(repo), threading.Lock())


def _new_work_since(owner_repo: str, pr_number, approved_sha: str, head: str,
                    passes=None) -> bool:
    """Did work you need to re-review land after your review, or was it only a branch
    update, a whitespace-only formatter pass, or (with `passes`) a commit the caller
    has ruled out?

    Walks the branch's own (first-parent) commits between the sha you reviewed and the
    live head, and judges each:
      * a non-merge commit with real content      → real new work     → True
      * a non-merge commit touching only blank
        lines / trailing whitespace, or one
        `passes(git, sha)` accepts                → nothing new       → keep looking
      * a merge whose resolution adds new content → author's decision → True
      * a merge that only combines the two sides  → nothing to review → keep looking
    Every commit since the review being a combine-only merge ⇒ False — an "Update
    branch" / `git merge main`, which is hidden.

    The combined diff (`git show --cc`) holds only the hunks where the merge result
    differs from *both* parents. It is empty for an automatic merge, and for a
    conflict resolved by keeping both sides it contains only lines taken verbatim from
    one parent or the other — neither is something to re-review. A merge counts as
    new work only when `_merge_adds_content` finds a line from neither parent.

    Needs the repo cloned under REPOS_ROOT to read the combined diff; when it isn't, or
    git can't resolve the history (a rebase/force-push detaches the reviewed sha), it
    errs toward True so a real re-review is never hidden."""
    approved = (approved_sha or "").lower()
    head = (head or "").lower()
    if not approved or not head:
        return True
    repo = REPOS_ROOT / owner_repo.split("/")[-1]
    if not (repo / ".git").exists():
        return True                        # not cloned — can't inspect, so show it

    def _git(*args):
        try:
            return subprocess.run(["git", "-C", str(repo), *args],
                                  capture_output=True, text=True, timeout=45)
        except (OSError, subprocess.TimeoutExpired):
            return None

    with _fetch_lock(repo):
        present = _git("cat-file", "-e", f"{head}^{{commit}}")
        if present is None or present.returncode != 0:
            got = _git("fetch", "--no-write-fetch-head", "-q", "origin",
                       f"pull/{pr_number}/head")
            if got is None or got.returncode != 0:
                return True                # couldn't obtain the commits — show it
    # The branch's own new commits since the review, each printed with its parents.
    rev = _git("rev-list", "--first-parent", "--parents", f"{approved}..{head}")
    if rev is None or rev.returncode != 0:
        return True
    rows = [ln.split() for ln in rev.stdout.splitlines() if ln.strip()]
    if not rows:
        return True                        # review not an ancestor (rebase) — show
    for row in rows:
        sha, parents = row[0], row[1:]
        if len(parents) < 2:
            if passes is not None and passes(_git, sha):
                continue                   # the caller ruled this commit out
            if _commit_has_content(_git, sha):
                return True                # a non-merge branch commit = real new work
            continue                       # only blank lines / trailing whitespace
        shown = _git("show", "--cc", "--format=", sha)
        if shown is None or shown.returncode != 0:
            return True                    # can't read the merge — show it
        if _merge_adds_content(shown.stdout, len(parents)):
            return True                    # resolution wrote/dropped its own content
    return False                           # every new commit only combines the sides


def candidates(me: str, teams: list[str]) -> dict[str, dict]:
    """Every open, non-draft PR that could be on your plate: GitHub requests your
    review (directly or via one of your teams), a teammate opened it with a ticket key
    in its title (priority.team_pr), or you have reviewed it before. Your own PRs are
    excluded. Keyed by PR url. A GitHub draft PR is never a candidate — it is not ready
    for review by definition."""
    found: dict[str, dict] = {}
    queries = [["--review-requested=@me"], ["--reviewed-by=@me"]]
    queries += [[f"team-review-requested:{ORG}/{t}"] for t in teams]
    for query in queries:
        prs = _gh_json(["gh", "search", "prs", f"--owner={ORG}", "--state=open",
                        "--draft=false", *query, "--json",
                        "url,title,author,repository,isDraft", "--limit", "100"])
        for pr in prs if isinstance(prs, list) else []:
            url = (pr.get("url") or "").rstrip("/")
            author = (pr.get("author") or {}).get("login", "")
            if not url or pr.get("isDraft") or (me and author.lower() == me.lower()):
                continue
            found.setdefault(url, pr)
    for url, pr in _team_candidates(me).items():
        found.setdefault(url, pr)
    return found


def _team_candidates(me: str) -> dict[str, dict]:
    """Your teammates' open, non-draft PRs that count as asking for your review
    (priority.team_pr), shaped like `gh search prs` results. The REST search API is
    used because it matches any of several `author:` qualifiers, which `gh search prs`
    doesn't."""
    q = priority.team_search(ORG, me, TEAM)
    if not q:
        return {}
    got = _gh_json(["gh", "api", "-X", "GET", "search/issues", "-f", f"q={q}",
                    "-f", "per_page=100"])
    out = {}
    for item in (got or {}).get("items") or [] if isinstance(got, dict) else []:
        url = (item.get("html_url") or "").rstrip("/")
        author = (item.get("user") or {}).get("login", "")
        title = item.get("title") or ""
        if (not url or item.get("draft") or author.lower() == (me or "").lower()
                or not priority.team_pr(author, title, TEAM, TEAM_TICKETS)):
            continue
        repo = "/".join((item.get("repository_url") or "").rstrip("/").split("/")[-2:])
        out[url] = {"url": url, "title": title, "author": {"login": author},
                    "repository": {"nameWithOwner": repo}, "isDraft": False}
    return out


def _review_state(url: str, pr: dict, me: str, teams: list[str]) -> dict | None:
    """Live review state of one PR, read from GitHub.

    Each reviewer's standing decision is their latest APPROVED / CHANGES_REQUESTED /
    DISMISSED review, in submission order. A COMMENTED review changes nothing — an
    approval stays in force after a later comment — so reading only someone's
    *latest* review misfiles an approver who then leaves a nit as "not approved".
    A dismissed review cancels that reviewer's decision. The PR author's own reviews
    are ignored. `approvals` is therefore distinct reviewers, not approval events."""
    owner_repo = (pr.get("repository") or {}).get("nameWithOwner", "")
    num = int(url.split("/")[-1])
    pull = _gh_json(["gh", "api", f"repos/{owner_repo}/pulls/{num}"])
    reviews = _gh_json(["gh", "api", "--paginate",
                        f"repos/{owner_repo}/pulls/{num}/reviews"])
    if not isinstance(pull, dict) or not isinstance(reviews, list):
        return {"unreadable": True, "pr_url": url}
    if pull.get("draft") or pull.get("state") != "open":
        return None
    author = (pull.get("user") or {}).get("login", "")
    decisions: dict[str, tuple[str, str]] = {}
    decided_at: dict[str, str] = {}     # login -> when their standing decision was made
    commented = False
    my_review: tuple[str, str] = ("", "")   # (submitted_at, commit sha) of your latest review
    for review in sorted(reviews, key=lambda r: r.get("submitted_at") or ""):
        user = (review.get("user") or {}).get("login", "")
        if not user or user == author:
            continue
        state = review.get("state")
        if user == me:
            commented = True
            my_review = (review.get("submitted_at") or "",
                         (review.get("commit_id") or "").lower())
        if state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            decisions[user] = (state, (review.get("commit_id") or "").lower())
            decided_at[user] = review.get("submitted_at") or ""
    reviewers = sorted({(r.get("user") or {}).get("login", "") for r in reviews
                        if not _is_bot_user(r.get("user"))} - {"", author, me})
    requested_direct = any((u or {}).get("login") == me
                           for u in pull.get("requested_reviewers") or [])
    requested = requested_direct or any((t or {}).get("slug") in teams
                                        for t in pull.get("requested_teams") or [])
    title = pull.get("title") or pr.get("title") or ""
    team_ask = not requested and priority.team_pr(author, title, TEAM, TEAM_TICKETS)
    return {
        "pr_url": url,
        "pr_number": num,
        "owner_repo": owner_repo,
        "author": author,
        "title": title,
        "tickets": re.findall(r"\b[A-Z][A-Z0-9]+-\d+", title)[:2],
        "head_sha": ((pull.get("head") or {}).get("sha") or "").lower(),
        "size": f"+{pull.get('additions', 0)}/−{pull.get('deletions', 0)}",
        "lines_changed": (pull.get("additions") or 0) + (pull.get("deletions") or 0),
        "created_at": pull.get("created_at") or "",
        "approvals": sorted(u for u, (s, _) in decisions.items() if s == "APPROVED"),
        "change_requests": {u: sha for u, (s, sha) in decisions.items()
                            if s == "CHANGES_REQUESTED" and u != me},
        "change_requested_at": {u: decided_at[u] for u, (s, _) in decisions.items()
                                if s == "CHANGES_REQUESTED" and u != me},
        "mine": decisions.get(me),
        "commented": commented,
        "my_review": my_review,
        "reviewers": reviewers,
        "author_reviews": [r for r in reviews
                           if (r.get("user") or {}).get("login") == author],
        "me": me,
        "teams": teams,
        "requested": requested,
        "requested_direct": requested_direct,
        "team_ask": team_ask,
    }


def _is_bot_user(user: dict | None) -> bool:
    """A REST `user` that is a GitHub App, a `[bot]` login, or a configured bot."""
    login = ((user or {}).get("login") or "").lower()
    return (not login or (user or {}).get("type") == "Bot" or login.endswith("[bot]")
            or login in BOTS)


def _issue_comments(st: dict) -> list[dict]:
    """The PR's top-level comments, fetched once and kept on `st`."""
    if "issue_comments" not in st:
        issue = _gh_json(["gh", "api", "--paginate",
                          f"repos/{st['owner_repo']}/issues/{st['pr_number']}/comments"])
        st["issue_comments"] = issue if isinstance(issue, list) else []
    return st["issue_comments"]


def _engaged(st: dict) -> list[str]:
    """Everyone other than you, the author and bots who has reviewed the PR (any
    review, so inline comments count) or left a top-level comment on it."""
    people = set(st.get("reviewers") or [])
    people |= {(c.get("user") or {}).get("login", "") for c in _issue_comments(st)
               if not _is_bot_user(c.get("user"))}
    return sorted(people - {"", st["author"], st["me"]})


def _rerequested_at(st: dict, since: str) -> str:
    """When your review was first requested again after `since` (directly or via one
    of your teams), while that request is still pending; '' otherwise."""
    if not since or not st["requested"]:
        return ""
    stamps = [ev.get("created_at") or "" for ev in _timeline(st)
              if ev.get("event") == "review_requested" and _requests_me(st, ev)
              and _is_after(ev.get("created_at"), since)]
    return min(stamps, default="")


def _touched_at(st: dict) -> str:
    """When you last reviewed or left a top-level comment on the PR ('' if never):
    `_my_last_touch`'s time, without its commit lookup."""
    mine = [c.get("created_at") or "" for c in _issue_comments(st)
            if (c.get("user") or {}).get("login") == st["me"]]
    return max([st["my_review"][0] or "", *mine])


def _my_last_touch(st: dict) -> tuple[str, str]:
    """(time, sha) of your most recent review or top-level PR comment.

    Inline comments and thread replies are always part of a review, so the review list
    covers them. A top-level comment carries no commit, so when it is your latest touch
    the sha is the PR's last commit at that moment (from `pulls/<n>/commits`); the
    sha is empty when that can't be worked out."""
    when, sha = st["my_review"]
    mine = [c.get("created_at") or "" for c in _issue_comments(st)
            if (c.get("user") or {}).get("login") == st["me"]]
    latest = max(mine, default="")
    if latest and (not when or _is_after(latest, when)):
        when = latest
        if not sha:
            commits = _gh_json(["gh", "api", "--paginate",
                                f"repos/{st['owner_repo']}/pulls/{st['pr_number']}/commits"])
            before = [c for c in (commits if isinstance(commits, list) else [])
                      if not _is_after(((c.get("commit") or {}).get("committer") or {})
                                       .get("date"), when)]
            sha = (before[-1].get("sha") or "").lower() if before else ""
    return when, sha


def _author_moved_since(st: dict, since: str) -> str | None:
    """Why the PR is back on you after your last touch at `since` without new commits,
    or None when it isn't.

    Counts only moves aimed at you: the author replying on a thread you took part in,
    the author leaving a top-level comment or a review with a body, or anyone
    re-requesting your review (directly or via one of your teams) after `since`.
    Another reviewer's activity, or the author answering someone else's thread, leaves
    the PR with the author."""
    repo, num, author, me = st["owner_repo"], st["pr_number"], st["author"], st["me"]
    if not since:
        return None
    inline = _gh_json(["gh", "api", "--paginate", f"repos/{repo}/pulls/{num}/comments"])
    inline = inline if isinstance(inline, list) else []
    my_threads = {c.get("in_reply_to_id") or c.get("id") for c in inline
                  if (c.get("user") or {}).get("login") == me}
    for c in inline:
        if ((c.get("user") or {}).get("login") == author
                and (c.get("in_reply_to_id") or c.get("id")) in my_threads
                and _is_after(c.get("created_at"), since)):
            return "the author replied on your thread"
    if any((c.get("user") or {}).get("login") == author
           and _is_after(c.get("created_at"), since) for c in _issue_comments(st)):
        return "the author commented on the PR"
    if any((r.get("body") or "").strip() and _is_after(r.get("submitted_at"), since)
           for r in st["author_reviews"]):
        return "the author replied in a review"
    for ev in _timeline(st):
        if ev.get("event") != "review_requested" or not _is_after(ev.get("created_at"), since):
            continue
        if _requests_me(st, ev):
            return "you were re-requested"
    return None


def _timeline(st: dict) -> list[dict]:
    """The PR's issue timeline, fetched once and kept on `st`."""
    if "timeline" not in st:
        events = _gh_json(["gh", "api", "--paginate",
                           f"repos/{st['owner_repo']}/issues/{st['pr_number']}/timeline"])
        st["timeline"] = events if isinstance(events, list) else []
    return st["timeline"]


def _requests_me(st: dict, ev: dict) -> bool:
    """A `review_requested` event aimed at you directly or at one of your teams."""
    return ((ev.get("requested_reviewer") or {}).get("login") == st["me"]
            or (ev.get("requested_team") or {}).get("slug") in st["teams"])


def _rerequested_since(st: dict, since: str) -> bool:
    """True when your review (directly, or via one of your teams) was requested again
    after `since`. Read from the timeline rather than `requested_reviewers`, because a
    team request can outlive a member's review and would look like a re-request."""
    if not since:
        return False
    return any(ev.get("event") == "review_requested"
               and _is_after(ev.get("created_at"), since) and _requests_me(st, ev)
               for ev in _timeline(st))


def _work_since(st: dict, sha: str, passes=None) -> bool:
    """Real new work on the branch after `sha` (see `_new_work_since`)."""
    head = st["head_sha"]
    if sha and head and (head.startswith(sha) or sha.startswith(head)):
        return False
    return _new_work_since(st["owner_repo"], st["pr_number"], sha, head, passes)


def _triage(st: dict) -> tuple[str, str]:
    """Decide whose court a PR is in. Returns (bucket, reason):

      needs_you     — you owe a review: you haven't made an approve/request-changes
                      decision yet, or real work landed after your change request, or
                      your review was re-requested after you approved.
      author        — the next move is the author's: a change request (yours or
                      anyone's) stands against the current code with nothing pushed
                      since it, or you commented and neither commits nor an author
                      response (see `_author_moved_since`) have landed since.
      well_approved — WELL_APPROVED+ distinct approvals and no standing change
                      request. This outranks everything, including "you approved and
                      it moved since": enough people have signed off.
      done          — you approved and your review hasn't been re-requested since.
                      New commits alone never bring an approved PR back.

    A change request counts as standing until real work lands after it; once the
    author pushes past it, it no longer blocks and the PR is back with reviewers."""
    if len(st["approvals"]) >= WELL_APPROVED and not st["change_requests"] and not (
            st["mine"] and st["mine"][0] == "CHANGES_REQUESTED"):
        return "well_approved", f"{len(st['approvals'])} approvals, no change requests"
    state, sha = st["mine"] or (None, "")
    if state == "APPROVED" and not _rerequested_since(st, st["my_review"][0]):
        return "done", "you approved; your review wasn't re-requested since"
    # Someone else's change request on the current code outranks every reason the PR
    # might otherwise be back on you: the next move is the author's.
    if st["standing"]:
        return "author", ("@" + ", @".join(st["standing"])
                          + " requested changes on the current code; nothing pushed since")
    if state == "APPROVED":
        return "needs_you", f"you approved @{sha[:8]}; your review was re-requested since"
    if state == "CHANGES_REQUESTED":
        if _work_since(st, sha):
            return "needs_you", f"you requested changes @{sha[:8]}; the author has pushed since"
        if st["requested"]:
            return "needs_you", "you requested changes; the author re-requested your review"
        moved = _author_moved_since(st, _my_last_touch(st)[0])
        if moved:
            return "needs_you", f"you requested changes; {moved}"
        return "author", "you requested changes; no new commits or replies since"
    if state == "DISMISSED":
        return "needs_you", "your earlier review was dismissed"
    if st["commented"]:
        since, sha = _my_last_touch(st)
        if not sha or _work_since(st, sha):
            return "needs_you", (f"you commented @{sha[:8]}, real commits landed since"
                                 if sha else "you commented; can't tell what landed since")
        moved = _author_moved_since(st, since)
        if moved:
            return "needs_you", f"you commented; {moved}"
        return "author", f"you commented @{sha[:8]}; no new commits or replies since"
    return "needs_you", "not reviewed yet"


def _is_rereview(st: dict) -> bool:
    """You have reviewed or commented on this PR before."""
    return bool(st["mine"] or st["commented"])


def _ci_failing(st: dict) -> list[str]:
    """Names of the checks failing on the PR's head, ignoring CI_IGNORED. A check that
    ran more than once counts as failing only when no run of it passed. Empty when CI
    is green, still running, or unreadable."""
    rollup = _gh_json(["gh", "pr", "view", st["pr_url"], "--json", "statusCheckRollup"])
    checks = rollup.get("statusCheckRollup") or [] if isinstance(rollup, dict) else []
    failed, passed = set(), set()
    for check in checks:
        name = check.get("name") or check.get("context") or ""
        if any(ignored in name.lower() for ignored in CI_IGNORED):
            continue
        verdict = (check.get("conclusion") or check.get("state") or "").upper()
        if verdict in ("FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"):
            failed.add(name)
        elif verdict == "SUCCESS":
            passed.add(name)
    return sorted(failed - passed)


def _opened_at(st: dict) -> str:
    """When the PR was opened for review: created, or last marked ready for review if
    it started as a GitHub draft."""
    ready = [ev.get("created_at") or "" for ev in _timeline(st)
             if ev.get("event") == "ready_for_review"]
    return max([st.get("created_at") or "", *ready])


def _waiting_since(st: dict) -> str:
    """When the PR landed on you. For a first review: when your review (or one of your
    teams') was first requested, or when the PR last left draft if that is later. For
    a re-review: the author's first move after your last touch — a commit, an author
    comment or review, or a re-request — falling back to the touch itself."""
    events = _timeline(st)
    if not _is_rereview(st):
        asked = [ev.get("created_at") or "" for ev in events
                 if ev.get("event") == "review_requested" and _requests_me(st, ev)]
        ready = [ev.get("created_at") or "" for ev in events
                 if ev.get("event") == "ready_for_review"]
        return max(min(asked, default="") or st["created_at"], max(ready, default=""))
    since = _my_last_touch(st)[0]
    moves = []
    for ev in events:
        kind = ev.get("event")
        if kind == "committed":
            stamp = (ev.get("committer") or {}).get("date")
        elif kind == "review_requested" and _requests_me(st, ev):
            stamp = ev.get("created_at")
        elif (kind in ("commented", "reviewed")
              and ((ev.get("actor") or ev.get("user") or {}).get("login")) == st["author"]):
            stamp = ev.get("created_at") or ev.get("submitted_at")
        else:
            continue
        if _is_after(stamp, since):
            moves.append(stamp)
    return min(moves, default=since)


def _days_since(stamp: str) -> float:
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - then).total_seconds() / 86400)


def short(login: str, me: str = "") -> str:
    if login == me:
        return "you"
    return NAMES.get(login) or f"@{login}"


def _rank(st: dict) -> None:
    """Place a needs_you PR in a tier and score it within the tier, recording the
    reasons as `tags` so a readout can show why it sits where it does.

    Tiers, in order: re-reviews (you have reviewed or commented and the author has
    moved since), first reviews, then parked — PRs your review can't unblock yet,
    because they already carry WELL_APPROVED approvals and wait on another reviewer's
    change request, or because CI is failing. Score within a tier: your own change
    request blocking the merge +4, your review requested of you personally (not just a
    team), or a teammate's PR that doesn't request you (`team_ask`), +3, your approval would be the last one needed +2, the author is in POD +2,
    a current draft found blockers (feedback is ready to post) +1, a diff of at most
    QUICK_LINES changed lines +1, and half a point per day waited, up to MAX_WAIT_DAYS."""
    mine = (st["mine"] or ("", ""))[0]
    n = len(st["approvals"])
    parked = []
    if n >= WELL_APPROVED and mine != "CHANGES_REQUESTED":
        waiting_on = ", ".join(short(u, st["me"]) for u in sorted(st["change_requests"]))
        parked.append(f"{n} approvals already; needs {waiting_on} to re-review")
    failing = _ci_failing(st)
    if failing:
        parked.append("CI failing: " + ", ".join(failing))
    score, tags = 0.0, []
    if mine == "CHANGES_REQUESTED":
        score += 4
        tags.append("your change request blocks it")
    if st["requested_direct"]:
        score += 3
        tags.append("re-requested you" if _is_rereview(st) else "asked you directly")
    elif st.get("team_ask"):
        score += 3
        tags.append("your teammate's PR")
    elif st["requested"]:
        tags.append("team request")
    if n == WELL_APPROVED - 1 and not parked:
        score += 2
        tags.append(f"your approval makes {WELL_APPROVED}")
    if st["author"] in POD:
        score += 2
        tags.append("your pod")
    if (st.get("draft") or {}).get("blocking") and "current" in st["freshness"]:
        score += 1
        tags.append("draft found blockers")
    if st["lines_changed"] <= QUICK_LINES:
        score += 1
        tags.append("quick")
    st["waiting_since"] = _waiting_since(st)
    st["opened_at"] = _opened_at(st)
    waited = _days_since(st["waiting_since"])
    score += min(waited, MAX_WAIT_DAYS) / 2
    tags.append(f"waiting {int(waited)}d" if waited >= 1 else "waiting <1d")
    st["tier"] = TIER_PARKED if parked else (
        TIER_REREVIEW if _is_rereview(st) else TIER_FIRST)
    st["score"], st["tags"] = score, parked + tags


def ranked(needs_you: list[dict]) -> list[dict]:
    """The needs_you list in reading order: by tier, then score (highest first), then
    smaller diffs. A ticket's sibling PRs (same key, other repos) in the same tier stay
    adjacent, at the position of the higher-ranked sibling."""
    order = sorted(needs_you, key=lambda st: (st["tier"], -st["score"],
                                              st["lines_changed"]))
    first_seen: dict[tuple, int] = {}
    for i, st in enumerate(order):
        first_seen.setdefault((st["tier"], (st["tickets"] or [st["pr_url"]])[0]), i)
    return sorted(order, key=lambda st: (
        st["tier"], first_seen[(st["tier"], (st["tickets"] or [st["pr_url"]])[0])]))


def _triage_one(url: str, pr: dict, me: str, teams: list[str],
                drafts: list[dict]) -> dict | None:
    st = _review_state(url, pr, me, teams)
    if st is None or st.get("unreadable"):
        return st
    st["standing"] = sorted(u for u, s in st["change_requests"].items()
                            if not _work_since(st, s))
    st["bucket"], st["reason"] = _triage(st)
    st["draft"] = next((d for d in drafts if d["pr_url"] == url), None)
    st["draft_path"], st["freshness"] = draft_freshness(url, st["head_sha"], drafts)
    st["touched_at"] = _touched_at(st)
    if st["bucket"] == "needs_you":
        _rank(st)
        st["engaged"] = _engaged(st)
    # A pending re-request of a PR you already reviewed or commented on puts it at the
    # top of your follow-ups (triage/priority.py), whatever the bucket.
    st["rerequested_at"] = (_rerequested_at(st, _my_last_touch(st)[0])
                            if _is_rereview(st) and st["requested"] else "")
    return st


def review_triage(me: str, drafts: list[dict] | None = None,
                  teams: list[str] | None = None) -> dict[str, list[dict]]:
    """Every candidate PR, triaged against live GitHub state, bucketed into
    needs_you / author / well_approved / done / unreadable. Each bucket keeps the
    candidates' search order. `drafts` (dicts with pr_url, path, draft_head, and
    optionally blocking) feed draft freshness and ranking. Read-only."""
    drafts = drafts or []
    teams = my_teams() if teams is None else teams
    out: dict[str, list[dict]] = {"needs_you": [], "author": [], "well_approved": [],
                                  "done": [], "unreadable": []}
    found = list(candidates(me, teams).items())
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(lambda item: _triage_one(*item, me, teams, drafts), found))
    for st in results:
        if st is None:
            continue
        out["unreadable" if st.get("unreadable") else st["bucket"]].append(st)
    return out
