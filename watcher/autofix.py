"""Automatic fixes for the nits and suggestions reviewers leave on your own PRs.

When a tick sees a human open a review thread on one of your PRs with a comment whose
kind (triage/comment_kind.py) is in `[autofix] categories`, it queues one job for that
thread (`enqueue`). A job is one JSON file, state/watcher/jobs/autofix/<job id>.json.
The id comes from (thread id, comment id), so a comment never queues twice. Helper
files are named <job id>.<suffix>.* and `all_jobs()` skips them; the folder sits below
the follow-up jobs folder, so followup.all_jobs() never sees these.

  queued  -> running (detached `python -m watcher autofix-run <job>`, at most one per
             PR at a time) -> done | escalated | dry_run
  queued, escalated or dry_run -> cleared, when you answer the thread yourself (you
             reply in it or resolve it: `cleared_jobs`, run each tick), when the PR
             merges or closes (`cancel_for_pr`), or when the run finds either first

A run (`Fix.run`) goes through these steps and stops at the first one that fails.
Every step's outcome is appended to the job's `steps`, so a stop says where it was:

  preconditions  code, not the model: the PR is yours and open, its head is the one
                 the comment was made on (allowing for this pipeline's own pushes,
                 `expected_head`), the thread is still waiting on you, no local
                 checkout of the branch has unpushed commits (`unpushed_checkouts`),
                 and no other fix runs on the PR (a per-PR lock).
  worktree       a clone of the repo at `[autofix] worktrees_dir`/<repo>-<number>
                 (borrowing objects from your clone, never sharing its branches),
                 checked out on the PR's branch at the remote head. Its push URL is a
                 guard that fails every push except the push step's own.
  validate-plan  how this repo is validated, from its files (`plan_validation`): an
                 Elm repo runs the Elm gate, an sbt build the tasks its files call for
                 (`sbt_tasks`), `[autofix.validate]` overrides both, and a repo of any
                 other type escalates.
  assess         headless claude with Read, Grep and Glob only returns strict JSON;
                 `mechanical` and `agrees` must be literally true and `confidence` at
                 least `min_confidence` (`parse_assessment`, `decide`).
  edit           headless claude with Read, Grep, Glob and Edit/Write scoped to the
                 worktree (no Bash, no git) makes the change from the prompt template.
  scope          the diff touches only the thread's file and the files the assessment
                 named, changes at most `max_lines` lines, and isn't empty
                 (`scope_problem`). Checked again after validation, since a formatter
                 in the validate command can touch more files.
  validate       that plan, in the worktree, with a timeout. The Elm gate is npm ci,
                 then one headless claude that runs the elm-validation agent and then
                 the elm-conventions-review agent (passed with --agents, since a clone
                 outside the workspace's tree doesn't load its project agents); a
                 conventions must-fix escalates, since only you can waive one.
  -- a dry run stops here, before anything is committed --
  commit         one commit for the comment, `Address <reviewer>'s <kind>: <summary>`.
  merge          origin/<base> merged in when the branch is behind; a conflict aborts
                 the merge. After a merge, validate runs again.
  push           `git push origin <branch>`, refused in code for the base branch, and
                 never forced (`push_argv`).
  reply          "Done — <summary> in <linked sha>." with the footer, in the thread.
  resolve        GraphQL resolveReviewThread.
  rerequest      the reviewer, once every thread they opened is answered, unless their
                 latest review is an approval (`rerequest_plan`).

A finished fix is `done` and shows in the menu's Done for you section, with a quiet
"✅ Fixed for you" banner. Any other stop is `escalated` with the reason; the thread
stays open, the PR's row in My PRs turns red ("Couldn't auto-fix: <reason>") and a red
alert goes out. A job queued with `[autofix] dry_run` on is marked `dry_run`: it stops
before the commit, never notifies, and leaves the PR's color alone.

Every local command (git, claude, the validate command) runs through `Tools.run`;
every GitHub write and the push run through `Tools.remote`; fresh PR snapshots come
from `Tools.fetch_pr`. Tests swap all three for fakes. Every claude run uses
--permission-mode dontAsk and --strict-mcp-config (no MCP servers load).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import notify  # noqa: E402
import settings  # noqa: E402
from triage import comment_kind  # noqa: E402
from watcher import followup, render  # noqa: E402
from watcher.state import JOBS_DIR  # noqa: E402

Runner = Callable[..., subprocess.CompletedProcess]

AUTOFIX_DIR = JOBS_DIR / "autofix"
PROMPT_FILE = Path(__file__).with_name("autofix_prompt.md")
FOOTER = followup.FOOTER
CO_AUTHOR = "Co-Authored-By: Claude <noreply@anthropic.com>"
UNFINISHED = ("queued", "running")
CLEARABLE = ("queued", "escalated", "dry_run")
# Branch names a fix never pushes to, whatever the PR's base is.
PROTECTED = ("main", "master")
TAIL = 3000
DIFF_LIMIT = 20_000
PLACEHOLDERS = ("me", "org", "pr_url", "comment", "path", "line", "plan", "reviewer",
                "kind", "files")

ASSESS_PROMPT = """A reviewer left a comment on @{me}'s pull request. Before anything is
changed, decide whether it can be fixed automatically. Change nothing yourself.

PR: {org}/{repo}#{number} — {title}
{pr_url}
Reviewer: @{reviewer}, whose comment reads as a {kind}.
File: {path}, line {line}
The comment ({comment_url}):
---
{comment}
---
{thread}
The working tree (the current folder) is the PR's head commit. Read the file and
whatever else you need. This repo's CLAUDE.md, .claude/CLAUDE.md and .ai/ docs, when
they exist, hold its conventions.

Decide:
- "agrees": true only when the comment is right and the change it asks for makes the
  code better.
- "mechanical": true only when the change is small, local and fully determined by the
  comment: no design choice, no behavior question, nothing to ask @{me} or @{reviewer}.
- "confidence": from 0 to 1, how sure you are that the fix, made exactly as you plan
  it, is correct and is what @{reviewer} meant.
- "files": every file the fix changes, relative to the repo root (normally just
  {path}).
- "plan": the exact edit, concretely, in one to three sentences.
- "reason": one sentence on your judgment, for @{me} to read when you say no.

Output ONLY a JSON object, no prose, no code fences:
{{"mechanical": true | false, "agrees": true | false, "confidence": 0.0,
  "files": ["..."], "plan": "...", "reason": "..."}}
"""

ELM_GATE_PROMPT = """This folder ({wt}) is a clone of the {repo} repo. Its staged, uncommitted
change fixes a reviewer's comment on {pr_url}. Run the gate this workspace requires
before any `.elm` commit, on this folder:

1. Launch the `elm-validation` agent on {wt}. Tell it: no test-count baseline is
   supplied; don't record the commit-gate mark (don't run elm-commit-gate.py); run
   Playwright only with CI=true, never a headed browser, and report any spec that
   needs a real login as skipped.
2. Only if it passes, launch the `elm-conventions-review` agent on {wt} in commit mode.
   Tell it not to record the commit-gate mark. No finding is waived.

Don't edit files yourself, and don't commit, push or post anything.

Then output ONLY a JSON object, no prose, no code fences:
{{"validation": "pass" | "fail", "conventions": "pass" | "fail" | "not_run",
  "must_fix": ["<path:line — rule — what to change>"],
  "summary": "<one line per gate: hooks, format, build, elm-review, tests with the count, playwright, conventions>",
  "failure": "<when something failed: the failing gate's error, quoted>"}}
"""

# What the Elm gate's session may run. The agents' own steps are these npm scripts and
# read-only git and shell commands; nothing that commits, pushes or posts.
ELM_GATE_BASH = [
    "Bash(npm run format:elm)", "Bash(npm run build:elm)", "Bash(npm run elm-review:dry)",
    "Bash(npm run test:elm)", "Bash(npm run pwt:*)", "Bash(CI=true npm run pwt:*)",
    "Bash(npx husky)", "Bash(git diff:*)", "Bash(git ls-files:*)", "Bash(git status:*)",
    "Bash(git log:*)", "Bash(git show:*)", "Bash(lsof:*)", "Bash(grep:*)", "Bash(sort:*)",
    "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)",
    "Bash([ -d .husky/_ ]:*)", "Bash(test -d:*)",
]
NEVER_BASH = ["Bash(git push:*)", "Bash(git commit:*)", "Bash(git reset:*)",
              "Bash(git checkout:*)", "Bash(git merge:*)", "Bash(git rebase:*)",
              "Bash(git remote:*)", "Bash(git config:*)", "Bash(gh:*)", "Bash(curl:*)",
              "Bash(python3 ~/.claude/hooks/elm-commit-gate.py:*)"]
# The clone's push URL while anything but the push step runs, so no other step can
# push even by accident.
PUSH_GUARD = "no-push-outside-the-autofix-push-step"

RESOLVE_QUERY = ("mutation($threadId: ID!) { resolveReviewThread(input: {threadId: $threadId}) "
                 "{ thread { isResolved } } }")


# --- jobs ------------------------------------------------------------------------------


def job_id(thread_id: str, comment_id: int) -> str:
    return hashlib.sha1(f"autofix:{thread_id}:{comment_id}".encode()).hexdigest()[:12]


def job_path(jid: str) -> Path:
    return AUTOFIX_DIR / f"{jid}.json"


def load_job(jid: str) -> dict:
    return json.loads(job_path(jid).read_text())


def save_job(job: dict) -> None:
    AUTOFIX_DIR.mkdir(parents=True, exist_ok=True)
    tmp = job_path(job["id"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(job, indent=2) + "\n")
    os.replace(tmp, job_path(job["id"]))


def all_jobs() -> list[dict]:
    """Every job file, `<job id>.json`; helper files (`<job id>.<suffix>.*`) aren't jobs."""
    if not AUTOFIX_DIR.exists():
        return []
    out = []
    for p in sorted(AUTOFIX_DIR.glob("*.json")):
        if "." in p.stem:
            continue
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, ValueError):
            continue
    return out


def _same_pr(job: dict, pr_id: str) -> bool:
    return (job.get("pr") or {}).get("id") == pr_id


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def candidates(opened: list[dict], categories) -> list[dict]:
    """The threads from events.opened_threads whose opening comment's kind is in
    `categories`, each with its `kind`. Pure."""
    out = []
    for t in opened:
        k = comment_kind.kind(t.get("body") or "")
        if k in categories:
            out.append({**t, "kind": k})
    return out


def new_job(pr: dict, thread: dict, cfg: dict) -> dict:
    """The job for one candidate thread (`candidates`) on PR snapshot `pr`."""
    return {
        "id": job_id(thread["thread_id"], thread["comment_id"]), "type": "autofix",
        "status": "queued", "created_at": _iso_now(),
        "dry_run": bool(cfg["autofix"]["dry_run"]),
        "pr": {k: pr.get(k) for k in ("id", "repo", "number", "title", "url", "author",
                                      "head", "head_ref", "base_ref")},
        "thread_id": thread["thread_id"], "comment_id": thread["comment_id"],
        "reviewer": thread["author"], "kind": thread["kind"], "body": thread.get("body") or "",
        "path": thread.get("path") or "", "line": thread.get("line"),
        "comment_url": thread.get("url") or "",
        "comment_commit": thread.get("original_commit") or thread.get("commit") or "",
        "steps": [],
    }


def enqueue(pr: dict, thread: dict, cfg: dict) -> str | None:
    """Queue a fix for one candidate thread; None when this comment already has one."""
    job = new_job(pr, thread, cfg)
    if job_path(job["id"]).exists():
        return None
    save_job(job)
    return job["id"]


def clear(jid: str, why: str) -> None:
    job = load_job(jid)
    job.update(status="cleared", cleared=why)
    save_job(job)


def cleared_jobs(pr: dict, jobs: list[dict], me: str) -> list[tuple[str, str]]:
    """(job id, why) for each of the PR's CLEARABLE jobs whose thread you answered
    yourself: it's resolved, gone, or has a reply from you after the comment. Pure;
    `pr` is a watcher snapshot (watcher/gh.py normalize_pr)."""
    threads = {t["id"]: t for t in pr["threads"]}
    out = []
    for job in jobs:
        if job.get("status") not in CLEARABLE or not _same_pr(job, pr["id"]):
            continue
        t = threads.get(job.get("thread_id"))
        if t is None:
            out.append((job["id"], "the thread is gone"))
        elif t["resolved"]:
            out.append((job["id"], "the thread is resolved"))
        elif any(c["author"] == me and c["id"] > (job.get("comment_id") or 0)
                 for c in t["comments"]):
            out.append((job["id"], "you replied in the thread"))
    return out


def cancel_for_pr(pr_id: str, state: str = "CLOSED", cfg: dict | None = None) -> list[str]:
    """Clear every CLEARABLE job on a PR that merged or closed, and delete its worktree
    once no fix is running there; returns the cleared ids."""
    why = "the PR merged" if state == "MERGED" else "the PR closed"
    out, jobs = [], [j for j in all_jobs() if _same_pr(j, pr_id)]
    for job in jobs:
        if job.get("status") in CLEARABLE:
            clear(job["id"], why)
            out.append(job["id"])
    if jobs and cfg and not any(j.get("status") == "running" for j in jobs):
        remove_worktree(jobs[0]["pr"], cfg)
    return out


def orphaned_prs(jobs: list[dict], open_ids) -> set[str]:
    """Ids of PRs with a CLEARABLE job that aren't among `open_ids`. Pure."""
    return {j["pr"]["id"] for j in jobs
            if j.get("status") in CLEARABLE and (j.get("pr") or {}).get("id")
            and j["pr"]["id"] not in open_ids}


def dismiss_done(jid: str, when: int | None = None) -> None:
    """Take a finished fix off the menu's Done for you section."""
    job = load_job(jid)
    job["done_dismissed_at"] = int(when if when is not None else time.time())
    save_job(job)


# --- pure checks --------------------------------------------------------------------------


def expected_head(job: dict, jobs: list[dict]) -> str:
    """The head the PR should have now: the head it had when the comment was queued,
    moved along by each fix this pipeline pushed to the PR since (`pushed_from` ->
    `pushed_sha`). Pure."""
    pushes = {j["pushed_from"]: j["pushed_sha"] for j in jobs
              if _same_pr(j, job["pr"]["id"]) and j.get("pushed_from") and j.get("pushed_sha")
              and j["id"] != job["id"]}
    head, seen = job["pr"]["head"], set()
    while head in pushes and head not in seen:
        seen.add(head)
        head = pushes[head]
    return head


def preconditions(job: dict, snap: dict | None, jobs: list[dict], cfg: dict,
                  alive: Callable[[int | None], bool] = followup._alive) -> tuple[str, str]:
    """("go" | "clear" | "escalate", why) before anything is touched. Pure apart from
    `alive`, which tells whether another job's process still runs. `snap` is a fresh
    watcher snapshot of the PR; `jobs` every autofix job."""
    me = cfg["me"]
    if snap is None:
        return "escalate", "couldn't read the PR from GitHub"
    if snap.get("state") in ("MERGED", "CLOSED"):
        return "clear", f"the PR {snap['state'].lower()}"
    if snap.get("author") != me:
        return "escalate", "the PR isn't yours"
    if snap.get("state") != "OPEN":
        return "escalate", "the PR isn't open"
    t = next((t for t in snap["threads"] if t["id"] == job["thread_id"]), None)
    if t is None:
        return "clear", "the thread is gone"
    if t["resolved"]:
        return "clear", "the thread is resolved"
    if any(c["author"] == me and c["id"] > (job.get("comment_id") or 0) for c in t["comments"]):
        return "clear", "you replied in the thread"
    root = t["comments"][0] if t["comments"] else {}
    kind = comment_kind.kind(root.get("body") or "")
    if kind not in cfg["autofix"]["categories"]:
        return "clear", f"the comment reads as a {kind}, which isn't in [autofix] categories"
    if snap.get("head") != expected_head(job, jobs):
        return "escalate", "the PR moved since the comment"
    if job.get("comment_commit") and job["comment_commit"] != job["pr"]["head"]:
        return "escalate", "the comment is on an older commit than the PR's head"
    branch, base = snap.get("head_ref") or "", snap.get("base_ref") or ""
    if not branch or not base:
        return "escalate", "couldn't tell the PR's branch or base"
    if branch == base or branch in PROTECTED:
        return "escalate", f"the PR's branch is {branch}, which is never pushed to"
    if any(j["id"] != job["id"] and _same_pr(j, job["pr"]["id"]) and j.get("status") == "running"
           and alive(j.get("pid")) for j in jobs):
        return "escalate", "another auto-fix is running on this PR"
    return "go", ""


def parse_assessment(text: str) -> dict:
    """The assessment's JSON; raises ValueError when it isn't one. `mechanical` and
    `agrees` are true only when the model said literally true."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the assessment")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("the assessment isn't a JSON object")
    conf = data.get("confidence")
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
        raise ValueError(f"bad confidence: {conf!r}")
    files = [f.strip() for f in data.get("files") or [] if isinstance(f, str) and f.strip()]
    return {"mechanical": data.get("mechanical") is True, "agrees": data.get("agrees") is True,
            "confidence": float(conf), "files": files,
            "plan": str(data.get("plan") or "").strip(),
            "reason": str(data.get("reason") or "").strip()}


def decide(a: dict, min_confidence: float) -> str | None:
    """Why the assessment stops the fix, or None when it can go ahead. Pure."""
    why = f": {a['reason']}" if a.get("reason") else ""
    if not a["agrees"]:
        return f"Claude disagrees with the comment{why}"
    if not a["mechanical"]:
        return f"it isn't a mechanical change{why}"
    if a["confidence"] < min_confidence:
        return (f"Claude is only {a['confidence']:.0%} sure, below the "
                f"{min_confidence:.0%} needed{why}")
    if not a["plan"]:
        return "the assessment gave no plan for the fix"
    return None


def parse_edit(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object after the edit")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("the edit's result isn't a JSON object")
    return {"changed": data.get("changed") is True,
            "summary": " ".join(str(data.get("summary") or "").split()),
            "reason": str(data.get("reason") or "").strip()}


def parse_elm_gate(text: str) -> dict:
    """The Elm gate session's JSON. `validation` and `conventions` pass only when the
    session said literally "pass"."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the gate's result")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("the gate's result isn't a JSON object")
    return {"validation": "pass" if data.get("validation") == "pass" else "fail",
            "conventions": data.get("conventions") if data.get("conventions") in
            ("pass", "fail", "not_run") else "fail",
            "must_fix": [str(m) for m in data.get("must_fix") or [] if str(m).strip()],
            "summary": str(data.get("summary") or "").strip(),
            "failure": str(data.get("failure") or "").strip()}


def safe_files(files: list[str]) -> list[str]:
    """Repo-relative paths only: nothing absolute and nothing climbing out with `..`."""
    return [f for f in files if not f.startswith("/") and ".." not in Path(f).parts]


def parse_numstat(text: str) -> dict[str, int | None]:
    """`git diff --numstat --no-renames` as path -> lines added plus removed (None for a
    binary file)."""
    out: dict[str, int | None] = {}
    for line in text.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added, removed, path = parts
        out[path] = None if "-" in (added, removed) else int(added) + int(removed)
    return out


def scope_problem(changed: dict[str, int | None], allowed: set[str], max_lines: int) -> str | None:
    """Why the edit reaches further than the comment, or None when it's in scope. Pure."""
    if not changed:
        return "the edit changed nothing"
    outside = sorted(set(changed) - allowed)
    if outside:
        more = f" and {len(outside) - 3} more" if len(outside) > 3 else ""
        return f"the fix reaches beyond the comment: it also changes {', '.join(outside[:3])}{more}"
    if any(n is None for n in changed.values()):
        return "the edit changed a binary file"
    total = sum(changed.values())
    if total > max_lines:
        return f"the fix changes {total} lines, more than the {max_lines} allowed"
    return None


def push_argv(branch: str, base: str) -> list[str]:
    """The one push a fix makes. Raises ValueError for a branch that's never pushed to."""
    if not branch or branch.startswith("-") or branch == base or branch in PROTECTED:
        raise ValueError(f"refusing to push to {branch or 'an empty branch name'}")
    return ["git", "push", "origin", branch]


def commit_message(job: dict, summary: str) -> str:
    return (f"Address {job['reviewer']}'s {job['kind']}: {summary}\n\n"
            f"{job.get('comment_url') or ''}\n\n{CO_AUTHOR}\n")


def reply_text(job: dict, summary: str, sha: str, org: str) -> str:
    link = f"[{sha[:7]}](https://github.com/{org}/{job['pr']['repo']}/commit/{sha})"
    summary = summary.rstrip(".")
    return f"Done — {summary} in {link}.\n\n{FOOTER}\n"


def rerequest_plan(snap: dict, reviewer: str, me: str, jobs: list[dict],
                   jid: str) -> tuple[bool, str]:
    """(re-request now, why). Only once every unresolved thread the reviewer opened
    has your reply last, no other fix of theirs on the PR is queued or running, their
    latest review isn't an approval, and they aren't already re-requested. Pure."""
    reviews = [r for r in snap["reviews"] if r["author"] == reviewer and r["state"] != "PENDING"]
    if reviews and reviews[-1]["state"] == "APPROVED":
        return False, "their latest review is an approval"
    waiting = 0
    for t in snap["threads"]:
        humans = [c for c in t["comments"] if not c["is_bot"]]
        if t["resolved"] or not humans or humans[0]["author"] != reviewer:
            continue
        if humans[-1]["author"] != me:
            waiting += 1
    if waiting:
        return False, f"{waiting} of their threads still wait on you"
    if any(j["id"] != jid and _same_pr(j, snap["id"]) and j.get("reviewer") == reviewer
           and j.get("status") in UNFINISHED for j in jobs):
        return False, "another of their comments is still being fixed"
    last = (reviews[-1].get("submitted_at") or "") if reviews else ""
    if any(q["reviewer"] == reviewer and (q.get("at") or "") > last
           for q in snap.get("review_requests") or []):
        return False, "their review is already requested"
    return True, "every thread they opened is answered"


def fill_template(template: str, values: dict[str, str]) -> str:
    """Each `{name}` in PLACEHOLDERS replaced; any other brace is left as written."""
    for k in PLACEHOLDERS:
        template = template.replace("{" + k + "}", str(values.get(k, "")))
    return template


# --- how a repo is validated -------------------------------------------------------------


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _has_tests(wt: Path) -> bool:
    """Unit test sources: test/ (Play layout) or src/test in the root or a subproject."""
    return ((wt / "test").is_dir() or (wt / "src" / "test").is_dir()
            or any(p.is_dir() for p in wt.glob("*/src/test")))


def sbt_tasks(wt: Path) -> tuple[list[str], list[str]]:
    """(sbt tasks, why each was chosen) for an sbt build, from the repo's own files, run
    without live services. compile and Test/compile always (unused-symbol warnings are
    fatal there in some repos); it:compile when the build has an `it` config;
    scalafixAll --check / scalafmtCheckAll only when both the plugin and its config file
    exist; test when there are unit test sources; it:test only when the integration
    tests use embedded Mongo (flapdoodle), never Testcontainers or a live database. Pure
    apart from reading files."""
    build, plugins = _read(wt / "build.sbt"), _read(wt / "project" / "plugins.sbt")
    project = build + "".join(_read(p) for p in (wt / "project").glob("*.scala"))
    tasks, why = ["compile", "Test/compile"], ["build.sbt"]
    has_it = 'config("it")' in build or "IntegrationTest" in build or (wt / "it").is_dir()
    if has_it:
        tasks.append("it:compile")
        why.append("an `it` config")
    if "sbt-scalafix" in plugins and (wt / ".scalafix.conf").exists():
        tasks.append('"scalafixAll --check"')
        why.append("sbt-scalafix + .scalafix.conf")
    if "sbt-scalafmt" in plugins and (wt / ".scalafmt.conf").exists():
        tasks.append("scalafmtCheckAll")
        why.append("sbt-scalafmt + .scalafmt.conf")
    if _has_tests(wt):
        tasks.append("test")
        why.append("unit test sources")
    if has_it and "flapdoodle" in project:
        it_src = "".join(_read(p) for p in (wt / "it").rglob("*.scala")) if (wt / "it").is_dir() else ""
        if "testcontainers" not in it_src.lower():
            tasks.append("it:test")
            why.append("integration tests on embedded Mongo")
    return tasks, why


def plan_validation(wt: Path, repo: str, cfg: dict) -> dict | None:
    """How to validate a fix in the clone `wt`: {kind, command?, source}. The
    `[autofix.validate]` override wins; an Elm repo (elm.json and package.json) runs the
    Elm gate (`Fix._validate_elm`); an sbt build runs `sbt_tasks`. None for a repo of
    any other type, which escalates."""
    override = (cfg["autofix"].get("validate") or {}).get(repo)
    if override:
        return {"kind": "command", "command": override, "source": "[autofix.validate]"}
    if (wt / "elm.json").exists() and (wt / "package.json").exists():
        return {"kind": "elm", "source": "elm.json",
                "agents": list(cfg["autofix"]["elm_agents"])}
    if (wt / "build.sbt").exists():
        tasks, why = sbt_tasks(wt)
        return {"kind": "command", "command": "sbt -batch " + " ".join(tasks),
                "source": "; ".join(why)}
    return None


def describe_validation(v: dict) -> str:
    if v["kind"] == "elm":
        return f"the Elm gate: npm ci, then the {' and '.join(v['agents'])} agents"
    return f"`{v['command']}` (from {v['source']})"


def agent_definitions(names: list[str], cfg: dict) -> dict:
    """`claude --agents` JSON for the named agents, read from the workspace's
    .claude/agents/ or ~/.claude/agents/. A headless run in a clone outside the
    workspace's tree doesn't load the workspace's project agents on its own. Raises
    ValueError for an agent that isn't found."""
    out = {}
    for name in names:
        path = next((p for p in (settings.workspace(cfg) / ".claude" / "agents" / f"{name}.md",
                                 Path("~/.claude/agents").expanduser() / f"{name}.md")
                     if p.exists()), None)
        if path is None:
            raise ValueError(f"no {name} agent in .claude/agents")
        text = path.read_text()
        parts = text.split("---", 2)
        if len(parts) < 3:
            raise ValueError(f"{path} has no frontmatter")
        meta = {}
        for line in parts[1].strip().splitlines():
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
        spec = {"description": meta.get("description") or name, "prompt": parts[2].strip()}
        if meta.get("tools"):
            spec["tools"] = [t.strip() for t in meta["tools"].split(",") if t.strip()]
        out[meta.get("name") or name] = spec
    return out


# --- impure helpers ------------------------------------------------------------------------


def _fetch_pr(pr: dict, cfg: dict) -> dict | None:
    from watcher import gh
    try:
        return gh.details({pr["id"]: "mine"}, set(cfg["watcher"]["bots"]), gh.token()).get(pr["id"])
    except (gh.GhError, gh.gh_prs.DigestError):
        return None


def worktree_path(pr: dict, cfg: dict) -> Path:
    return settings.path_of(cfg["autofix"]["worktrees_dir"]) / f"{pr['repo']}-{pr['number']}"


def remove_worktree(pr: dict, cfg: dict) -> None:
    root = settings.path_of(cfg["autofix"]["worktrees_dir"]).resolve()
    path = worktree_path(pr, cfg)
    if path.exists() and path.resolve().parent == root:
        shutil.rmtree(path, ignore_errors=True)


def unpushed_checkouts(repo: str, branch: str, cfg: dict, run: Runner = subprocess.run) -> list[str]:
    """Local checkouts of `repo` whose `branch` has commits no origin ref has: your
    clone at <repos_root>/<repo>, and folders named <repo>-* under repos_root and the
    workspace (ticket worktrees). Worktrees of one clone share its branches, so each
    clone is checked once. Read-only: nothing is fetched."""
    roots = []
    for r in (settings.path_of(cfg["repos_root"]), settings.workspace(cfg)):
        if r not in roots:
            roots.append(r)
    found = []
    for root in roots:
        found.append(root / repo)
        found += sorted(root.glob(f"{repo}-*")) if root.is_dir() else []
    out, seen = [], set()
    for path in found:
        if not (path / ".git").exists():
            continue
        common = run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                     cwd=path, capture_output=True, text=True, timeout=60).stdout.strip()
        if not common or common in seen:
            continue
        seen.add(common)
        ref = f"refs/heads/{branch}"
        if run(["git", "rev-parse", "--verify", "--quiet", ref], cwd=path,
               capture_output=True, text=True, timeout=60).returncode != 0:
            continue
        ahead = run(["git", "rev-list", "--count", ref, "--not", "--remotes=origin"], cwd=path,
                    capture_output=True, text=True, timeout=60).stdout.strip()
        if ahead.isdigit() and int(ahead) > 0:
            out.append(f"{path} ({ahead} unpushed)")
    return out


@dataclass
class Tools:
    """Everything a run does outside its own job file."""
    run: Runner = subprocess.run           # git (except push), claude, the validate command
    remote: Runner = subprocess.run        # git push and every GitHub write
    fetch_pr: Callable[[dict, dict], dict | None] = _fetch_pr
    notify_run: Runner = subprocess.run
    alive: Callable[[int | None], bool] = followup._alive
    jobs: Callable[[], list[dict]] = all_jobs


class Stop(Exception):
    def __init__(self, status: str, step: str, reason: str, detail: str = ""):
        super().__init__(reason)
        self.status, self.step, self.reason, self.detail = status, step, reason, detail


def _tail(text: str, n: int = TAIL) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else "…" + text[-n:]


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1][:200] if lines else ""


class Fix:
    """One run of one job. `dry_run` stops before the commit; `persist` False (the
    autofix-run --dry-run command) writes no job file and sends no notification.
    `probe` (that command too) runs past failed preconditions, reporting where a live
    run would stop, and works at the comment's own commit when the PR moved after it,
    so a fix can be tried on a comment that was answered long ago."""

    def __init__(self, job: dict, cfg: dict, tools: Tools | None = None, *,
                 dry_run: bool, persist: bool = True, out: Callable[[str], None] | None = None,
                 probe: bool = False):
        self.job, self.cfg, self.tools = job, cfg, tools or Tools()
        self.dry_run, self.persist, self.out = dry_run, persist, out
        self.probe = probe and dry_run
        self.pr = job["pr"]
        self.org = cfg["org"]
        self.wt = worktree_path(self.pr, cfg)
        self.env = followup._env()
        self.cost = 0.0

    # -- bookkeeping --

    def save(self) -> None:
        if self.persist:
            save_job(self.job)

    def step(self, name: str, ok: bool, detail: str = "") -> None:
        self.job.setdefault("steps", []).append(
            {"step": name, "ok": ok, "at": _iso_now(), "detail": detail[:TAIL]})
        self.save()
        if self.out:
            self.out(f"[{'ok' if ok else 'stop'}] {name}" + (f": {detail}" if detail else ""))

    def escalate(self, step: str, reason: str, detail: str = "") -> None:
        raise Stop("escalated", step, reason, detail)

    # -- commands --

    def git(self, *args: str, check: bool = True, timeout: int = 600,
            cwd: Path | None = None) -> subprocess.CompletedProcess:
        proc = self.tools.run(["git", *args], cwd=str(cwd or self.wt), capture_output=True,
                              text=True, timeout=timeout, env=self.env)
        if check and proc.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed: {_last_line(proc.stderr or proc.stdout)}")
        return proc

    def claude(self, step: str, prompt: str, allowed: list[str], denied: list[str],
               timeout: int, extra: list[str] = ()) -> str:
        model = self.cfg["autofix"].get("model") or self.cfg["models"]["followup"]
        argv = ["claude", "-p", "--model", model, "--output-format", "json",
                "--permission-mode", "dontAsk", "--strict-mcp-config", *extra,
                "--allowedTools", *allowed, "--disallowedTools", *denied]
        try:
            proc = self.tools.run(argv, input=prompt, capture_output=True, text=True,
                                  cwd=str(self.wt), env=self.env, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.escalate(step, f"Claude timed out after {timeout}s")
        try:
            envelope = json.loads(proc.stdout or "{}")
        except ValueError:
            envelope = {}
        self.cost += float(envelope.get("total_cost_usd") or 0)
        self.job["cost_usd"] = round(self.cost, 4)
        denied = [f"{d.get('tool_name')}: {json.dumps(d.get('tool_input'))[:200]}"
                  for d in envelope.get("permission_denials") or [] if isinstance(d, dict)]
        if denied:
            self.job.setdefault("denials", {})[step] = denied
        if envelope.get("is_error") or not envelope.get("result"):
            self.escalate(step, "Claude's run failed",
                          _tail(str(envelope.get("result") or proc.stderr or proc.stdout)))
        return envelope["result"]

    # -- the run --

    def run(self) -> int:
        self.job.update(status="running", started_at=int(time.time()), dry_run=self.dry_run)
        if self.persist:
            self.job["pid"] = os.getpid()
        self.save()
        lock = self._lock()
        try:
            if lock is None:
                self.escalate("preconditions", "another auto-fix is running on this PR")
            self._pipeline()
        except Stop as s:
            return self._finish_stopped(s)
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
            return self._finish_stopped(Stop("escalated", self._current_step(),
                                             f"the auto-fix run failed: {str(exc)[:200]}"))
        finally:
            if lock is not None:
                lock.close()
        return 0

    def _current_step(self) -> str:
        steps = self.job.get("steps") or []
        return steps[-1]["step"] if steps else "preconditions"

    def _lock(self):
        AUTOFIX_DIR.mkdir(parents=True, exist_ok=True)
        f = (AUTOFIX_DIR / f"{self.pr['repo']}-{self.pr['number']}.lock").open("w")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return None
        return f

    def _pipeline(self) -> None:
        job, cfg, af = self.job, self.cfg, self.cfg["autofix"]
        snap = self.tools.fetch_pr(self.pr, cfg)
        action, why = preconditions(job, snap, self.tools.jobs(), cfg, self.tools.alive)
        thread = next((t for t in (snap or {}).get("threads") or []
                       if t["id"] == job["thread_id"] and t["comments"]), None)
        if action != "go" and (not self.probe or thread is None):
            if action == "clear":
                raise Stop("cleared", "preconditions", why)
            self.escalate("preconditions", why)
        job["body"] = thread["comments"][0].get("body") or job.get("body") or ""
        self.branch, self.base = snap["head_ref"], snap["base_ref"]
        unpushed = unpushed_checkouts(self.pr["repo"], self.branch, cfg, self.tools.run)
        if unpushed and not self.probe:
            self.escalate("preconditions", f"you have unpushed commits on {self.branch}",
                          "; ".join(unpushed))
        if self.probe and (action != "go" or unpushed):
            live = why or f"you have unpushed commits on {self.branch}: {'; '.join(unpushed)}"
            self.step("preconditions", False, f"a live run would stop here ({live}); "
                                              "the dry run goes on")
        else:
            self.step("preconditions", True)

        target = None
        if self.probe and job.get("comment_commit") and job["comment_commit"] != snap["head"]:
            target = job["comment_commit"]
        self.start = self._prepare_worktree(snap["head"], target)
        self.step("worktree", True, f"{self.wt} at {self.start[:12]}"
                  + (" (the comment's commit, since the PR moved after it)" if target else ""))

        self.validation = plan_validation(self.wt, self.pr["repo"], cfg)
        if self.validation is None:
            self.escalate("validate-plan", f"can't tell how to validate {self.pr['repo']}: "
                          "no elm.json or build.sbt, and no [autofix.validate] override")
        self.step("validate-plan", True, describe_validation(self.validation))

        a = self._assess(thread)
        job["assessment"] = a
        stop = decide(a, float(af["min_confidence"]))
        if stop:
            self.escalate("assess", stop)
        self.step("assess", True, f"{a['confidence']:.0%} sure: {a['plan']}")

        summary = self._edit(a)
        job["summary"] = summary
        allowed = {job["path"], *safe_files(a["files"])}
        self._scope("scope", allowed)

        self._validate("validate")
        self._scope("scope-after-validate", allowed)
        message = commit_message(job, summary)
        if self.dry_run:
            job["would_commit"] = message
            job["diff"] = self.git("diff", "--cached").stdout[:DIFF_LIMIT]
            raise Stop("dry_run", "commit", "dry run: stopped before the commit")

        self.git("commit", "-q", "-m", message)
        commit_sha = self.git("rev-parse", "HEAD").stdout.strip()
        job["commit_sha"] = commit_sha
        self.step("commit", True, commit_sha[:12])

        if self._merge_base():
            self._validate("validate-after-merge")
        self._push()

        job["reply_url"] = self._reply(summary, commit_sha)
        job["resolved"] = self._resolve()
        job["rerequested"] = self._rerequest()
        self._finish_done()

    def _prepare_worktree(self, head: str, target: str | None = None) -> str:
        """Check out the PR's branch at the remote head, or (a probe) at `target`, the
        comment's commit, reached through the PR's pull ref so a deleted branch still
        works. The clone's push URL is the guard from here until the push step."""
        clone = settings.path_of(self.cfg["repos_root"]) / self.pr["repo"]
        if not (self.wt / ".git").exists():
            remove_worktree(self.pr, self.cfg)
            self.wt.parent.mkdir(parents=True, exist_ok=True)
            url = ""
            if (clone / ".git").exists():
                url = self.git("remote", "get-url", "origin", check=False, cwd=clone).stdout.strip()
            url = url or f"https://github.com/{self.org}/{self.pr['repo']}.git"
            argv = ["clone", "--quiet", "--no-checkout"]
            if (clone / ".git").is_dir():
                argv += ["--reference-if-able", str(clone)]
            self.git(*argv, url, str(self.wt), cwd=self.wt.parent, timeout=1800)
        self.git("config", "remote.origin.pushurl", PUSH_GUARD)
        b, base = self.branch, self.base
        base_ref = f"+refs/heads/{base}:refs/remotes/origin/{base}"
        if self.probe:
            pull = f"refs/remotes/origin/pull-{self.pr['number']}"
            self.git("fetch", "-q", "origin", f"+refs/pull/{self.pr['number']}/head:{pull}",
                     base_ref, timeout=900)
            if target and self.git("cat-file", "-e", f"{target}^{{commit}}",
                                   check=False).returncode != 0:
                self.git("fetch", "-q", "origin", target, timeout=900)
            self.git("checkout", "-q", "-f", "-B", b, target or pull)
        else:
            self.git("fetch", "-q", "origin", f"+refs/heads/{b}:refs/remotes/origin/{b}",
                     base_ref, timeout=900)
            self.git("checkout", "-q", "-f", "-B", b, f"refs/remotes/origin/{b}")
        self.git("clean", "-q", "-fd")
        start = self.git("rev-parse", "HEAD").stdout.strip()
        if start != (target or head):
            self.escalate("worktree", "the PR moved since the comment",
                          f"GitHub says {head[:12]}, the remote branch is {start[:12]}")
        return start

    def _thread_text(self, thread: dict) -> str:
        """The thread's later human comments. A probe shows none: it replays the moment
        the comment arrived, before anyone answered it."""
        if self.probe:
            return ""
        rest = [c for c in thread["comments"][1:] if not c["is_bot"]]
        if not rest:
            return ""
        return "\nLater in the thread:\n" + "\n".join(
            f"- @{c['author']}: {c['body'][:1500]}" for c in rest) + "\n"

    def _values(self, plan: str = "", files: list[str] = ()) -> dict[str, str]:
        job = self.job
        return {"me": self.cfg["me"], "org": self.org, "pr_url": self.pr["url"],
                "comment": job["body"], "path": job["path"],
                "line": str(job.get("line") or "none (a comment on the whole file)"),
                "plan": plan, "reviewer": job["reviewer"], "kind": job["kind"],
                "files": ", ".join(files) or job["path"]}

    def _assess(self, thread: dict) -> dict:
        job = self.job
        prompt = ASSESS_PROMPT.format(
            **self._values(), repo=self.pr["repo"], number=self.pr["number"],
            title=self.pr.get("title") or "", comment_url=job.get("comment_url") or "",
            thread=self._thread_text(thread))
        text = self.claude("assess", prompt, ["Read", "Grep", "Glob"],
                           ["Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch",
                            "Agent"], int(self.cfg["autofix"]["assess_timeout"]))
        try:
            return parse_assessment(text)
        except ValueError as exc:
            self.escalate("assess", f"couldn't read Claude's assessment: {exc}", _tail(text))

    def _template(self) -> str:
        custom = self.cfg["autofix"].get("prompt") or ""
        path = settings.path_of(custom) if custom else PROMPT_FILE
        return path.read_text()

    def _edit(self, a: dict) -> str:
        files = safe_files(a["files"])
        prompt = fill_template(self._template(), self._values(a["plan"], files))
        wt = str(self.wt.resolve())
        text = self.claude("edit", prompt,
                           ["Read", "Grep", "Glob", f"Edit(/{wt}/**)", f"Write(/{wt}/**)"],
                           ["Bash", "NotebookEdit", "WebFetch", "WebSearch", "Agent"],
                           int(self.cfg["autofix"]["edit_timeout"]))
        try:
            result = parse_edit(text)
        except ValueError as exc:
            self.escalate("edit", f"couldn't read Claude's edit result: {exc}", _tail(text))
        if not result["changed"]:
            self.escalate("edit", "Claude didn't make the change"
                          + (f": {result['reason']}" if result["reason"] else ""))
        summary = result["summary"] or a["plan"]
        self.step("edit", True, summary)
        return summary

    def _scope(self, step: str, allowed: set[str]) -> None:
        self.git("add", "-A")
        changed = parse_numstat(self.git("diff", "--cached", "--numstat", "--no-renames").stdout)
        self.job["changed"] = changed
        problem = scope_problem(changed, allowed, int(self.cfg["autofix"]["max_lines"]))
        if problem:
            self.escalate(step, problem, ", ".join(sorted(changed)))
        self.step(step, True, ", ".join(f"{p} ({n})" for p, n in sorted(changed.items())))

    def _validate(self, step: str) -> None:
        started = time.time()
        if self.validation["kind"] == "elm":
            detail = self._validate_elm(step)
        else:
            detail = self._validate_command(step, self.validation["command"])
        took = int(time.time() - started)
        self.job.setdefault("validate_seconds", {})[step] = took
        self.step(step, True, f"{detail} ({took}s)")

    def _sh(self, step: str, cmd: str, timeout: int) -> subprocess.CompletedProcess:
        try:
            return self.tools.run(["/bin/sh", "-c", cmd], cwd=str(self.wt), capture_output=True,
                                  text=True, timeout=timeout, env=self.env)
        except subprocess.TimeoutExpired:
            self.escalate(step, f"`{cmd}` timed out after {timeout}s")

    def _validate_command(self, step: str, cmd: str) -> str:
        proc = self._sh(step, cmd, int(self.cfg["autofix"]["validate_timeout"]))
        output = (proc.stdout or "") + (proc.stderr or "")
        self.job["validate_tail"] = _tail(output)
        if proc.returncode != 0:
            self.escalate(step, f"validation failed (exit {proc.returncode}): {_last_line(output)}",
                          _tail(output))
        return f"`{cmd}` passed"

    def _validate_elm(self, step: str) -> str:
        """npm ci when the clone has no node_modules, then one headless session that runs
        the elm-validation agent and, when it passes, the elm-conventions-review agent.
        A failing gate or any conventions must-fix escalates; only Josh can waive one."""
        timeout = int(self.cfg["autofix"]["validate_timeout"])
        if not (self.wt / "node_modules").is_dir():
            proc = self._sh(step, "npm ci --no-audit --no-fund", timeout)
            if proc.returncode != 0:
                self.escalate(step, "npm ci failed", _tail(proc.stdout + proc.stderr))
        try:
            agents = agent_definitions(self.validation["agents"], self.cfg)
        except (OSError, ValueError) as exc:
            self.escalate(step, f"can't load the Elm gate's agents: {exc}")
        wt = str(self.wt.resolve())
        prompt = ELM_GATE_PROMPT.format(wt=wt, repo=self.pr["repo"], pr_url=self.pr["url"])
        with self._elm_lock():
            text = self.claude(step, prompt,
                               ["Agent", "Read", "Grep", "Glob", f"Edit(/{wt}/**)",
                                f"Write(/{wt}/**)", *ELM_GATE_BASH],
                               [*NEVER_BASH, "NotebookEdit", "WebFetch", "WebSearch"],
                               timeout, extra=["--agents", json.dumps(agents)])
        try:
            gate = parse_elm_gate(text)
        except ValueError as exc:
            self.escalate(step, f"couldn't read the Elm gate's result: {exc}", _tail(text))
        self.job["elm_gate"] = gate
        if gate["validation"] != "pass":
            self.escalate(step, f"the Elm gate failed: {gate['failure'] or gate['summary']}",
                          gate["summary"])
        if gate["conventions"] != "pass":
            self.escalate(step, f"the conventions review found {len(gate['must_fix']) or 'a'} "
                          f"must-fix: {(gate['must_fix'] or [gate['failure']])[0]}",
                          "\n".join(gate["must_fix"]))
        return f"Elm gate passed: {gate['summary']}"

    @contextlib.contextmanager
    def _elm_lock(self):
        """One Elm gate at a time across PRs: its Playwright step serves on :3000."""
        AUTOFIX_DIR.mkdir(parents=True, exist_ok=True)
        with (AUTOFIX_DIR / "elm-gate.lock").open("w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield

    def _merge_base(self) -> bool:
        base = self.base
        self.git("fetch", "-q", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}",
                 timeout=900)
        if self.git("merge-base", "--is-ancestor", f"refs/remotes/origin/{base}", "HEAD",
                    check=False).returncode == 0:
            self.step("merge", True, f"already up to date with {base}")
            return False
        proc = self.git("merge", "--no-edit", f"refs/remotes/origin/{base}", check=False)
        if proc.returncode != 0:
            self.git("merge", "--abort", check=False)
            self.escalate("merge", f"merging {base} into the branch conflicts",
                          _tail(proc.stdout + proc.stderr))
        self.step("merge", True, f"merged {base}")
        return True

    def _push(self) -> None:
        try:
            argv = push_argv(self.branch, self.base)
        except ValueError as exc:
            self.escalate("push", str(exc))
        remote = self.git("ls-remote", "origin", f"refs/heads/{self.branch}").stdout.split()
        if not remote or remote[0] != self.start:
            self.escalate("push", "the branch moved on GitHub during the fix")
        self.git("config", "--unset", "remote.origin.pushurl", check=False)
        try:
            proc = self.tools.remote(argv, cwd=str(self.wt), capture_output=True, text=True,
                                     timeout=600, env=self.env)
        finally:
            self.git("config", "remote.origin.pushurl", PUSH_GUARD, check=False)
        if proc.returncode != 0:
            self.escalate("push", f"the push failed: {_last_line(proc.stderr or proc.stdout)}")
        pushed = self.git("rev-parse", "HEAD").stdout.strip()
        self.job.update(pushed_sha=pushed, pushed_from=self.start, pushed_at=int(time.time()))
        self.step("push", True, f"{self.start[:7]}..{pushed[:7]} to {self.branch}")

    def _reply(self, summary: str, sha: str) -> str:
        text = reply_text(self.job, summary, sha, self.org)
        path = AUTOFIX_DIR / f"{self.job['id']}.reply.md"
        AUTOFIX_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        self.job["reply_text"] = text
        endpoint = followup.reply_endpoint(self.pr["repo"], self.pr["number"],
                                           self.job["comment_id"], self.org)
        proc = self.tools.remote(["gh", "api", "-X", "POST", endpoint, "-F", f"body=@{path}",
                                  "--jq", ".html_url"], capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            self.escalate("reply", f"pushed {sha[:7]}, but the reply didn't post: "
                          f"{_last_line(proc.stderr)}")
        url = proc.stdout.strip()
        self.step("reply", True, url)
        return url

    def _resolve(self) -> bool:
        proc = self.tools.remote(["gh", "api", "graphql", "-f", f"query={RESOLVE_QUERY}",
                                  "-f", f"threadId={self.job['thread_id']}",
                                  "--jq", ".data.resolveReviewThread.thread.isResolved"],
                                 capture_output=True, text=True, timeout=120)
        ok = proc.returncode == 0 and proc.stdout.strip() == "true"
        self.step("resolve", ok, "" if ok else _last_line(proc.stderr) or "not resolved")
        return ok

    def _rerequest(self) -> bool:
        reviewer = self.job["reviewer"]
        snap = self.tools.fetch_pr(self.pr, self.cfg)
        if snap is None:
            self.step("rerequest", False, "couldn't read the PR to check")
            return False
        go, why = rerequest_plan(snap, reviewer, self.cfg["me"], self.tools.jobs(),
                                 self.job["id"])
        if not go:
            self.step("rerequest", True, f"not yet: {why}")
            return False
        proc = self.tools.remote(["gh", "api", "-X", "POST",
                                  f"repos/{self.org}/{self.pr['repo']}/pulls/{self.pr['number']}"
                                  f"/requested_reviewers", "-f", f"reviewers[]={reviewer}"],
                                 capture_output=True, text=True, timeout=120)
        ok = proc.returncode == 0
        self.step("rerequest", ok, f"re-requested {reviewer}" if ok else _last_line(proc.stderr))
        return ok

    # -- endings --

    def _who(self) -> str:
        try:
            import names
            return names.book().get(self.job["reviewer"], self.job["reviewer"])
        except Exception:  # noqa: BLE001 — a name is a nicety
            return self.job["reviewer"]

    def _finish_done(self) -> None:
        job = self.job
        job.update(status="done", done_at=int(time.time()), finished_at=int(time.time()))
        self.save()
        if not self.persist:
            return
        sha = (job.get("commit_sha") or "")[:7]
        body = f"{self._who()}'s {job['kind']} · pushed {sha}, replied"
        body += ", resolved" if job.get("resolved") else ", not resolved"
        notify.banner("✅ Fixed for you", render.subtitle(self.pr), body,
                      href=job.get("reply_url") or self.pr["url"], group=f"autofix-{job['id']}",
                      run=self.tools.notify_run)
        opts = notify.sound_options(self.cfg, "done")
        if opts.get("sound_file"):
            notify.play(opts["sound_file"], self.tools.notify_run)

    def _finish_stopped(self, s: Stop) -> int:
        job = self.job
        self.step(s.step, s.status != "escalated", s.reason)
        job.update(status=s.status, reason=s.reason, detail=s.detail[:TAIL], stopped_at=s.step,
                   finished_at=int(time.time()))
        if s.status == "escalated":
            job["escalated_at"] = _iso_now()
        if s.status == "cleared":
            job["cleared"] = s.reason
        self.save()
        if s.status == "escalated" and self.persist and not self.dry_run:
            self._alert(s.reason)
        return 1 if s.status == "escalated" else 0

    def _alert(self, reason: str) -> None:
        import session
        from triage import priority
        key = priority.ticket(self.pr.get("title") or "", self.pr["url"])
        notify.alert(f"🔴 Couldn't auto-fix {self._who()}'s {self.job['kind']} on {key}",
                     render.subtitle(self.pr), reason, href=session.respond_url(self.pr["url"]),
                     plugin=self.cfg["notify"]["swiftbar_plugin"], group=f"autofix-{self.job['id']}",
                     run=self.tools.notify_run, **notify.sound_options(self.cfg, "red"))


# --- starting runs ---------------------------------------------------------------------


def start_queued(cfg: dict, spawn: Callable[[str], int] | None = None,
                 logged_in: Callable[[], bool | None] = followup.claude_logged_in) -> tuple[int, bool]:
    """Start queued fixes, one per PR at a time and up to watcher.max_concurrent in
    all. A running job whose process died escalates. Returns (started, claude logged
    out). Starts nothing while `[autofix] enabled` is off."""
    jobs = all_jobs()
    for job in jobs:
        if job["status"] == "running" and not followup._alive(job.get("pid")):
            job.update(status="escalated", reason="the auto-fix process died",
                       escalated_at=_iso_now(), finished_at=int(time.time()))
            save_job(job)
    if not cfg["autofix"]["enabled"]:
        return 0, False
    busy = {j["pr"]["id"] for j in jobs if j["status"] == "running"}
    room = cfg["watcher"]["max_concurrent"] - len(busy)
    queued = sorted((j for j in jobs if j["status"] == "queued"),
                    key=lambda j: j.get("created_at") or "")
    if not queued or room <= 0:
        return 0, False
    if logged_in() is False:
        return 0, True
    spawn = spawn or _spawn
    started = 0
    for job in queued:
        if started >= room or job["pr"]["id"] in busy:
            continue
        pid = spawn(job["id"])
        job.update(status="running", pid=pid, started_at=int(time.time()))
        save_job(job)
        busy.add(job["pr"]["id"])
        started += 1
    return started, False


def _spawn(jid: str) -> int:
    proc = subprocess.Popen(
        [sys.executable, "-m", "watcher", "autofix-run", jid],
        cwd=settings.REPO_DIR, start_new_session=True, env=followup._env(),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.pid


# --- the command ------------------------------------------------------------------------

_COMMENT_URL = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+).*#discussion_r(\d+)")


def job_from_url(url: str, cfg: dict, tools: Tools) -> dict:
    """A job (not saved) for the thread holding the review comment at `url`."""
    m = _COMMENT_URL.search(url)
    if not m:
        raise ValueError("expected a review comment link, …/pull/<n>#discussion_r<id>")
    org, repo, number, cid = m.group(1), m.group(2), int(m.group(3)), int(m.group(4))
    proc = tools.run(["gh", "api", f"repos/{org}/{repo}/pulls/{number}", "--jq", ".node_id"],
                     capture_output=True, text=True, timeout=60)
    node = proc.stdout.strip()
    if proc.returncode != 0 or not node:
        raise ValueError(f"couldn't read {org}/{repo}#{number}: {_last_line(proc.stderr)}")
    snap = tools.fetch_pr({"id": node}, cfg)
    if snap is None:
        raise ValueError(f"couldn't read {org}/{repo}#{number}")
    t = next((t for t in snap["threads"] if any(c["id"] == cid for c in t["comments"])), None)
    if t is None or not t["comments"]:
        raise ValueError(f"no review thread holds comment {cid}")
    c = t["comments"][0]
    opened = {"thread_id": t["id"], "comment_id": c["id"], "author": c["author"],
              "body": c["body"], "url": c.get("url"), "path": t.get("path"),
              "line": t.get("line") or t.get("original_line"), "commit": c.get("commit"),
              "original_commit": c.get("original_commit"),
              "kind": comment_kind.kind(c["body"])}
    return new_job(snap, opened, cfg)


def run_command(target: str, cfg: dict, dry_run: bool, tools: Tools | None = None,
                out: Callable[[str], None] = print) -> int:
    """`python -m watcher autofix-run <job | comment link> [--dry-run]`.

    With --dry-run it runs preconditions, worktree, assess, edit, scope and validate
    for real, prints each step and the diff it would commit, and stops: no job file
    changes, no commit, no push, no post, no notification. A comment link works there
    too, so a fix can be tried while `[autofix] enabled` is off. Without --dry-run it
    is the run the watcher starts for a queued job, and refuses while the feature is
    off."""
    tools = tools or Tools()
    if not dry_run and not cfg["autofix"]["enabled"]:
        out("[autofix] enabled is off; nothing runs. Use --dry-run to try a fix.")
        return 2
    try:
        job = (job_from_url(target, cfg, tools) if "://" in target else load_job(target))
    except (OSError, ValueError) as exc:
        out(f"autofix-run: {exc}")
        return 2
    if not dry_run:
        fix = Fix(job, cfg, tools, dry_run=bool(cfg["autofix"]["dry_run"] or job.get("dry_run")))
        return fix.run()

    job = json.loads(json.dumps(job))
    job["steps"] = []
    out(f"Dry run of the auto-fix for {job['reviewer']}'s {job['kind']} on "
        f"{job['pr']['repo']}#{job['pr']['number']} ({job.get('comment_url') or job['thread_id']})")
    fix = Fix(job, cfg, tools, dry_run=True, persist=False, out=out, probe=True)
    rc = fix.run()
    out(f"\nStopped: {job['status']}: {job.get('reason') or ''}")
    if job.get("detail"):
        out(job["detail"])
    if job.get("assessment"):
        out("\nAssessment:\n" + json.dumps(job["assessment"], indent=2))
    if job.get("elm_gate"):
        out("\nElm gate:\n" + json.dumps(job["elm_gate"], indent=2))
    if job.get("validate_tail"):
        out("\nValidation output (tail):\n" + job["validate_tail"])
    for step, denied in (job.get("denials") or {}).items():
        out(f"\nTool calls refused during {step}:\n" + "\n".join(f"- {d}" for d in denied))
    if job.get("changed") and not job.get("would_commit") and (fix.wt / ".git").exists():
        out("\nDiff left in the clone:\n" + fix.git("diff", "HEAD", check=False).stdout[:DIFF_LIMIT])
    if job.get("would_commit"):
        out("\nWould commit:\n" + job["would_commit"])
        out("Diff:\n" + (job.get("diff") or ""))
        out(f"Then: merge origin/{fix.base} if behind (validating again), "
            f"`{' '.join(push_argv(fix.branch, fix.base))}`, reply in the thread "
            f"(\"{reply_text(job, job['summary'], 'abcdef0', cfg['org']).splitlines()[0]}\" with the "
            f"real sha), resolve it, and re-request {job['reviewer']} once all their threads "
            f"are answered.")
    if fix.cost:
        out(f"Claude cost: ${fix.cost:.2f}")
    return rc
