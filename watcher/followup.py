"""Follow-ups: when someone responds to Josh's review comment, check the new commits.

A job is one JSON file in state/watcher/jobs/<job_id>.json, where job_id is derived
from (thread id, Josh's comment id), so the same response never queues twice.

  queued  -> running (detached `python -m watcher followup-run <job>`) -> ready | failed
  ready   -> posted (Josh clicked "Post this reply") | dismissed

The run collects only the commits since Josh's comment, asks claude (models.followup)
for a verdict and a drafted reply, and writes the report itself. claude gets read-only
tools scoped to the repo clone and cannot post: posting happens only in post(), which
the menu bar calls when Josh clicks, and which posts exactly the reply file's text.
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

PROMPT = """Josh left a review comment on a coworker's pull request. Since then, {trigger_text}.
Decide whether the commits since his comment address it, and draft his reply.

PR: avetta/{repo}#{number} — {title}
PR author: @{author}
File: {path}{line_suffix}
Josh's comment ({comment_url}):
---
{my_comment}
---

The thread, in order:
{thread}

Commits since Josh's comment ({arm_short}..{head_short}){rebased_note}:
{commits}

Diff of {path} over that range:
```diff
{file_diff}
```

Files changed over that range:
{stat}

The clone is at {clone}. You may read more with `git -C {clone} show|log|diff` at
these commits. Judge only against what Josh's comment asked for. Don't review anything else.

Reply rules: address the PR author by handle, plain English, at most 3 sentences. Cite the
commit (short sha) that fixed it. If it's not addressed, say concretely what is still missing.
No bare acceptance-criteria labels. Don't add a signature.

"mechanical" is true only when the commits do exactly what Josh's comment asked, as he asked
it: no pushback, no different approach, nothing he asked left open. Explaining a separate
question from someone else in the thread doesn't make it false.

Output ONLY a JSON object, no prose, no code fences:
{{"verdict": "addressed" | "partly" | "not_addressed",
  "mechanical": true | false,
  "evidence": [{{"file": "...", "line": 0, "sha": "<full or short sha of the fixing commit>", "note": "..."}}],
  "reply": "..."}}
"""


def job_id(thread_id: str, arm_id: int) -> str:
    return hashlib.sha1(f"{thread_id}:{arm_id}".encode()).hexdigest()[:12]


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
    if not JOBS_DIR.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(JOBS_DIR.glob("*.json"))]


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
        "pr": event["pr"],
    })
    return jid


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
    """Start queued jobs up to max_concurrent. Returns (started, claude_logged_out)."""
    jobs = all_jobs()
    for job in jobs:   # a run that died (crash, reboot) without finishing
        if job["status"] == "running" and not _alive(job.get("pid")):
            job.update(status="failed", error="the follow-up process died")
            save_job(job)
    running = sum(1 for j in jobs if j["status"] == "running")
    queued = [j for j in jobs if j["status"] == "queued"]
    room = cfg["watcher"]["max_concurrent"] - running
    if not queued or room <= 0:
        return 0, False
    if claude_logged_in() is False:
        return 0, True
    started = 0
    for job in queued[:room]:
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


def collect_range(clone: Path, number: int, arm: str, head: str, path: str) -> dict:
    """Commits and diffs between Josh's comment's commit and the current head."""
    _git(clone, "fetch", "-q", "--no-write-fetch-head", "origin", f"pull/{number}/head")
    if _git(clone, "cat-file", "-e", f"{arm}^{{commit}}").returncode != 0:
        _git(clone, "fetch", "-q", "--no-write-fetch-head", "origin", arm)
    rebased = _git(clone, "merge-base", "--is-ancestor", arm, head).returncode != 0
    if rebased:
        commits = "(the branch was rebased or force-pushed since the comment; showing the tree diff)"
    else:
        commits = _git(clone, "log", "--first-parent", "--format=%h %an: %s", f"{arm}..{head}").stdout.strip()
    file_diff = _git(clone, "diff", arm, head, "--", path).stdout
    stat = _git(clone, "diff", "--stat", arm, head).stdout.strip()
    return {"rebased": rebased, "commits": commits or "(none)",
            "file_diff": file_diff[:DIFF_LIMIT] or "(no change to this file)",
            "stat": stat[-4000:] or "(no files changed)"}


def build_prompt(job: dict, rng: dict, clone: Path) -> str:
    pr = job["pr"]
    trigger_text = {
        "reply": f"@{job['actor']} replied in the thread",
        "outdated": "a commit changed the lines his comment was on",
        "moved": "commits landed that touch the file his comment was on",
    }[job["trigger"]]
    thread = "\n".join(f"- @{c['author']}: {c['body'][:1500]}" for c in job["thread"])
    return PROMPT.format(
        trigger_text=trigger_text, repo=pr["repo"], number=pr["number"], title=pr["title"],
        author=pr["author"], path=job["path"],
        line_suffix=f":{job['line']}" if job.get("line") else "",
        comment_url=job.get("my_comment_url") or "", my_comment=job["my_comment"],
        thread=thread, arm_short=job["arm_commit"][:8], head_short=job["head"][:8],
        rebased_note=" — rebased, tree diff" if rng["rebased"] else "",
        commits=rng["commits"], file_diff=rng["file_diff"], stat=rng["stat"], clone=clone,
    )


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


def _josh_reviews(repo: str, number: int) -> list[dict] | None:
    proc = subprocess.run(["gh", "api", f"repos/avetta/{repo}/pulls/{number}/reviews",
                           "--paginate", "--jq", ".[]"], capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def reapproval_plan(job: dict, result: dict, cfg: dict, clone: Path) -> tuple[list[str] | None, str]:
    """(fix commit shas to cite, reason). shas is None when Josh must decide himself.

    Re-approve only when the author did exactly what Josh's comment asked, Josh's latest
    decision on the PR is an approval that predates the current head, and the head
    hasn't moved since the follow-up looked at it.
    """
    pr = job["pr"]
    if not cfg["watcher"].get("auto_reapprove", True):
        return None, "auto re-approval is off"
    if result["verdict"] != "addressed" or not result["mechanical"]:
        return None, "not a mechanical fix"
    if job["actor"] != pr["author"]:
        return None, "the response came from someone other than the PR author"
    view = subprocess.run(["gh", "pr", "view", str(pr["number"]), "--repo", f"avetta/{pr['repo']}",
                           "--json", "state,headRefOid"], capture_output=True, text=True)
    try:
        live = json.loads(view.stdout)
    except ValueError:
        return None, "couldn't read the PR"
    if live.get("state") != "OPEN" or live.get("headRefOid") != job["head"]:
        return None, "the PR moved or closed after the follow-up ran"
    me = cfg["me"]
    reviews = _josh_reviews(pr["repo"], pr["number"])
    if reviews is None:
        return None, "couldn't read the reviews"
    mine = [r for r in reviews if (r.get("user") or {}).get("login") == me
            and r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")]
    if not mine or mine[-1]["state"] != "APPROVED":
        return None, "your latest decision on the PR isn't an approval"
    if mine[-1].get("commit_id") == job["head"]:
        return None, "you already approved the current head"
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
    return shas, "mechanical fix on a PR you approved"


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


def with_footer(reply: str) -> str:
    reply = reply.strip()
    return reply if reply.endswith(FOOTER) else f"{reply}\n\n{FOOTER}"


def report_markdown(job: dict, result: dict) -> str:
    pr = job["pr"]
    lines = [
        f"# Follow-up: {render.subtitle(pr)}",
        "",
        f"🔗 {pr['url']}",
        f"Your comment: {job.get('my_comment_url') or ''}",
        f"Trigger: {job['trigger']} by @{job['actor']} · range {job['arm_commit'][:8]}..{job['head'][:8]}",
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
    stem = reports / f"{pr['repo']}-{pr['number']}-{job['root_id']}-{job['arm_id']}"
    try:
        if not (clone / ".git").exists():
            raise RuntimeError(f"no local clone at {clone}")
        rng = collect_range(clone, pr["number"], job["arm_commit"], job["head"], job["path"])
        prompt = build_prompt(job, rng, clone)
        job["prompt_sha256"] = hashlib.sha256(prompt.encode()).hexdigest()
        argv = ["claude", "-p", "--model", cfg["models"]["followup"], "--output-format", "json",
                "--permission-mode", "dontAsk",
                "--allowedTools", "Read", "Grep", "Glob",
                f"Bash(git -C {clone} show:*)", f"Bash(git -C {clone} log:*)",
                f"Bash(git -C {clone} diff:*)",
                "--disallowedTools", "Write", "Edit", "Bash(gh:*)", "Bash(curl:*)",
                "Bash(git push:*)", "WebFetch", "WebSearch", "Agent"]
        proc = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                              cwd=settings.path_of(cfg["claude_cwd"]), env=_env(),
                              timeout=cfg["watcher"]["followup_timeout"])
        envelope = json.loads(proc.stdout or "{}")
        if envelope.get("is_error") or not envelope.get("result"):
            raise RuntimeError(f"claude failed: {str(envelope.get('result') or proc.stderr)[:200]}")
        result = parse_result(envelope["result"])
    except (RuntimeError, ValueError, subprocess.SubprocessError, OSError) as exc:
        job.update(status="failed", error=str(exc)[:300])
        save_job(job)
        notify.banner("⚠️ Follow-up failed", render.subtitle(pr), str(exc)[:200], href=job["url"])
        return 1

    stem.with_suffix(".md").write_text(report_markdown(job, result))
    reply_path = stem.with_name(stem.name + ".reply.md")
    reply_path.write_text(with_footer(result["reply"]) + "\n")
    stem.with_suffix(".json").write_text(json.dumps({**result, "job": jid}, indent=2) + "\n")
    job.update(status="ready", verdict=result["verdict"], mechanical=result["mechanical"],
               report=str(stem.with_suffix(".md")),
               reply_file=str(reply_path), cost_usd=envelope.get("total_cost_usd"),
               finished_at=int(time.time()))
    shas, why = reapproval_plan(job, result, cfg, clone)
    job["reapproval"] = why
    if shas:
        url = reapprove(job, shas)
        if url:
            job.update(status="reapproved", posted_url=url)
            save_job(job)
            who = cfg.get("nicknames", {}).get(job["actor"], job["actor"])
            notify.banner(f"✅ Re-approved: {who} did what you asked", render.subtitle(pr),
                          f"Fixed in {', '.join(s[:7] for s in shas)}", href=url)
            return 0
        job["reapproval"] = "posting the approval failed; decide yourself"
    save_job(job)
    shown = render.render({"type": "verdict", "verdict": result["verdict"], "actor": job["actor"],
                           "pr": pr, "url": Path(job["report"]).as_uri()},
                          cfg.get("nicknames", {}))
    notify.alert(shown["title"], shown["subtitle"], shown["body"], href=shown["href"],
                 plugin=cfg["notify"]["swiftbar_plugin"], sound=cfg["notify"]["alert_sound"])
    return 0


# --- Josh's actions from the menu ------------------------------------------------------


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
    proc = subprocess.run(
        ["gh", "api", "-X", "POST",
         f"repos/avetta/{pr['repo']}/pulls/{pr['number']}/comments/{job['root_id']}/replies",
         "-F", f"body=@{reply_file}", "--jq", ".html_url"],
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


def cancel_for_pr(pr_id: str) -> None:
    for job in all_jobs():
        if job["pr"]["id"] == pr_id and job["status"] == "queued":
            job.update(status="dismissed", error="PR closed")
            save_job(job)


def label(job: dict) -> str:
    return re.sub(r"\s+", " ", job.get("verdict", job["status"]).replace("_", " "))
