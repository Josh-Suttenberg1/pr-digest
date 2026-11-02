"""Harvest review comments that PR authors acted on, and propose review lessons.

    review/harvest_lessons.py [--days 90] [--no-propose]

1. Harvest (deterministic, read-only). Every PR in the configured repos updated in
   the window is searched, and each review thread is kept only when:
   - it was started by a human other than the PR author (bots and Copilot are out),
   - and GitHub marks it outdated: a commit after the comment changed the lines it
     was anchored to. That is the "the author acted on it" signal; a thread resolved
     with no code change, or argued down with nothing changed, never qualifies.
   Output: <out_dir>/harvest-<date>.json.

2. Propose (claude -p on models.lessons). The threads are too many for one context,
   so chunks of CHUNK threads each yield candidate rules (candidates-<date>.json),
   then one run merges them, keeps rules backed by 3+ threads, drops anything a repo's
   .ai/ files, the recurring checks, or the existing lessons already cover, and writes
   <out_dir>/proposals-<date>.md for Josh to approve one by one. Nothing is added to
   review-lessons.md automatically.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import settings  # noqa: E402

LESSONS_FILE = Path("~/avetta/.claude/review/review-lessons.md").expanduser()
CHECKS_FILE = Path("~/avetta/.claude/review/recurring-checks.md").expanduser()
BODY_LIMIT = 800
PAGE = 20

THREADS_QUERY = """
query($q: String!, $after: String) {
  search(type: ISSUE, query: $q, first: %d, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest {
      number url title author { login }
      repository { name }
      reviewThreads(first: 60) { nodes {
        isOutdated isResolved path
        comments(first: 15) { nodes {
          url body createdAt author { login __typename }
        } }
      } }
    } }
  }
}
""" % PAGE


def is_bot(author: dict | None, bots: list[str]) -> bool:
    if not author:
        return True                       # deleted ("ghost") accounts carry no signal
    login = author.get("login") or ""
    return (author.get("__typename") == "Bot" or login.endswith("[bot]")
            or login in bots or "copilot" in login.lower())


def legitimate_threads(pr: dict, bots: list[str]) -> list[dict]:
    """The PR's review threads that a human reviewer started and the author acted on."""
    pr_author = (pr.get("author") or {}).get("login")
    out = []
    for thread in (pr.get("reviewThreads") or {}).get("nodes") or []:
        comments = (thread.get("comments") or {}).get("nodes") or []
        if not comments or not thread.get("isOutdated"):
            continue
        first = comments[0]
        if is_bot(first.get("author"), bots) or first["author"]["login"] == pr_author:
            continue
        out.append({
            "repo": pr["repository"]["name"],
            "pr": pr["number"],
            "pr_title": pr["title"],
            "pr_author": pr_author,
            "reviewer": first["author"]["login"],
            "path": thread.get("path"),
            "url": first["url"],
            "created_at": first["createdAt"],
            "resolved": bool(thread.get("isResolved")),
            "comment": first["body"][:BODY_LIMIT],
            "replies": [
                {"author": (c.get("author") or {}).get("login"), "body": c["body"][:BODY_LIMIT]}
                for c in comments[1:]
            ],
        })
    return out


def search_prs(query: str):
    after = None
    while True:
        argv = ["gh", "api", "graphql", "-f", f"query={THREADS_QUERY}", "-f", f"q={query}"]
        if after:
            argv += ["-f", f"after={after}"]
        data = json.loads(subprocess.run(argv, capture_output=True, text=True, check=True).stdout)
        search = data["data"]["search"]
        yield from (n for n in search["nodes"] if n)
        if not search["pageInfo"]["hasNextPage"]:
            return
        after = search["pageInfo"]["endCursor"]


def harvest(cfg: dict, days: int) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    bots = cfg["watcher"]["bots"]
    threads = []
    for repo in cfg["lessons"]["repos"]:
        query = f"repo:{cfg['org']}/{repo} is:pr updated:>={since}"
        n_prs = 0
        for pr in search_prs(query):
            n_prs += 1
            threads.extend(legitimate_threads(pr, bots))
        print(f"  {repo}: {n_prs} PRs scanned", file=sys.stderr)
    return threads


CHUNK = 250
MAP_WORKERS = 3

MAP_PROMPT = """Below are {n} code-review threads from Josh's repos, one JSON object per line.
Each is a comment a human reviewer left that the PR author then acted on (the commented lines
changed afterwards).

Find feedback that RECURS within them: the same kind of point made on at least two different
PRs. Ignore one-offs and points that only make sense for one specific change. Phrase each as a
rule a reviewer could check against any diff.

Output ONLY a JSON array, no prose, no code fences:
[{{"rule": "<one imperative sentence>", "why": "<one sentence, from what reviewers said>",
   "repos": ["..."], "reviewers": ["..."], "urls": ["<every matching thread url>"]}}]

THREADS:
{threads}
"""

PROPOSE_PROMPT = """You are distilling code-review lessons for the pr-reviewer agent.

{candidates} holds candidate rules, each with the review-thread URLs behind it. They were
extracted chunk by chunk from {n} review threads over the last {days} days on Josh's repos;
every thread is a comment a human reviewer left that the PR author then acted on.

First merge candidates that state the same rule (union their URLs, repos and reviewers). Keep
only rules backed by at least 3 threads, ideally from more than one reviewer.

For each remaining rule, check whether it is already covered, and drop it if so:
- the touched repo's .ai/ files (~/avetta/<repo>/.ai/*.md) and its CLAUDE.md / .claude/CLAUDE.md
- {checks}
- {lessons}
- ~/avetta/.claude/elm-conventions.md (for ui)

Write markdown only, no preamble. One section per proposed lesson, strongest first, at most 25:

### <n>. <the rule, one imperative sentence a reviewer can check against a diff>
- Seen: <count> threads, <repos>, reviewers <logins>
- Examples: <2-3 thread URLs from the file>
- Why it matters: <one sentence, from what the reviewers actually said>
- Suggested home: review-lessons.md (cross-repo) or <repo>/.ai/<file> (repo-specific)

End with a section "### Considered and dropped" listing, one line each, recurring points you
dropped because an existing rule already covers them, naming that rule's file.
"""


def _claude(cfg: dict, prompt: str, tools: bool) -> str:
    """One claude -p run on models.lessons; the prompt goes in on stdin. Returns the result."""
    argv = ["claude", "-p", "--model", cfg["models"]["lessons"], "--output-format", "json"]
    if tools:
        argv += ["--allowedTools", "Read", "Grep", "Glob",
                 "--disallowedTools", "Edit", "Write", "Bash", "Agent"]
    else:
        argv += ["--disallowedTools", "Read", "Grep", "Glob", "Edit", "Write", "Bash", "Agent",
                 "WebFetch", "WebSearch"]
    proc = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                          cwd=settings.path_of(cfg["claude_cwd"]), timeout=3600)
    envelope = json.loads(proc.stdout or "{}")
    if envelope.get("is_error") or not envelope.get("result"):
        raise SystemExit(f"claude run failed: {str(envelope.get('result'))[:300] or proc.stderr[-300:]}")
    return envelope["result"]


def compact(thread: dict) -> str:
    first_reply = thread["replies"][0]["body"][:200] if thread["replies"] else ""
    return json.dumps({
        "repo": thread["repo"], "reviewer": thread["reviewer"], "path": thread["path"],
        "url": thread["url"], "comment": thread["comment"][:500], "reply": first_reply,
    })


def parse_candidates(text: str) -> list[dict]:
    """The JSON array a map run returned, tolerating code fences or stray prose."""
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return []
    return [c for c in data if isinstance(c, dict) and c.get("rule")]


def propose(cfg: dict, threads: list[dict], days: int, out_dir: Path) -> Path:
    from concurrent.futures import ThreadPoolExecutor

    chunks = [threads[i:i + CHUNK] for i in range(0, len(threads), CHUNK)]

    def map_chunk(chunk: list[dict]) -> list[dict]:
        prompt = MAP_PROMPT.format(n=len(chunk), threads="\n".join(compact(t) for t in chunk))
        return parse_candidates(_claude(cfg, prompt, tools=False))

    with ThreadPoolExecutor(max_workers=MAP_WORKERS) as pool:
        candidates = [c for found in pool.map(map_chunk, chunks) for c in found]
    print(f"  {len(candidates)} candidate rules from {len(chunks)} chunks", file=sys.stderr)

    candidates_path = out_dir / f"candidates-{date.today()}.json"
    candidates_path.write_text(json.dumps(candidates, indent=2) + "\n")
    prompt = PROPOSE_PROMPT.format(
        candidates=candidates_path, n=len(threads), days=days,
        checks=CHECKS_FILE, lessons=LESSONS_FILE,
    )
    result = _claude(cfg, prompt, tools=True)
    out_path = out_dir / f"proposals-{date.today()}.md"
    header = (f"# Proposed review lessons ({date.today()})\n\n"
              f"From {len(threads)} acted-on review threads over {days} days. "
              f"Approve each one before it goes into {LESSONS_FILE} or a repo's .ai/ files.\n\n")
    out_path.write_text(header + result.strip() + "\n")
    return out_path


def main(argv: list[str] | None = None) -> int:
    cfg = settings.load()
    parser = argparse.ArgumentParser(description="Harvest acted-on review comments into proposed lessons.")
    parser.add_argument("--days", type=int, default=cfg["lessons"]["days"])
    parser.add_argument("--no-propose", action="store_true", help="harvest only, no claude call")
    args = parser.parse_args(argv)

    out_dir = settings.path_of(cfg["lessons"]["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    threads = harvest(cfg, args.days)
    harvest_path = out_dir / f"harvest-{date.today()}.json"
    harvest_path.write_text(json.dumps(threads, indent=2) + "\n")
    print(f"{len(threads)} acted-on threads → {harvest_path}", file=sys.stderr)

    if not args.no_propose and threads:
        proposals = propose(cfg, threads, args.days, out_dir)
        print(f"proposals → {proposals}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
