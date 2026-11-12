"""Follow-ups: when someone responds to your review comment or to your change request,
check the new commits; when someone @mentions you, draft your answer; when a reviewer
comments on your own PR, draft your reply.

A job is one JSON file in state/watcher/jobs/<job_id>.json. A response's id is derived
from (thread id, your comment id), a mention's from the mentioning item, so the same
response or mention never queues twice. A mention job has `trigger` "mention" and
`where` (issue comment, review body, or inline comment); its reply posts as a reply in
the inline thread, or as a top-level comment otherwise.

A change-request job (`trigger` "change_request") is the author's response to your
standing change request outside any thread (watcher/events.py `_diff_change_request`):
real commits since it, a top-level comment, or a review body. Its id comes from your
change request and the responses it carries, so a later response queues a fresh job,
and that newer job replaces the older one (`cleared_jobs`): one verdict at a time,
always over everything since your last touch. Your change request's body is "your
comment"; Claude judges the commits and the author's words against it, like a
thread's follow-up. It never re-approves (your latest decision isn't an approval).

An answer job (`trigger` "answer") is a comment waiting on you on your own PR: an open
thread whose last word isn't yours, a top-level comment, or a change request's body
(`answer_asks`, from the triage/mine.py row). Its id comes from the comment that put
it on you, so a new reply in the same thread queues a fresh one. Claude reads the PR's
code and drafts the reply with a `stance`: answer (a question, answered from the
code), agree (a change worth making, said as a plan, never as done), push_back (with
the reason from the code), or needs_you (it needs your call; `note` says what). A
comment an auto-fix is working on gets no answer job. It clears once the comment is no
longer waiting on you (`stale_answers`).

  queued  -> running (detached `python -m watcher followup-run <job>`) -> ready | failed
  ready   -> posted (you clicked "Post this reply") | dismissed
  queued, ready or failed -> cleared, when you answer the thread yourself: you reply
          in it, resolve it, or submit a new review (`cleared_jobs`, run each tick);
          for a change-request job, when you submit a new review, leave a top-level
          comment, your change request is dismissed, or a newer response replaces it;
          or when the PR merges or closes (`cancel_for_pr`, `orphaned_prs`)

A finished run doesn't pop up a notification: the watcher already sent the PR's one
notification when the response arrived, and the menu row updates with the verdict.

The run collects only the commits since your comment, asks claude (models.followup)
for a verdict and a drafted reply, and writes the report itself. claude gets read-only
tools scoped to the repo clone and cannot post: posting happens only in post(), which
the menu bar calls when you click, and which posts exactly the reply file's text.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import notify  # noqa: E402
import settings  # noqa: E402
from watcher import render  # noqa: E402
from watcher.state import JOBS_DIR  # noqa: E402

FOOTER = "Drafted with Claude 🤖"
DIFF_LIMIT = 60_000
VERDICTS = ("addressed", "partly", "not_addressed")

CR_PROMPT = """@{me} requested changes on a coworker's pull request. Since then, {trigger_text}.
Decide whether the author has addressed the change request, and draft @{me}'s reply.

PR: avetta/{repo}#{number} — {title}
PR author: @{author}
@{me}'s change request ({comment_url}):
---
{my_comment}
---
{inline}
The author's words since the change request, in order:
{responses}

Commits since @{me}'s last review or comment ({arm_short}..{head_short}){rebased_note}:
{commits}

Files changed over that range:
{stat}

Diff over that range (may be cut off):
```diff
{diff}
```

The clone is at {clone}. You may read more with `git -C {clone} show|log|diff` at
these commits. Judge only against what @{me}'s change request asked for. Don't review
anything else. When the author answered in words instead of code, judge whether their
answer settles what was asked.

Reply rules: address the PR author by handle, plain English, at most 3 sentences. Cite the
commit (short sha) that fixed it. If it's not addressed, say concretely what is still missing.
No bare acceptance-criteria labels. Don't add a signature.

"mechanical" is true only when the commits do exactly what @{me}'s change request asked,
as it was asked: no pushback, no different approach, nothing asked left open.

Output ONLY a JSON object, no prose, no code fences:
{{"verdict": "addressed" | "partly" | "not_addressed",
  "mechanical": true | false,
  "evidence": [{{"file": "...", "line": 0, "sha": "<full or short sha of the fixing commit>", "note": "..."}}],
  "reply": "..."}}
"""


PROMPT = """@{me} left a review comment on a coworker's pull request. Since then, {trigger_text}.
Decide whether the commits since that comment address it, and draft @{me}'s reply.

PR: avetta/{repo}#{number} — {title}
PR author: @{author}
File: {path}{line_suffix}
@{me}'s comment ({comment_url}):
---
{my_comment}
---

The thread, in order:
{thread}

Commits since @{me}'s comment ({arm_short}..{head_short}){rebased_note}:
{commits}

Diff of {path} over that range:
```diff
{file_diff}
```

Files changed over that range:
{stat}

The clone is at {clone}. You may read more with `git -C {clone} show|log|diff` at
these commits. Judge only against what @{me}'s comment asked for. Don't review anything else.

Reply rules: address the PR author by handle, plain English, at most 3 sentences. Cite the
commit (short sha) that fixed it. If it's not addressed, say concretely what is still missing.
No bare acceptance-criteria labels. Don't add a signature.

"mechanical" is true only when the commits do exactly what @{me}'s comment asked, as it was
asked: no pushback, no different approach, nothing asked left open. Explaining a separate
question from someone else in the thread doesn't make it false.

Output ONLY a JSON object, no prose, no code fences:
{{"verdict": "addressed" | "partly" | "not_addressed",
  "mechanical": true | false,
  "evidence": [{{"file": "...", "line": 0, "sha": "<full or short sha of the fixing commit>", "note": "..."}}],
  "reply": "..."}}
"""


MENTION_PROMPT = """@{actor} mentioned @{me} on a pull request. Draft @{me}'s answer.

PR: avetta/{repo}#{number} — {title}
PR author: @{author}
Where: {where}
Their comment ({url}):
---
{body}
---
{thread}
{clone_text}

Reply rules: address @{actor} by handle, plain English, at most 4 sentences, and answer
what they asked. Ground every claim in the code or the PR; when the answer needs @{me}'s
own decision or knowledge you can't find, say in the reply that they'll follow up, and
never invent facts. No bare acceptance-criteria labels. Don't add a signature.

Output ONLY a JSON object, no prose, no code fences:
{{"summary": "<one line: what they asked or told @{me}>", "reply": "..."}}
"""


ANSWER_PROMPT = """@{actor} left a {kind} on @{me}'s own pull request. Draft @{me}'s reply.

PR: avetta/{repo}#{number} — {title}
Where: {where}
Their comment ({url}):
---
{body}
---
{thread}
{clone_text}

First decide what the reply does (its "stance"):
- "answer": they asked a question or want an explanation. Answer it from the code.
- "agree": they asked for a change that is worth making. Say briefly what will change.
  Nothing has been pushed yet, so never say or imply that it's done or fixed.
- "push_back": the change isn't warranted. Say why, grounded in the code, politely.
- "needs_you": the reply needs @{me}'s own decision or knowledge you can't find in the
  code. The reply says @{me} will follow up; "note" says what @{me} has to decide.

Reply rules: address @{actor} by handle, plain English, at most 4 sentences. Ground every
claim in the code or the PR and never invent facts. No bare acceptance-criteria labels.
Don't add a signature.

Output ONLY a JSON object, no prose, no code fences:
{{"stance": "answer" | "agree" | "push_back" | "needs_you",
  "summary": "<one line: what they asked or told @{me}>",
  "note": "<one line for @{me}: why this stance, or what to decide>",
  "reply": "..."}}
"""

STANCES = ("answer", "agree", "push_back", "needs_you")
# What each stance means, in the menu and on the page of drafted replies.
STANCE_WORDS = {"answer": "answers it", "agree": "agrees to change it",
                "push_back": "pushes back", "needs_you": "needs your call"}


def job_id(thread_id: str, arm_id: int) -> str:
    return hashlib.sha1(f"{thread_id}:{arm_id}".encode()).hexdigest()[:12]


def mention_job_id(where: str, comment_id: int) -> str:
    return hashlib.sha1(f"mention:{where}:{comment_id}".encode()).hexdigest()[:12]


def cr_job_id(review_id, response_key: str) -> str:
    """A change-request job's id, from your change request and the responses it
    carries."""
    return hashlib.sha1(f"cr:{review_id}:{response_key}".encode()).hexdigest()[:12]


def answer_job_id(ask: dict) -> str:
    """An answer job's id, from the comment that put it on you (`ask["key"]`)."""
    return hashlib.sha1(f"answer:{ask['key']}".encode()).hexdigest()[:12]


def answer_asks(row: dict) -> list[dict]:
    """Every comment waiting on you on one of your PRs (a triage/mine.py row), as the
    asks an answer job drafts a reply to: {key, where, kind, actor, body, url,
    thread_id, path, thread}. A thread's key is its id and the time of the comment
    that put it on you; a top-level comment's and a change request's is its url."""
    from triage import comment_kind
    out = []
    for t in row.get("threads_waiting") or []:
        # The comment to answer is the thread's last, which put it back on you; the
        # whole thread goes along for context.
        bodies = t.get("bodies") or []
        body = bodies[-1]["body"] if bodies else ""
        out.append({"key": f"{t.get('thread_id')}@{t.get('last_at')}", "where": "inline",
                    "kind": t.get("kind") or comment_kind.kind(body),
                    "actor": t.get("last_author") or t.get("author") or "",
                    "body": body,
                    "url": t.get("url") or "", "thread_id": t.get("thread_id") or "",
                    "path": t.get("path") or "", "thread": bodies})
    for c in row.get("unanswered_comments") or []:
        out.append({"key": c.get("url") or f"{c.get('author')}@{c.get('at')}", "where": "issue",
                    "kind": c.get("kind") or comment_kind.kind(c.get("body") or ""),
                    "actor": c.get("author") or "", "body": c.get("body") or "",
                    "url": c.get("url") or "", "thread_id": "", "path": "", "thread": []})
    for b in row.get("blockers") or []:
        if not (b.get("body") or "").strip():
            continue
        out.append({"key": b.get("url") or f"cr:{b.get('author')}@{b.get('at')}", "where": "review",
                    "kind": "blocking", "actor": b.get("author") or "", "body": b.get("body") or "",
                    "url": b.get("url") or "", "thread_id": "", "path": "", "thread": []})
    return out


def answers_to_queue(pr: dict, asks: list[dict], jobs: list[dict],
                     fixing_threads: set[str]) -> list[dict]:
    """The asks on one of your PRs that need an answer job: none queued for it yet, and
    no auto-fix working on its thread (`fixing_threads`). Pure."""
    known = {j["id"] for j in jobs}
    return [a for a in asks if answer_job_id(a) not in known
            and not (a["thread_id"] and a["thread_id"] in fixing_threads)]


def stale_answers(pr_id: str, asks: list[dict], jobs: list[dict]) -> list[tuple[str, str]]:
    """(job id, why) for the open answer jobs on a PR whose comment is no longer
    waiting on you (`asks` is what still is): you replied, resolved the thread, or
    re-requested the reviewer. Pure."""
    live = {answer_job_id(a) for a in asks}
    return [(j["id"], "you answered it") for j in jobs
            if j.get("trigger") == "answer" and (j.get("pr") or {}).get("id") == pr_id
            and j.get("status") in CLEARABLE and j["id"] not in live]


def answer_job(pr: dict, ask: dict, root_id: int | None, now: str) -> dict:
    """A queued answer job. `pr` is the watcher snapshot's PR ref; `root_id` the
    thread's first comment, which an inline reply posts under."""
    return {"id": answer_job_id(ask), "status": "queued", "trigger": "answer", "created_at": now,
            **{k: ask.get(k) for k in ("where", "kind", "actor", "body", "url", "thread_id",
                                       "path", "thread")},
            "root_id": root_id if ask["where"] == "inline" else None,
            "head": pr.get("head"), "my_comment": "", "blocking": ask["kind"] == "blocking",
            "pr": {k: pr.get(k) for k in ("id", "repo", "number", "title", "url", "author", "head")}}


def enqueue_answer(job: dict) -> str | None:
    if job_path(job["id"]).exists():
        return None
    save_job(job)
    return job["id"]


def event_job_id(event: dict) -> str:
    """The id of the job an event queues."""
    if event["type"] == "mention":
        return mention_job_id(event["where"], event["comment_id"])
    if event["type"] == "change_request":
        return cr_job_id(event["review_id"], event["response_key"])
    return job_id(event["thread_id"], event["arm_id"])


def job_path(jid: str) -> Path:
    return JOBS_DIR / f"{jid}.json"


def load_job(jid: str) -> dict:
    return json.loads(job_path(jid).read_text())


def save_job(job: dict) -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = job_path(job["id"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(job, indent=2) + "\n")
    os.replace(tmp, job_path(job["id"]))


def all_jobs() -> list[dict]:
    """Every job file, `<job id>.json`. Other files in the folder, such as a
    re-approval's review payload (`<job id>.reapprove.json`), aren't jobs."""
    if not JOBS_DIR.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(JOBS_DIR.glob("*.json"))
            if "." not in p.stem]


def enqueue(event: dict) -> str | None:
    """Queue a follow-up for a thread event; None when this response already has one."""
    jid = job_id(event["thread_id"], event["arm_id"])
    if job_path(jid).exists():
        return None
    save_job({
        "id": jid, "status": "queued",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **{k: event[k] for k in ("trigger", "actor", "thread_id", "root_id", "arm_id",
                                 "arm_commit", "head", "path", "line", "my_comment",
                                 "my_comment_url", "thread", "url")},
        "blocking": bool(event.get("blocking")),
        "pr": event["pr"],
    })
    return jid


def enqueue_mention(event: dict) -> str | None:
    """Queue a drafted answer to an @mention; None when this one already has one."""
    jid = mention_job_id(event["where"], event["comment_id"])
    if job_path(jid).exists():
        return None
    save_job({
        "id": jid, "status": "queued", "trigger": "mention",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **{k: event.get(k) for k in ("actor", "where", "comment_id", "thread_id", "root_id",
                                     "path", "line", "body", "thread", "url",
                                     "mentioned_at", "head")},
        "my_comment": "", "blocking": False, "pr": event["pr"],
    })
    return jid


def enqueue_change_request(event: dict) -> str | None:
    """Queue a follow-up for the author's response to your change request; None when
    this response already has one."""
    jid = event_job_id(event)
    if job_path(jid).exists():
        return None
    save_job({
        "id": jid, "status": "queued", "trigger": "change_request",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **{k: event.get(k) for k in ("actor", "triggers", "review_id", "response_key",
                                     "arm_commit", "head", "since", "my_comment",
                                     "my_comment_url", "my_inline", "responses", "url")},
        "thread_id": None, "root_id": None, "blocking": True, "pr": event["pr"],
    })
    return jid


CLEARABLE = ("queued", "ready", "failed")


def _cr_cleared(job: dict, pr: dict, jobs: list[dict], me: str, reviewed: bool) -> str:
    """Why a change-request job no longer stands, '' while it does."""
    since = job.get("created_at") or ""
    if reviewed:
        return "you submitted a new review"
    decisions = [r for r in sorted(pr["reviews"], key=lambda r: r.get("submitted_at") or "")
                 if r["author"] == me and r["state"] in ("APPROVED", "CHANGES_REQUESTED",
                                                         "DISMISSED")]
    if not decisions or decisions[-1]["state"] != "CHANGES_REQUESTED":
        return "your change request was dismissed"
    if any(c["author"] == me and (c.get("created_at") or "") > since
           for c in pr["issue_comments"]):
        return "you commented on the PR"
    if any(j is not job and j.get("trigger") == "change_request"
           and (j.get("pr") or {}).get("id") == pr["id"]
           and j.get("review_id") == job.get("review_id")
           and (j.get("created_at") or "") > since for j in jobs):
        return "a newer response replaced it"
    return ""


def cleared_jobs(pr: dict, jobs: list[dict], me: str) -> list[tuple[str, str]]:
    """(job id, why) for each of the PR's open jobs that you have answered yourself since
    the response: you replied in the thread or resolved it, or submitted a review that
    approves, requests changes, or has a body after the job was queued. Pure; `pr` is a
    watcher snapshot (watcher/gh.py normalize_pr). A running job is left for the next
    tick, so its run doesn't overwrite the status."""
    threads = {t["id"]: t for t in pr["threads"]}
    out = []
    for job in jobs:
        if job.get("status") not in CLEARABLE or (job.get("pr") or {}).get("id") != pr["id"]:
            continue
        since = job.get("created_at") or ""
        reviewed = any(r["author"] == me and (r.get("submitted_at") or "") > since
                       and (r["state"] in ("APPROVED", "CHANGES_REQUESTED")
                            or (r["state"] == "COMMENTED" and (r.get("body") or "").strip()))
                       for r in pr["reviews"])
        if job.get("trigger") == "change_request":
            why = _cr_cleared(job, pr, jobs, me, reviewed)
            if why:
                out.append((job["id"], why))
            continue
        t = threads.get(job.get("thread_id"))
        if job.get("trigger") == "mention":
            after = job.get("mentioned_at") or since
            answered = (any(c["author"] == me and c["id"] > job.get("comment_id", 0)
                            for c in (t or {}).get("comments", [])) if t else
                        any(c["author"] == me and (c.get("created_at") or "") > after
                            for c in pr["issue_comments"])
                        or any(r["author"] == me and (r.get("submitted_at") or "") > after
                               and (r.get("body") or "").strip() for r in pr["reviews"]))
            if answered or reviewed:
                out.append((job["id"], "you answered the mention"))
            continue
        if reviewed:
            out.append((job["id"], "you submitted a new review"))
        elif t and t["resolved"]:
            out.append((job["id"], "the thread is resolved"))
        elif t and any(c["author"] == me and c["id"] > job.get("arm_id", 0)
                       for c in t["comments"]):
            out.append((job["id"], "you replied in the thread"))
    return out


def clear(jid: str, why: str) -> None:
    job = load_job(jid)
    job.update(status="cleared", cleared=why)
    save_job(job)


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _env() -> dict:
    env = dict(os.environ)
    extra = [str(Path("~/.local/bin").expanduser()), "/opt/homebrew/bin", "/usr/local/bin"]
    env["PATH"] = ":".join(extra + [env.get("PATH", "/usr/bin:/bin")])
    return env


def claude_logged_in() -> bool | None:
    try:
        out = subprocess.run(["claude", "auth", "status", "--json"], capture_output=True,
                             text=True, timeout=30, env=_env()).stdout
        value = json.loads(out).get("loggedIn")
        return value if isinstance(value, bool) else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def start_queued(cfg: dict) -> tuple[int, bool]:
    """Start every queued job. Returns (started, claude_logged_out)."""
    jobs = all_jobs()
    for job in jobs:   # a run that died (crash, reboot) without finishing
        if job["status"] == "running" and not _alive(job.get("pid")):
            job.update(status="failed", error="the follow-up process died")
            save_job(job)
    queued = [j for j in jobs if j["status"] == "queued"]
    if not queued:
        return 0, False
    if claude_logged_in() is False:
        return 0, True
    started = 0
    for job in queued:
        proc = subprocess.Popen(
            [sys.executable, "-m", "watcher", "followup-run", job["id"]],
            cwd=settings.REPO_DIR, start_new_session=True, env=_env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        job.update(status="running", pid=proc.pid, started_at=int(time.time()))
        save_job(job)
        started += 1
    return started, False


# --- the run -----------------------------------------------------------------------


def _git(clone: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(clone), *args], capture_output=True, text=True,
                          timeout=300, check=check)


def collect_range(clone: Path, number: int, arm: str, head: str, path: str | None) -> dict:
    """Commits and diffs between your comment's commit and the current head. With no
    `path`, `file_diff` is the whole diff."""
    _git(clone, "fetch", "-q", "--no-write-fetch-head", "origin", f"pull/{number}/head")
    if _git(clone, "cat-file", "-e", f"{arm}^{{commit}}").returncode != 0:
        _git(clone, "fetch", "-q", "--no-write-fetch-head", "origin", arm)
    rebased = _git(clone, "merge-base", "--is-ancestor", arm, head).returncode != 0
    if rebased:
        commits = "(the branch was rebased or force-pushed since the comment; showing the tree diff)"
    else:
        commits = _git(clone, "log", "--first-parent", "--format=%h %an: %s", f"{arm}..{head}").stdout.strip()
    file_diff = _git(clone, "diff", arm, head, *(["--", path] if path else [])).stdout
    stat = _git(clone, "diff", "--stat", arm, head).stdout.strip()
    return {"rebased": rebased, "commits": commits or "(none)",
            "file_diff": file_diff[:DIFF_LIMIT] or "(no change to this file)",
            "stat": stat[-4000:] or "(no files changed)"}


def build_prompt(job: dict, rng: dict, clone: Path, me: str) -> str:
    pr = job["pr"]
    trigger_text = {
        "reply": f"@{job['actor']} replied in the thread",
        "outdated": "a commit changed the lines the comment was on",
        "moved": "commits landed that touch the file the comment was on",
    }[job["trigger"]]
    thread = "\n".join(f"- @{c['author']}: {c['body'][:1500]}" for c in job["thread"])
    return PROMPT.format(
        me=me, trigger_text=trigger_text, repo=pr["repo"], number=pr["number"], title=pr["title"],
        author=pr["author"], path=job["path"],
        line_suffix=f":{job['line']}" if job.get("line") else "",
        comment_url=job.get("my_comment_url") or "", my_comment=job["my_comment"],
        thread=thread, arm_short=job["arm_commit"][:8], head_short=job["head"][:8],
        rebased_note=" — rebased, tree diff" if rng["rebased"] else "",
        commits=rng["commits"], file_diff=rng["file_diff"], stat=rng["stat"], clone=clone,
    )


CR_TRIGGER_WORDS = {"pushed": "pushed commits", "commented": "commented on the PR",
                    "review": "answered in a review"}


def build_cr_prompt(job: dict, rng: dict, clone: Path, me: str) -> str:
    pr = job["pr"]
    did = [CR_TRIGGER_WORDS[t] for t in job.get("triggers") or [] if t in CR_TRIGGER_WORDS]
    trigger_text = f"@{job['actor']} " + (" and ".join(did) or "responded")
    inline = ""
    if job.get("my_inline"):
        inline = "Its inline comments:\n" + "\n".join(
            f"- {c.get('path')}" + (f":{c['line']}" if c.get("line") else "")
            + f": {(c.get('body') or '')[:800]}" for c in job["my_inline"]) + "\n"
    said = [r for r in job.get("responses") or [] if r.get("trigger") != "pushed"]
    responses = "\n".join(f"- ({r.get('trigger')}, {r.get('url') or ''}) {(r.get('body') or '')[:2000]}"
                          for r in said) or "(none; the author only pushed commits)"
    return CR_PROMPT.format(
        me=me, trigger_text=trigger_text, repo=pr["repo"], number=pr["number"],
        title=pr["title"], author=pr["author"], comment_url=job.get("my_comment_url") or "",
        my_comment=job.get("my_comment") or "(no body; see the inline comments)",
        inline=inline, responses=responses, arm_short=(job.get("arm_commit") or "")[:8],
        head_short=(job.get("head") or "")[:8],
        rebased_note=" — rebased, tree diff" if rng["rebased"] else "",
        commits=rng["commits"], stat=rng["stat"],
        diff=rng["file_diff"].replace("(no change to this file)", "(no changes)"), clone=clone)


def parse_result(text: str) -> dict:
    """The model's JSON verdict; raises ValueError when it isn't one."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the result")
    data = json.loads(text[start:end + 1])
    if data.get("verdict") not in VERDICTS:
        raise ValueError(f"bad verdict: {data.get('verdict')!r}")
    if not str(data.get("reply") or "").strip():
        raise ValueError("empty reply")
    data["evidence"] = [e for e in data.get("evidence") or [] if isinstance(e, dict)]
    data["mechanical"] = data.get("mechanical") is True
    return data


# --- automatic re-approval ------------------------------------------------------------


def reapproval_body(repo: str, shas: list[str]) -> str:
    links = [f"[{s[:7]}](https://github.com/avetta/{repo}/commit/{s})" for s in shas]
    if len(links) == 1:
        lead = f"the author's commit {links[0]} addresses"
    else:
        lead = f"the author's commits {', '.join(links[:-1])} and {links[-1]} address"
    return f"Reapproving, {lead} my comment.\n\n{FOOTER}"


DONE_JOBS = ("ready", "reapproved", "posted")


def _addressed(job: dict) -> bool:
    return (job.get("status") in DONE_JOBS and job.get("verdict") == "addressed"
            and job.get("mechanical") is True)


def _names(logins: list[str]) -> str:
    logins = sorted(set(logins))
    return logins[0] if len(logins) == 1 else f"{', '.join(logins[:-1])} and {logins[-1]}"


def reapproval_blockers(snap: dict, job: dict, jobs: list[dict], me: str) -> list[str]:
    """Why the PR can't be re-approved for you yet; empty when it can. Pure: `snap` is
    a fresh watcher snapshot (watcher/gh.py normalize_pr), `jobs` every follow-up job.

    All of these must hold:
    - the PR is open and its head is the one the follow-up checked;
    - your latest decision is an approval given on an older commit;
    - every unresolved thread with a comment of yours has a follow-up, armed on your
      latest comment there, that found the author did exactly what you asked;
    - no other reviewer has a change request standing that hasn't been re-requested
      since, and no unresolved thread holds another reviewer's blocking comment with
      someone other than the author having the last word.
    """
    from triage import comment_kind, priority

    author = snap["author"]
    if snap.get("state") != "OPEN" or snap.get("head") != job["head"]:
        return ["the PR moved or closed after the follow-up ran"]
    decisions = [r for r in snap["reviews"] if r["author"] == me
                 and r["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")]
    if not decisions or decisions[-1]["state"] != "APPROVED":
        return ["your latest decision on the PR isn't an approval"]
    if decisions[-1].get("commit") == job["head"]:
        return ["you already approved the current head"]

    why = []
    pr_jobs = [j for j in jobs if (j.get("pr") or {}).get("id") == snap["id"]
               and j.get("trigger") != "mention"]
    unaddressed = 0
    for t in snap["threads"]:
        humans = [c for c in t["comments"] if not c["is_bot"]]
        mine = [c for c in humans if c["author"] == me]
        if t["resolved"] or not mine:
            continue
        latest = mine[-1]["id"]
        if not any(j["thread_id"] == t["id"] and j.get("arm_id") == latest
                   and (j["id"] == job["id"] or _addressed(j)) for j in pr_jobs):
            unaddressed += 1
    if unaddressed:
        why.append(f"{unaddressed} of your comments {'isn' if unaddressed == 1 else 'aren'}'t "
                   f"addressed yet")

    latest_decision: dict[str, dict] = {}
    for r in snap["reviews"]:
        if (not r["is_bot"] and r["author"] not in (me, author)
                and r["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")):
            latest_decision[r["author"]] = r
    standing = [who for who, r in latest_decision.items() if r["state"] == "CHANGES_REQUESTED"
                and not any(q["reviewer"] == who and (q.get("at") or "") > (r.get("submitted_at") or "")
                            for q in snap.get("review_requests") or [])]
    if standing:
        why.append(f"{_names(standing)}'s change request{'s are' if len(set(standing)) > 1 else ' is'} "
                   f"still unanswered")

    blocked_by = []
    for t in snap["threads"]:
        humans = [c for c in t["comments"] if not c["is_bot"]]
        if t["resolved"] or not humans or any(c["author"] == me for c in humans):
            continue
        if humans[-1]["author"] == author:
            continue
        blockers = [c["author"] for c in humans if c["author"] != author
                    and priority.is_blocking(comment_kind.kind(c["body"]), c.get("review_state"))]
        blocked_by += blockers[:1]
    if blocked_by:
        n = len(blocked_by)
        why.append(f"{_names(blocked_by)}'s blocking comment{'s are' if n > 1 else ' is'} still unanswered")
    return why


def reapproval_plan(job: dict, result: dict, cfg: dict, clone: Path,
                    snapshot=None, jobs=None) -> tuple[list[str] | None, str]:
    """(fix commit shas to cite, reason). shas is None when you must decide yourself.

    The follow-up must find that the author did exactly what your comment asked, the
    response must come from the author, reapproval_blockers must find nothing on a
    fresh snapshot of the PR, and a fixing commit must sit between your comment and
    the head. `snapshot` and `jobs` stand in for the GitHub fetch and the job store.
    """
    pr = job["pr"]
    if not cfg["watcher"].get("auto_reapprove", False):
        return None, "auto re-approval is off"
    if result["verdict"] != "addressed" or not result["mechanical"]:
        return None, "not a mechanical fix"
    if job["actor"] != pr["author"]:
        return None, "the response came from someone other than the PR author"
    if snapshot is None:
        from watcher import gh
        try:
            snapshot = gh.details({pr["id"]: "others"}, set(cfg["watcher"]["bots"]),
                                  gh.token()).get(pr["id"])
        except (gh.GhError, gh.gh_prs.DigestError):
            snapshot = None
        if snapshot is None:
            return None, "couldn't read the PR"
    why = reapproval_blockers(snapshot, job, all_jobs() if jobs is None else jobs, cfg["me"])
    if why:
        return None, "; ".join(why)
    shas = []
    for e in result["evidence"]:
        sha = str(e.get("sha") or "").strip()
        if not sha:
            continue
        full = _git(clone, "rev-parse", "--verify", f"{sha}^{{commit}}").stdout.strip()
        if (full and full not in shas
                and _git(clone, "merge-base", "--is-ancestor", job["arm_commit"], full).returncode == 0
                and _git(clone, "merge-base", "--is-ancestor", full, job["head"]).returncode == 0
                and full != job["arm_commit"]):
            shas.append(full)
    if not shas:
        return None, "no fixing commit between your comment and the head"
    return shas, "every comment of yours is addressed and nothing blocking is left"


def reapprove(job: dict, shas: list[str]) -> str | None:
    """Post the APPROVE review on the job's head. Returns its URL, or None on failure."""
    pr = job["pr"]
    payload = JOBS_DIR / f"{job['id']}.reapprove.json"
    payload.write_text(json.dumps({"commit_id": job["head"], "event": "APPROVE",
                                   "body": reapproval_body(pr["repo"], shas)}))
    proc = subprocess.run(["gh", "api", "-X", "POST",
                           f"repos/avetta/{pr['repo']}/pulls/{pr['number']}/reviews",
                           "--input", str(payload), "--jq", ".html_url"],
                          capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else None


def build_mention_prompt(job: dict, clone: Path, me: str) -> str:
    pr = job["pr"]
    where = {"issue": "a top-level comment on the PR", "review": "a review's body",
             "inline": f"an inline comment on {job.get('path')}"
                       + (f":{job['line']}" if job.get("line") else "")}[job["where"]]
    thread = ""
    if job.get("thread"):
        thread = "The thread, in order:\n" + "\n".join(
            f"- @{c['author']}: {c['body'][:1500]}" for c in job["thread"])
    clone_text = "(no local clone of this repo; answer from the comment and the thread)"
    if (clone / ".git").exists():
        _git(clone, "fetch", "-q", "--no-write-fetch-head", "origin", f"pull/{pr['number']}/head")
        clone_text = (f"The clone is at {clone}; the PR head is {str(job.get('head') or '')[:12]}. "
                      f"You may read with `git -C {clone} show|log|diff` at that commit.")
    return MENTION_PROMPT.format(
        actor=job["actor"], me=me, repo=pr["repo"], number=pr["number"], title=pr["title"],
        author=pr["author"], where=where, url=job.get("url") or "", body=job.get("body") or "",
        thread=thread, clone_text=clone_text)


def build_answer_prompt(job: dict, clone: Path, me: str) -> str:
    pr = job["pr"]
    from triage import comment_kind
    where = {"issue": "a top-level comment on the PR", "review": "the body of their change request",
             "inline": f"an inline comment on {job.get('path')}"}[job["where"]]
    thread = ""
    if job.get("thread"):
        thread = "The thread, in order:\n" + "\n".join(
            f"- @{c['author']}: {c['body'][:1500]}" for c in job["thread"])
    clone_text = "(no local clone of this repo; answer from the comment and the thread)"
    if (clone / ".git").exists():
        _git(clone, "fetch", "-q", "--no-write-fetch-head", "origin", f"pull/{pr['number']}/head")
        clone_text = (f"The clone is at {clone}; the PR head is {str(job.get('head') or '')[:12]}. "
                      f"You may read with `git -C {clone} show|log|diff` at that commit.")
    return ANSWER_PROMPT.format(
        actor=job["actor"], me=me, kind=comment_kind.WORDS.get(job.get("kind"), "comment").lower(),
        repo=pr["repo"], number=pr["number"], title=pr["title"], where=where,
        url=job.get("url") or "", body=job.get("body") or "", thread=thread, clone_text=clone_text)


def parse_answer_result(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the result")
    data = json.loads(text[start:end + 1])
    if not str(data.get("reply") or "").strip():
        raise ValueError("empty reply")
    stance = data.get("stance") if data.get("stance") in STANCES else "needs_you"
    return {"verdict": stance, "mechanical": False, "evidence": [],
            "summary": str(data.get("summary") or "").strip(),
            "note": str(data.get("note") or "").strip(), "reply": data["reply"]}


def answer_report(job: dict, result: dict) -> str:
    pr = job["pr"]
    quoted = "\n".join(f"> {ln}" for ln in (job.get("body") or "").splitlines())
    where = f" on `{job['path']}`" if job.get("path") else ""
    return "\n".join([
        f"# Reply to @{job['actor']}: {render.subtitle(pr)}", "", f"🔗 {pr['url']}",
        f"Their comment{where}: {job.get('url') or ''}", "",
        f"**@{job['actor']}:** {result.get('summary') or ''}", "", quoted, "",
        f"**Claude's draft {STANCE_WORDS.get(result['verdict'], result['verdict'])}.** "
        f"{result.get('note') or ''}", "",
        "## Drafted reply", "", with_footer(result["reply"]), "",
        "Post it from the menu (My PRs → this PR → Post this reply), or work through the "
        f"comments with Claude: {respond_link(pr['url'])}", ""])


def respond_link(pr_url: str) -> str:
    import session
    return session.respond_url(pr_url)


def parse_mention_result(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the result")
    data = json.loads(text[start:end + 1])
    if not str(data.get("reply") or "").strip():
        raise ValueError("empty reply")
    return {"verdict": "mention", "mechanical": False, "evidence": [],
            "summary": str(data.get("summary") or "").strip(), "reply": data["reply"]}


def mention_report(job: dict, result: dict) -> str:
    pr = job["pr"]
    quoted = "\n".join(f"> {ln}" for ln in (job.get("body") or "").splitlines())
    return "\n".join([
        f"# Mention: {render.subtitle(pr)}", "", f"🔗 {pr['url']}",
        f"Their comment: {job.get('url') or ''}", "",
        f"**@{job['actor']} asked:** {result.get('summary') or ''}", "", quoted, "",
        "## Drafted reply", "", with_footer(result["reply"]), ""])


def with_footer(reply: str) -> str:
    reply = reply.strip()
    return reply if reply.endswith(FOOTER) else f"{reply}\n\n{FOOTER}"


def report_markdown(job: dict, result: dict) -> str:
    pr = job["pr"]
    cr = job.get("trigger") == "change_request"
    trigger = (" and ".join(CR_TRIGGER_WORDS.get(t, t) for t in job.get("triggers") or [])
               if cr else job["trigger"])
    lines = [
        f"# Follow-up: {render.subtitle(pr)}",
        "",
        f"🔗 {pr['url']}",
        f"Your {'change request' if cr else 'comment'}: {job.get('my_comment_url') or ''}",
        f"Trigger: {trigger} by @{job['actor']} · range {job['arm_commit'][:8]}..{job['head'][:8]}",
        "",
        f"**Verdict:** {result['verdict'].replace('_', ' ')}",
        "",
        "## Evidence",
    ]
    for e in result["evidence"]:
        lines.append(f"- `{e.get('file')}:{e.get('line')}` @ {str(e.get('sha') or '')[:8]} — {e.get('note')}")
    lines += ["", "## Drafted reply", "", with_footer(result["reply"]), ""]
    return "\n".join(lines)


def run(jid: str, cfg: dict | None = None) -> int:
    cfg = cfg or settings.load()
    job = load_job(jid)
    pr = job["pr"]
    clone = settings.path_of(cfg["repos_root"]) / pr["repo"]
    reports = settings.path_of(cfg["watcher"]["reports_dir"])
    reports.mkdir(parents=True, exist_ok=True)
    mention = job.get("trigger") == "mention"
    answer = job.get("trigger") == "answer"
    cr = job.get("trigger") == "change_request"
    if cr:
        stem = reports / f"{pr['repo']}-{pr['number']}-cr-{jid}"
    elif answer:
        stem = reports / f"{pr['repo']}-{pr['number']}-answer-{jid}"
    elif mention:
        stem = reports / f"{pr['repo']}-{pr['number']}-mention-{job['comment_id']}"
    else:
        stem = reports / f"{pr['repo']}-{pr['number']}-{job['root_id']}-{job['arm_id']}"
    try:
        if answer:
            prompt = build_answer_prompt(job, clone, cfg["me"])
        elif mention:
            prompt = build_mention_prompt(job, clone, cfg["me"])
        elif not (clone / ".git").exists():
            raise RuntimeError(f"no local clone at {clone}")
        elif cr:
            rng = collect_range(clone, pr["number"], job["arm_commit"], job["head"], None)
            prompt = build_cr_prompt(job, rng, clone, cfg["me"])
        else:
            rng = collect_range(clone, pr["number"], job["arm_commit"], job["head"], job["path"])
            prompt = build_prompt(job, rng, clone, cfg["me"])
        job["prompt_sha256"] = hashlib.sha256(prompt.encode()).hexdigest()
        argv = ["claude", "-p", "--model", cfg["models"]["followup"], "--output-format", "json",
                "--permission-mode", "dontAsk",
                "--allowedTools", "Read", "Grep", "Glob",
                f"Bash(git -C {clone} show:*)", f"Bash(git -C {clone} log:*)",
                f"Bash(git -C {clone} diff:*)",
                "--disallowedTools", "Write", "Edit", "Bash(gh:*)", "Bash(curl:*)",
                "Bash(git push:*)", "WebFetch", "WebSearch", "Agent"]
        proc = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                              cwd=settings.workspace(cfg), env=_env(),
                              timeout=cfg["watcher"]["followup_timeout"])
        envelope = json.loads(proc.stdout or "{}")
        if envelope.get("is_error") or not envelope.get("result"):
            raise RuntimeError(f"claude failed: {str(envelope.get('result') or proc.stderr)[:200]}")
        result = (parse_answer_result if answer else parse_mention_result if mention
                  else parse_result)(envelope["result"])
    except (RuntimeError, ValueError, subprocess.SubprocessError, OSError) as exc:
        job.update(status="failed", error=str(exc)[:300])
        save_job(job)
        notify.banner("⚠️ Follow-up failed", render.subtitle(pr), str(exc)[:200], href=job["url"])
        return 1

    stem.with_suffix(".md").write_text(
        (answer_report if answer else mention_report if mention else report_markdown)(job, result))
    reply_path = stem.with_name(stem.name + ".reply.md")
    reply_path.write_text(with_footer(result["reply"]) + "\n")
    stem.with_suffix(".json").write_text(json.dumps({**result, "job": jid}, indent=2) + "\n")
    job.update(status="ready", verdict=result["verdict"], mechanical=result["mechanical"],
               report=str(stem.with_suffix(".md")),
               reply_file=str(reply_path), cost_usd=envelope.get("total_cost_usd"),
               finished_at=int(time.time()))
    if answer:
        job["note"] = result.get("note") or ""
    if mention or answer or cr:
        save_job(job)
        return 0
    shas, why = reapproval_plan(job, result, cfg, clone)
    _settle_reapproval(job, shas, why)
    return 0


def _settle_reapproval(job: dict, shas: list[str] | None, why: str) -> None:
    """Post the re-approval when the plan found fixing commits, record why otherwise,
    and save the job either way."""
    job["reapproval"] = why
    if shas:
        url = reapprove(job, shas)
        if url:
            job.update(status="reapproved", posted_url=url, reapproved_at=int(time.time()))
            save_job(job)
            import names
            who = names.book(fetch=True).get(job["actor"], job["actor"])
            notify.banner(f"✅ Re-approved: {who} did what you asked", render.subtitle(job["pr"]),
                          f"Fixed in {', '.join(s[:7] for s in shas)}", href=url)
            return
        job["reapproval"] = "posting the approval failed; decide yourself"
    save_job(job)


# Reasons that say re-approval isn't in play for the PR at all, so re-checking is pointless.
NOT_IN_PLAY = ("auto re-approval is off", "not a mechanical fix",
               "the response came from someone other than the PR author",
               "your latest decision on the PR isn't an approval",
               "you already approved the current head")


def held_reapproval(snap: dict, jobs: list[dict]) -> dict | None:
    """The job whose re-approval to re-check on this snapshot: the PR's latest finished
    follow-up that found the fix done as asked, on the head the PR still has, and was
    held for a reason that can change. None when there is none. Pure."""
    held = [j for j in jobs if (j.get("pr") or {}).get("id") == snap["id"]
            and j.get("trigger") != "mention" and _addressed(j) and j.get("status") == "ready"
            and j.get("head") == snap.get("head")
            and j.get("reapproval") and j["reapproval"] not in NOT_IN_PLAY]
    return max(held, key=lambda j: j.get("finished_at") or 0) if held else None


def recheck_reapprovals(snapshots: list[dict], jobs: list[dict], cfg: dict,
                        dry_run: bool = False) -> list[tuple[str, str]]:
    """Re-run the re-approval check on held follow-ups, against this tick's fresh
    snapshots of others' PRs, so a re-approval held by someone else's change request or
    blocking comment goes through once they're answered. Returns (job id, outcome)."""
    if not cfg["watcher"].get("auto_reapprove", False):
        return []
    out = []
    for snap in snapshots:
        job = held_reapproval(snap, jobs)
        if job is None:
            continue
        try:
            result = json.loads(Path(job["report"]).with_suffix(".json").read_text())
        except (OSError, ValueError, KeyError):
            continue
        clone = settings.path_of(cfg["repos_root"]) / job["pr"]["repo"]
        shas, why = reapproval_plan(job, result, cfg, clone, snapshot=snap, jobs=jobs)
        if dry_run:
            out.append((job["id"], "would re-approve" if shas else why))
        elif shas or why != job["reapproval"]:
            _settle_reapproval(job, shas, why)
            out.append((job["id"], "re-approved" if job["status"] == "reapproved" else why))
    return out


# --- Your actions from the menu --------------------------------------------------------


def reply_endpoint(repo: str, number: int, root_id: int | None, org: str = "avetta") -> str:
    """The REST endpoint a reply posts to: a reply in the inline thread whose first
    comment is `root_id`, or a top-level PR comment when there's no thread."""
    if root_id:
        return f"repos/{org}/{repo}/pulls/{number}/comments/{root_id}/replies"
    return f"repos/{org}/{repo}/issues/{number}/comments"


def post(jid: str) -> int:
    """Post the reply file's exact text as a threaded reply. Only the menu calls this."""
    job = load_job(jid)
    if job["status"] != "ready":
        print(f"job {jid} is {job['status']}, not ready", file=sys.stderr)
        return 1
    pr = job["pr"]
    state = subprocess.run(["gh", "pr", "view", str(pr["number"]), "--repo", f"avetta/{pr['repo']}",
                            "--json", "state", "-q", ".state"], capture_output=True, text=True).stdout.strip()
    if state != "OPEN":
        job.update(status="dismissed", error=f"PR is {state or 'unreadable'}; nothing posted")
        save_job(job)
        return 1
    reply_file = Path(job["reply_file"])
    text = reply_file.read_text()
    if not text.rstrip().endswith(FOOTER):
        reply_file.write_text(with_footer(text) + "\n")
        text = reply_file.read_text()
    digest = hashlib.sha256(text.encode()).hexdigest()
    if job.get("posted_sha256") == digest:
        return 0
    endpoint = reply_endpoint(pr["repo"], pr["number"], job.get("root_id"))
    proc = subprocess.run(
        ["gh", "api", "-X", "POST", endpoint, "-F", f"body=@{reply_file}", "--jq", ".html_url"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        job.update(error=f"post failed: {proc.stderr.strip()[:200]}")
        save_job(job)
        notify.banner("⚠️ Reply not posted", render.subtitle(pr), proc.stderr.strip()[:200])
        return 1
    job.update(status="posted", posted_sha256=digest, posted_url=proc.stdout.strip(),
               posted_at=int(time.time()))
    save_job(job)
    notify.banner("✅ Reply posted", render.subtitle(pr), proc.stdout.strip(), href=proc.stdout.strip())
    return 0


def dismiss(jid: str) -> int:
    job = load_job(jid)
    job.update(status="dismissed")
    save_job(job)
    return 0


def dismiss_done(jid: str, when: int | None = None) -> None:
    """Take a re-approval off the menu's Done for you section. The job keeps its
    status, so it still counts as addressed for later re-approval checks."""
    job = load_job(jid)
    job["done_dismissed_at"] = int(when if when is not None else time.time())
    save_job(job)


def cancel_for_pr(pr_id: str, state: str = "CLOSED") -> list[str]:
    """Clear every open job (CLEARABLE) on a PR that merged or closed, so its menu row
    goes away; returns their ids. A running job is left alone: once it finishes, the
    next tick finds it through `orphaned_prs` and clears it then."""
    why = "the PR merged" if state == "MERGED" else "the PR closed"
    out = []
    for job in all_jobs():
        if (job.get("pr") or {}).get("id") == pr_id and job.get("status") in CLEARABLE:
            clear(job["id"], why)
            out.append(job["id"])
    return out


def orphaned_prs(jobs: list[dict], open_ids) -> set[str]:
    """Ids of PRs that have an open job (CLEARABLE) but aren't among `open_ids`, the
    open PRs the watcher's search returned. Pure. The tick asks GitHub for their state
    and clears the jobs of any that merged or closed."""
    return {j["pr"]["id"] for j in jobs
            if j.get("status") in CLEARABLE and (j.get("pr") or {}).get("id")
            and j["pr"]["id"] not in open_ids}


def label(job: dict) -> str:
    return re.sub(r"\s+", " ", job.get("verdict", job["status"]).replace("_", " "))
