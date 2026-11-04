"""Plain-text status of every PR the menu knows about, for a Claude session to read.

Written by each menu fetch to output/status/:
  index.md         one line per PR: where the menu puts it and why
  <repo>#<n>.md    one file per PR: section, reason, and the facts the reason rests on
                   (change requests and whether you re-requested, open threads, your
                   last review, approvals, drafts and follow-up reports, with links)

A session asked about a PR reads its file instead of re-deriving the history from
GitHub. Files for PRs no longer in the menu are removed on the next write. Times are
local, with the zone abbreviation.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
STATUS_DIR = REPO_DIR / "output" / "status"


def key_of(url: str) -> str:
    parts = url.rstrip("/").split("/")
    return f"{parts[-3]}#{parts[-1]}"


def local(stamp) -> str:
    """'2026-10-30 17:15 MDT' from an ISO string or epoch seconds; '' when unknown."""
    if not stamp:
        return ""
    try:
        if isinstance(stamp, (int, float)):
            dt = datetime.fromtimestamp(stamp).astimezone()
        else:
            dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone()
    except (ValueError, OSError):
        return str(stamp)
    return f"{dt:%Y-%m-%d %H:%M %Z}"


def _review_facts(st: dict) -> list[str]:
    facts = [f"You are a reviewer. Author: {st.get('author', '?')}."]
    if st.get("approvals") is not None:
        facts.append(f"Approvals: {', '.join(st['approvals']) or 'none'}.")
    crs = st.get("change_requests") or {}
    if crs:
        standing = set(st.get("standing") or [])
        facts.append("Change requests: " + "; ".join(
            f"{u} ({'stands on the current code' if u in standing else 'predates newer commits'})"
            for u in sorted(crs)) + ".")
    mine = st.get("mine")
    if mine:
        facts.append(f"Your standing decision: {mine[0]} on {mine[1][:8]}.")
    elif st.get("commented"):
        facts.append("You have commented, without an approve/request-changes decision.")
    my_review = st.get("my_review") or ["", ""]
    if my_review[0]:
        facts.append(f"Your latest review: {local(my_review[0])} on {my_review[1][:8]}.")
    if st.get("head_sha"):
        facts.append(f"Head: {st['head_sha'][:8]}. Size: {st.get('size', '?')}.")
    if st.get("tags"):
        facts.append("Ranking notes: " + ", ".join(st["tags"]) + ".")
    if st.get("draft_path"):
        facts.append(f"Local review draft: {st['draft_path']} ({st.get('freshness', '')}).")
    elif st.get("freshness"):
        facts.append(f"Local review draft: {st['freshness']}.")
    return facts


def _mine_facts(row: dict) -> list[str]:
    facts = ["Your PR." + (" It is a GitHub draft." if row.get("isDraft") else "")]
    for b in row.get("blockers") or []:
        facts.append(f"Change request from {b['author']} at {local(b.get('at'))}, and you "
                     f"haven't re-requested them since: {b.get('url', '')}")
    for b in row.get("answered_change_requests") or []:
        facts.append(f"Change request from {b['author']} at {local(b.get('at'))}; you "
                     f"re-requested them at {local(b.get('rerequested_at'))}, so it's "
                     f"waiting on their re-review: {b.get('url', '')}")
    for t in row.get("threads_waiting") or []:
        where = f" on {t['path']}" if t.get("path") else ""
        facts.append(f"Open thread{where}, last comment by {t['last_author']} at "
                     f"{local(t.get('last_at'))}{' (outdated)' if t.get('outdated') else ''}: "
                     f"{t.get('url', '')}")
    for c in row.get("unanswered_comments") or []:
        facts.append(f"Comment from {c['author']} at {local(c.get('at'))}, after your last "
                     f"activity: {c.get('url', '')}")
    return facts


def _followup_facts(group: dict) -> list[str]:
    facts = []
    for job in group["jobs"]:
        where = f"{job.get('path')}:{job.get('line')}" if job.get("path") else "a thread"
        verdict = (job.get("verdict") or job["status"]).replace("_", " ")
        facts.append(f"Your comment on {where} ({job.get('my_comment_url', '')}): "
                     f"{job.get('actor')} moved at {local(job.get('created_at'))} "
                     f"({job.get('trigger')}); verdict: {verdict}.")
        if job.get("report"):
            facts.append(f"  Follow-up report: {job['report']}")
        if job.get("reply_file"):
            facts.append(f"  Drafted reply: {job['reply_file']}")
        if job.get("reapproval"):
            facts.append(f"  Automatic re-approval: {job['reapproval']}")
    return facts


def entries(sections: dict, data: dict) -> list[dict]:
    """Every PR the menu shows, as {key, url, title, section, why, facts}."""
    out: list[dict] = []
    reviews = {st.get("pr_url"): st for bucket in (data.get("reviews") or {}).values()
               for st in bucket}
    mine = {r.get("url"): r for r in data.get("mine") or []}

    for g in sections["waiting"]["followups"]:
        pr = g["pr"]
        url = re.sub(r"#.*$", "", g["jobs"][0].get("url", "")) or ""
        out.append({"url": url, "title": pr.get("title", ""), "section": "Waiting on you",
                    "why": "follow-ups on your review comments are ready",
                    "facts": _followup_facts(g) + (_review_facts(reviews[url])
                                                   if url in reviews else [])})
    for row in sections["waiting"]["mine"]:
        out.append({"url": row["url"], "title": row.get("title", ""),
                    "section": "Waiting on you",
                    "why": "your PR has a change request you haven't re-requested, an open "
                           "thread, or a comment you haven't answered",
                    "facts": _mine_facts(row)})
    for st in sections["owed"]:
        out.append({"url": st["pr_url"], "title": st.get("title", ""),
                    "section": "Reviews you owe", "why": st.get("reason", ""),
                    "facts": _review_facts(st)})
    for reason, item in sections["other"]:
        url = item.get("pr_url") or item.get("url") or ""
        if url in mine:
            facts = _mine_facts(mine[url])
        elif url in reviews:
            facts = _review_facts(reviews[url])
        else:
            facts = [f"Author: {item.get('author', '?')}. Not a review candidate and not yours."]
        out.append({"url": url, "title": item.get("title", ""),
                    "section": "Everything else (not counted)", "why": reason,
                    "facts": facts})

    seen, unique = set(), []
    for e in out:
        if e["url"] and e["url"] not in seen:
            seen.add(e["url"])
            unique.append({**e, "key": key_of(e["url"])})
    return unique


def render_file(e: dict, generated: str) -> str:
    lines = [f"# {e['key']} — {e['title']}", "", e["url"], "",
             f"Status as of {generated}.", "",
             f"**Menu section:** {e['section']}", f"**Why:** {e['why']}", "", "## Facts", ""]
    lines += [f"- {f}" if not f.startswith("  ") else f"  - {f.strip()}" for f in e["facts"]]
    return "\n".join(lines) + "\n"


def render_index(es: list[dict], generated: str) -> str:
    lines = [f"# PR status, {generated}", "",
             "One line per PR in the menu. Each links to its own file with the facts.", ""]
    for section in ("Waiting on you", "Reviews you owe", "Everything else (not counted)"):
        group = [e for e in es if e["section"] == section]
        lines += [f"## {section} ({len(group)})", ""]
        lines += [f"- [{e['key']}]({e['key']}.md) — {e['title']} — {e['why']}" for e in group]
        lines.append("")
    return "\n".join(lines)


def write(sections: dict, data: dict, directory: Path = STATUS_DIR) -> Path:
    generated = local(datetime.now().astimezone().isoformat())
    es = entries(sections, data)
    directory.mkdir(parents=True, exist_ok=True)
    keep = {"index.md"}
    for e in es:
        name = f"{e['key']}.md"
        (directory / name).write_text(render_file(e, generated), encoding="utf-8")
        keep.add(name)
    (directory / "index.md").write_text(render_index(es, generated), encoding="utf-8")
    for old in directory.glob("*.md"):
        if old.name not in keep:
            old.unlink(missing_ok=True)
    return directory / "index.md"


def path_for(url: str, directory: Path = STATUS_DIR) -> Path:
    return directory / f"{key_of(url)}.md"
