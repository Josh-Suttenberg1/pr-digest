"""An update review: re-review only what a PR's author changed since the draft's head.

review/auto_draft.py runs it when new commits arrive on a PR whose draft is ready, the
interdiff (review/interdiff.py) isn't empty, and it isn't large enough to call for a
full review. One claude run (the reviewer agent's model and effort, read-only tools,
the prompt in review/update_prompt.md) judges, against the interdiff only:

  - each earlier finding still open in a file the interdiff touches, or that names no
    file: addressed / partly / not / no longer relevant. An open finding in a file the
    interdiff leaves alone stays open without asking;
  - each open review thread, by any reviewer, on the lines the interdiff changed: the
    follow-up verdict (watcher/followup.py VERDICTS, `mechanical`, `evidence`);
  - the new and changed lines, for new defects, against pr-reviewer's checklists.

New findings other than nits go through the verifier agent (review.verifier_agent) on
their own, as a pr-reviewer report holding only them; earlier findings aren't
re-verified. The result goes at the top of the existing draft as an update section
between UPDATE_START and UPDATE_END markers, and the record (review/record.py) moves its
head to the new head with the findings' new statuses.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import settings
from review import record as rec_mod
from review import review_pr

PROMPT_PATH = Path(__file__).resolve().parent / "update_prompt.md"
COMMENT_VERDICTS = ("addressed", "partly", "not_addressed")
VERDICT_WORDS = {"addressed": "addressed", "partly": "partly addressed",
                 "not_addressed": "not addressed"}
# Lines around a review comment's line that still count as "on the changed lines".
NEAR = 3

THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) {
    id headRefOid baseRefName
    reviewThreads(first: 100) { nodes { id isResolved isOutdated path line originalLine
      resolvedBy { login } comments(first: 50) { nodes { %s } } } } } }
}
"""


class UpdateError(Exception):
    pass


# --- inputs ----------------------------------------------------------------------------


def agent_settings(cfg: dict, agent: str) -> dict:
    """The `model` and `effort` in an agent's frontmatter, from <workspace>/.claude/agents/
    or ~/.claude/agents/; {} when there's no such file."""
    for folder in (settings.workspace(cfg) / ".claude" / "agents",
                   Path("~/.claude/agents").expanduser()):
        path = folder / f"{agent}.md"
        if not path.exists():
            continue
        head = path.read_text().split("---")
        front = head[1] if len(head) > 2 else ""
        found = dict(re.findall(r"^(model|effort):\s*(\S+)\s*$", front, re.M))
        return found
    return {}


def fetch_threads(owner: str, repo: str, number: int, bots: set[str]) -> list[dict]:
    """The PR's review threads, normalized like the watcher's (watcher/gh.py)."""
    from watcher import gh
    data, _ = gh.graphql(THREADS_QUERY % gh.COMMENT_FIELDS,
                         {"owner": owner, "name": repo, "number": number}, gh.token())
    pr = ((data.get("repository") or {}).get("pullRequest")) or {}
    return [gh.normalize_thread(t, bots) for t in (pr.get("reviewThreads") or {}).get("nodes") or []]


def comments_on_changes(threads: list[dict], delta: dict, author: str) -> list[dict]:
    """Open threads started by a reviewer (not the author, not a bot) on a file the
    interdiff changed, either outdated by it or on a line within NEAR of a changed hunk.
    Each: {id (the first comment's id), author, path, line, url, body, thread}."""
    out = []
    for t in threads:
        humans = [c for c in t["comments"] if not c["is_bot"]]
        if t["resolved"] or not humans or humans[0]["author"] == author:
            continue
        path = t.get("path")
        if path not in delta["files"]:
            continue
        line = t.get("line")
        near = t.get("outdated") or line is None or any(
            a - NEAR <= line <= b + NEAR for a, b in delta["ranges"].get(path) or [])
        if not near:
            continue
        first = humans[0]
        out.append({"id": first["id"], "author": first["author"], "path": path,
                    "line": line or t.get("original_line"), "url": first.get("url") or "",
                    "body": first["body"], "thread": humans})
    return out


def touches(path: str, files) -> bool:
    """True when `path`, as a finding names it (often shortened to its last few
    segments), is one of `files` (full repo paths)."""
    path = path.strip("./")
    return any(f == path or f.endswith("/" + path) for f in files)


def to_judge(findings: list[dict], delta: dict) -> tuple[list[dict], list[dict]]:
    """(findings the model judges, findings left open untouched): open findings in a
    file the interdiff changes or naming no file, and open findings elsewhere."""
    judge, untouched = [], []
    for f in findings:
        if f.get("status", "open") not in rec_mod.OPEN:
            continue
        (judge if not f.get("path") or touches(f["path"], delta["files"])
         else untouched).append(f)
    return judge, untouched


def build_prompt(pr: dict, delta: dict, judge: list[dict], comments: list[dict],
                 old_head: str, new_head: str, clone: Path, me: str) -> str:
    def finding_line(f):
        where = f"`{f['path']}:{f['line']}`" if f.get("path") and f.get("line") else (
            f"`{f['path']}`" if f.get("path") else "(no file)")
        return f"- id {f['id']}: {rec_mod.MARKS[f['severity']]} {where} {f['summary']}"

    def comment_block(c):
        thread = "\n".join(f"    - @{x['author']}: {x['body'][:1500]}" for x in c["thread"])
        return f"- id {c['id']}: @{c['author']} on `{c['path']}:{c['line']}` ({c['url']})\n{thread}"

    n = delta.get("commits")
    commits_text = ("a force push (the old head isn't an ancestor of the new one)" if n is None
                    else f"{n} commit{'s' if n != 1 else ''}")
    method_text = ("the old PR diff replayed onto the new base, against the new head"
                   if delta["method"] == "replay" else
                   "replaying the old PR diff onto the new base conflicted, so this is the "
                   "plain diff between the heads for the files whose own changes differ; "
                   "it can include base-branch changes to those files")
    elm = ("~/avetta/.claude/elm-conventions.md (this is the ui repo)." if pr["repo"] == "ui"
           else "(no extra rules for this repo)")
    return PROMPT_PATH.read_text().format(
        me=me, repo=pr["repo"], number=pr["number"], title=pr.get("title") or "",
        author=pr.get("author") or "", base=pr.get("base") or "main",
        old_short=old_head[:7], new_short=new_head[:7], old_head=old_head, new_head=new_head,
        commits_text=commits_text, method_text=method_text, diff=delta["diff"],
        truncated_note=("(The interdiff is longer than shown; read the rest with git diff.)\n"
                        if delta.get("truncated") else ""),
        findings="\n".join(finding_line(f) for f in judge) or "(none)",
        comments="\n\n".join(comment_block(c) for c in comments) or "(none)",
        elm_rules=elm, clone=clone)


def parse_result(text: str, finding_ids: list[str], comment_ids: list[int]) -> dict:
    """The model's JSON, checked: one known status per finding and verdict per
    comment; anything unknown is dropped. Raises ValueError when it isn't JSON."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the result")
    data = json.loads(text[start:end + 1])
    findings = {}
    for f in data.get("findings") or []:
        if isinstance(f, dict) and str(f.get("id")) in finding_ids \
                and f.get("status") in rec_mod.STATUSES:
            findings[str(f["id"])] = {"status": f["status"], "sha": str(f.get("sha") or ""),
                                      "note": str(f.get("note") or "").strip()}
    comments = {}
    for c in data.get("comments") or []:
        try:
            cid = int(c.get("id"))
        except (TypeError, ValueError, AttributeError):
            continue
        if cid in comment_ids and c.get("verdict") in COMMENT_VERDICTS:
            comments[cid] = {"verdict": c["verdict"], "mechanical": c.get("mechanical") is True,
                             "evidence": [e for e in c.get("evidence") or [] if isinstance(e, dict)],
                             "note": str(c.get("note") or "").strip()}
    new = []
    for f in data.get("new_findings") or []:
        if isinstance(f, dict) and f.get("severity") in rec_mod.MARKS and str(f.get("text") or "").strip():
            try:
                line = int(f.get("line") or 0) or None
            except (TypeError, ValueError):
                line = None
            new.append({"severity": f["severity"], "path": str(f.get("path") or "") or None,
                        "line": line, "text": re.sub(r"\s+", " ", str(f["text"])).strip(),
                        "evidence": str(f.get("evidence") or "").strip()})
    return {"findings": findings, "comments": comments, "new_findings": new}


# --- verifying the new findings ----------------------------------------------------------


def _finding_md(n: int, f: dict) -> str:
    where = f"`{f['path']}:{f['line']}` — " if f.get("path") and f.get("line") else (
        f"`{f['path']}` — " if f.get("path") else "— ")
    return f"{n}. {rec_mod.MARKS[f['severity']]} {where}{f['text']}"


def mini_report(pr: dict, rec: dict, new: list[dict], old_head: str, new_head: str) -> str:
    """The new findings alone as a pr-reviewer report, for the verifier."""
    actions = "\n".join(_finding_md(i, f) for i, f in enumerate(new, 1))
    evidence = "\n".join(f"- {i}: {f['evidence']}" for i, f in enumerate(new, 1) if f["evidence"])
    sev = review_pr.severity_of(actions)
    return (f"Reviewer: pr-reviewer · coworker review · {datetime.now():%Y-%m-%d}\n\n"
            f"## {rec.get('key') or pr['repo']} ({pr.get('title') or ''}) · avetta/{pr['repo']}"
            f"#{pr['number']} · @{pr.get('author') or ''} · head {new_head}\n"
            f"🔗 {pr.get('url') or ''}\n"
            f"**TL;DR:** the issues found in the changes since {old_head[:7]}.\n\n"
            f"### Actions required\n{actions}\n\n"
            f"<details><summary>Evidence</summary>\n\n{evidence}\n</details>\n\n"
            f"SEVERITY: {sev}\n")


def surviving(verified: str) -> tuple[str, str]:
    """(the verified report's numbered findings, with their continuation lines; its
    Verification <details> block), both '' when absent."""
    body = re.split(r"^<details><summary>Verification", verified, maxsplit=1, flags=re.M)
    details = ""
    if len(body) == 2:
        details = "<details><summary>Verification" + body[1].split("</details>", 1)[0] + "</details>"
    m = re.search(r"^### Actions required\s*\n(.*?)(?=^### |^<details>|^SEVERITY:|\Z)",
                  body[0], re.S | re.M)
    block = m.group(1) if m else "\n".join(
        ln for ln in body[0].splitlines() if rec_mod.FINDING_LINE_RE.match(ln))
    return block.strip(), details


# --- the update section ------------------------------------------------------------------


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def summary_line(old_head: str, commits: int | None, judged: dict[str, dict],
                 still_open: int, comments: list[dict], verdicts: dict[int, dict],
                 new_count: int, nick: dict[str, str]) -> str:
    """'Since abc1234, 3 commits: 2 findings addressed, 1 still open; Mykal's comment
    addressed; 1 new issue.'"""
    lead = (f"Since {old_head[:7]}, a force push" if commits is None
            else f"Since {old_head[:7]}, {_plural(commits, 'commit')}")
    parts = []
    addressed = sum(1 for v in judged.values() if v["status"] == "addressed")
    gone = sum(1 for v in judged.values() if v["status"] == "no_longer_relevant")
    bits = []
    if addressed:
        bits.append(f"{_plural(addressed, 'finding')} addressed")
    if gone:
        bits.append(f"{gone} no longer relevant")
    if still_open:
        bits.append(f"{still_open} still open")
    if bits:
        parts.append(", ".join(bits))
    by: dict[str, list[str]] = {}
    for c in comments:
        v = verdicts.get(c["id"])
        if v:
            by.setdefault(c["author"], []).append(v["verdict"])
    for author, said in by.items():
        name = nick.get(author, author)
        if len(set(said)) == 1:
            noun = "comment" if len(said) == 1 else f"{len(said)} comments"
            parts.append(f"{name}'s {noun} {VERDICT_WORDS[said[0]]}")
        else:
            tally = ", ".join(f"{said.count(v)} {VERDICT_WORDS[v]}"
                              for v in COMMENT_VERDICTS if v in said)
            parts.append(f"{name}'s {len(said)} comments: {tally}")
    parts.append("no new issues" if not new_count else _plural(new_count, "new issue"))
    return f"{lead}: {'; '.join(parts)}."


STATUS_WORDS = {"addressed": "addressed", "partly": "partly addressed", "not": "still open",
                "no_longer_relevant": "no longer relevant", "open": "still open"}


def _cut(text: str, limit: int = 140) -> str:
    """`text` cut at a word boundary, with an ellipsis when it was longer, and any
    backtick or bold marker it leaves open closed."""
    if len(text) <= limit:
        return text
    out = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:(")
    if out.count("`") % 2:
        out += "`"
    if out.count("**") % 2:
        out += "**"
    return out + "…"


def section(pr: dict, old_head: str, new_head: str, delta: dict, line: str,
            earlier: list[dict], judged: dict[str, dict], comments: list[dict],
            verdicts: dict[int, dict], new_block: str, details: str,
            nick: dict[str, str]) -> str:
    out = [rec_mod.UPDATE_START,
           f"## Update since `{old_head[:7]}` · head `{new_head[:7]}` · "
           f"{datetime.now():%Y-%m-%d %H:%M}", "", line, ""]
    if earlier:
        out.append("### Earlier findings")
        for f in earlier:
            where = f" `{f['path']}{':' + str(f['line']) if f.get('line') else ''}`" if f.get("path") else ""
            v = judged.get(f["id"])
            if v:
                sha = f" in `{v['sha'][:7]}`" if v.get("sha") else ""
                note = f": {v['note']}" if v.get("note") else ""
                status = f"**{STATUS_WORDS[v['status']]}**{sha}{note}"
            else:
                status = f"**still open** (the changes don't touch `{f['path']}`)"
            out.append(f"- {f['n']}. {rec_mod.MARKS[f['severity']]}{where}: "
                       f"{_cut(f['summary'])} · {status}")
        out.append("")
    if comments:
        out.append("### Review comments on the changed lines")
        for c in comments:
            v = verdicts.get(c["id"])
            name = nick.get(c["author"], c["author"])
            words = (f"**{VERDICT_WORDS[v['verdict']]}**" + (f" · {v['note']}" if v["note"] else "")
                     if v else "not judged")
            out.append(f"- [{name} on `{c['path']}:{c['line']}`]({c['url']}): {words}")
        out.append("")
    out.append("### New issues")
    out.append(new_block or "None in the changed lines.")
    out.append("")
    if details:
        out += [details, ""]
    stat = "\n".join(f"- `{p}`" for p in delta["files"])
    out += [f"<details><summary>Interdiff: {_plural(len(delta['files']), 'file')}, "
            f"{_plural(delta['lines'], 'changed line')} of the PR's {delta['pr_lines']}</summary>",
            "", stat, "</details>", "", "---", rec_mod.UPDATE_END, "", ""]
    return "\n".join(out)


# --- the run -----------------------------------------------------------------------------


def _claude(prompt: str, cfg: dict, clone: Path, timeout: int) -> dict:
    agent = agent_settings(cfg, cfg["review"]["reviewer_agent"] or "pr-reviewer")
    argv = ["claude", "-p", "--model", agent.get("model") or "opus", "--output-format", "json",
            "--permission-mode", "dontAsk"]
    if agent.get("effort"):
        argv += ["--effort", agent["effort"]]
    argv += ["--allowedTools", "Read", "Grep", "Glob",
             f"Bash(git -C {clone} show:*)", f"Bash(git -C {clone} log:*)",
             f"Bash(git -C {clone} diff:*)", f"Bash(git -C {clone} grep:*)",
             "Bash(gh pr view:*)", "Bash(gh pr diff:*)", "Bash(gh pr checks:*)",
             "--disallowedTools", "Write", "Edit", "NotebookEdit", "Bash(gh api:*)",
             "Bash(gh pr comment:*)", "Bash(gh pr review:*)", "Bash(gh pr merge:*)",
             "Bash(gh pr edit:*)", "Bash(curl:*)", "Bash(git push:*)", "WebFetch", "WebSearch",
             "Agent"]
    try:
        proc = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                              cwd=settings.workspace(cfg), env=review_pr._env(), timeout=timeout)
    except subprocess.TimeoutExpired:
        raise UpdateError(f"the update review timed out after {timeout // 60} min")
    try:
        envelope = json.loads(proc.stdout or "{}")
    except ValueError:
        raise UpdateError(f"the update review returned no JSON (exit {proc.returncode})")
    if envelope.get("is_error") or not envelope.get("result"):
        raise UpdateError(f"the update review failed: {str(envelope.get('result') or proc.stderr)[:200]}")
    return envelope


def run(rec: dict, pr: dict, delta: dict, old_head: str, new_head: str, clone: Path,
        cfg: dict, threads: list[dict], nick: dict[str, str], *, claude=None, verify=None,
        index_dir: Path | None = None) -> dict:
    """Run the update review and write it into the draft and the record. Returns the
    new record. `claude(prompt) -> envelope` and `verify(report_path) -> envelope` stand
    in for the two claude runs."""
    started = time.time()
    rcfg = cfg["review"]
    claude = claude or (lambda prompt: _claude(prompt, cfg, clone, rcfg["timeout"]))
    findings = [dict(f) for f in rec.get("findings") or []]
    judge, untouched = to_judge(findings, delta)
    comments = comments_on_changes(threads, delta, pr.get("author") or "")
    prompt = build_prompt(pr, delta, judge, comments, old_head, new_head, clone, cfg["me"])
    env1 = claude(prompt)
    try:
        result = parse_result(env1["result"], [f["id"] for f in judge], [c["id"] for c in comments])
    except ValueError as exc:
        raise UpdateError(f"the update review's answer wasn't usable: {exc}")

    new = result["new_findings"]
    new_block, details, env2 = "", "", None
    if new:
        report = mini_report(pr, rec, new, old_head, new_head)
        new_block = "\n".join(_finding_md(i, f) for i, f in enumerate(new, 1))
        if rcfg.get("verifier_agent") and review_pr.severity_of(report) == "substantive":
            run_dir = review_pr.RUNS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-update-{pr['repo']}-{pr['number']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            path = run_dir / "update-findings.md"
            path.write_text(report)
            verify = verify or (lambda p: review_pr.run_agent(
                rcfg["verifier_agent"], f"Verify the review at {p}.", settings.workspace(cfg),
                rcfg["timeout"], run_dir / "verifier.stderr"))
            env2 = verify(path)
            new_block, details = surviving(review_pr.strip_preamble(env2["result"]))
            if details:
                details = details.replace("<summary>Verification", "<summary>Verification of the new issues", 1)

    sha7 = new_head[:7]
    kept = rec_mod.parse_findings(new_block, prefix=f"u{sha7}-") if new_block else []
    for f in kept:
        f["status_head"] = new_head
    judged = result["findings"]
    for f in findings:
        if f["id"] in judged:
            v = judged[f["id"]]
            f.update(status=v["status"], status_note=v["note"], status_head=new_head)
    earlier = judge + untouched
    earlier.sort(key=lambda f: f["n"])
    still_open = (sum(1 for f in judge if judged.get(f["id"], {}).get("status", "not") in ("partly", "not"))
                  + len(untouched))
    line = summary_line(old_head, delta.get("commits"), judged, still_open, comments,
                        result["comments"], len(kept), nick)
    text = section(pr, old_head, new_head, delta, line, earlier, judged, comments,
                   result["comments"], new_block, details, nick)

    draft = Path(rec["draft"])
    draft.write_text(text + draft.read_text())
    all_findings = findings + kept
    c = rec_mod.counts(all_findings)
    cost = round(sum((e or {}).get("total_cost_usd") or 0 for e in (env1, env2)), 2)
    verdicts = [{"id": cm["id"], "author": cm["author"], "path": cm["path"], "line": cm["line"],
                 "url": cm["url"], "head": new_head, **result["comments"][cm["id"]]}
                for cm in comments if cm["id"] in result["comments"]]
    judged_ids = {v["id"] for v in verdicts}
    out = {**rec, "head": new_head, "findings": all_findings, "counts": c,
           "severity": rec_mod.severity(c),
           "comment_verdicts": [v for v in rec.get("comment_verdicts") or []
                                if v.get("id") not in judged_ids] + verdicts,
           "history": (rec.get("history") or []) + [{
               "kind": "update", "from": old_head, "to": new_head,
               "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "cost_usd": cost, "duration_s": int(time.time() - started), "summary": line,
               "interdiff_lines": delta["lines"], "interdiff_files": len(delta["files"]),
               "verified": env2 is not None}]}
    rec_mod.save(out, index_dir)
    return out
