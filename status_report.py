"""Plain-text status of every PR the menu knows about, for a Claude session to read.

Written by each menu fetch to output/status/:
  index.md         one line per PR: where the menu puts it, its priority dot, and why
  <repo>#<n>.md    one file per PR: section, priority (triage/priority.py), reason,
                   and the facts the reason rests on
                   (change requests and whether you re-requested, open threads, your
                   last review, approvals, drafts and follow-up reports, with links).
                   For your own PRs it ends with "Comments to answer": the full text of
                   each comment waiting on you, tagged nit / suggestion / question.

A session asked about a PR reads its file instead of re-deriving the history from
GitHub. Files for PRs no longer in the menu are removed on the next write. Times are
local, with the zone abbreviation.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import settings

STATUS_DIR = settings.OUTPUT_DIR / "status"


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


def _name(login: str, nick: dict[str, str]) -> str:
    """'Woods (plaxdan)' when there's a nickname, else the login."""
    if login.startswith("team:"):
        return f"the {login[len('team:'):]} team"
    return f"{nick[login]} ({login})" if login in nick else login


def _mine_facts(row: dict, nick: dict[str, str] | None = None,
                required: int = 2) -> list[str]:
    nick = nick or {}
    facts = ["Your PR." + (" It is a GitHub draft." if row.get("isDraft") else "")]
    approvals = row.get("approvals") or []
    if approvals:
        facts.append(f"Approvals: {len(approvals)} of {required}.")
        for a in approvals:
            facts.append(f"  {_name(a['author'], nick)} approved at {local(a.get('at'))} "
                         f"({a.get('ordinal')} of {required}): {a.get('url', '')}")
    else:
        facts.append(f"Approvals: 0 of {required}.")
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
    decided = {a["author"] for a in approvals}
    decided |= {b["author"] for b in (row.get("blockers") or [])
                + (row.get("answered_change_requests") or [])}
    pending = [r for r in row.get("requested") or [] if r not in decided]
    if pending:
        facts.append("Review requested, none given yet: "
                     + ", ".join(_name(r, nick) for r in pending) + ".")
    return facts


def _mine_comments(row: dict, nick: dict[str, str] | None = None) -> list[str]:
    """The full text of every comment waiting on you, tagged by kind, most
    decision-heavy first: the section a session works through."""
    from triage import comment_kind as ck
    nick = nick or {}
    items = []
    for t in row.get("threads_waiting") or []:
        author = t.get("author") or t.get("last_author", "?")
        ask = next((b["body"] for b in t.get("bodies") or [] if b["author"] == author), "")
        items.append((t.get("kind") or ck.kind(ask), author, t.get("path") or "",
                      t.get("url", ""), t.get("bodies") or []))
    for c in row.get("unanswered_comments") or []:
        items.append((c.get("kind") or ck.kind(c.get("body", "")), c.get("author", "?"), "",
                      c.get("url", ""), [{"author": c.get("author", "?"),
                                          "body": c.get("body", "")}]))
    items.sort(key=lambda i: ck.sort_key(i[0]))
    out = []
    for n, (kind, author, path, url, bodies) in enumerate(items, 1):
        where = f" on {path}" if path else " (top-level comment)"
        label = ck.LABELS.get(kind, kind).split(" ", 1)[-1]
        out += [f"### {n}. {label} from {_name(author, nick)}{where}", "", url, ""]
        for b in bodies:
            if len(bodies) > 1:
                out.append(f"**{_name(b['author'], nick)}:**")
            out += [(b.get("body") or "").strip(), ""]
    return out


def _followup_facts(group: dict) -> list[str]:
    facts = []
    for job in group["jobs"]:
        if job.get("trigger") == "mention":
            facts.append(f"{job.get('actor')} mentioned you at {local(job.get('mentioned_at'))} "
                         f"({job.get('url', '')}); status: {job['status']}.")
            if job.get("reply_file"):
                facts.append(f"  Drafted reply: {job['reply_file']}")
            continue
        where = f"{job.get('path')}:{job.get('line')}" if job.get("path") else "a thread"
        verdict = (job.get("verdict") or job["status"]).replace("_", " ")
        facts.append(f"Your {'blocking ' if job.get('blocking') else ''}comment on {where} "
                     f"({job.get('my_comment_url', '')}): {job.get('actor')} moved at "
                     f"{local(job.get('created_at'))} ({job.get('trigger')}); verdict: {verdict}.")
        if job.get("report"):
            facts.append(f"  Follow-up report: {job['report']}")
        if job.get("reply_file"):
            facts.append(f"  Drafted reply: {job['reply_file']}")
        if job.get("reapproval"):
            facts.append(f"  Automatic re-approval: {job['reapproval']}")
    return facts


from triage import priority as P  # noqa: E402

SECTIONS = tuple(P.SECTION_NAMES[s] for s in (P.MINE, P.FOLLOWUP, P.OWED, P.OTHER))

PRIORITY_WORDS = {"red": "🔴 red (needs you now)", "yellow": "🟡 yellow (waiting a while)",
                  "green": "🟢 green (new)", "low": "⚪ low (no action needed)"}


def _priority(e: dict) -> str:
    if e.get("dismissed"):
        return "none (you dismissed it; it comes back when something new arrives)"
    return PRIORITY_WORDS.get(e.get("color"), "none")


def _dot(e: dict) -> str:
    """'🔴 ' for a colored PR's index line, '' otherwise."""
    return "" if e["priority"].startswith("none") else e["priority"].split(" ")[0] + " "


def entries(sections: dict, data: dict, nick: dict[str, str] | None = None,
            required: int = 2) -> list[dict]:
    """Every PR the menu shows, as {key, url, title, section, priority, why, facts,
    comments}. `sections` is swiftbar_plugin.build_sections' result."""
    out: list[dict] = []
    reviews = {st.get("pr_url"): st for bucket in (data.get("reviews") or {}).values()
               for st in bucket}

    def why(e: dict) -> str:
        return e["reason"] + (f". {e['why']}" if e.get("why") else "")

    for e in sections["mine"] + sections["mine_drafts"]:
        row = e["row"]
        out.append({"url": row["url"], "title": row.get("title", ""), "section": "Your PRs",
                    "priority": _priority(e), "why": why(e),
                    "facts": _mine_facts(row, nick, required),
                    "comments": _mine_comments(row, nick)})
    for e in sections["followups"]:
        url = e["url"]
        st = e.get("st") or reviews.get(url)
        facts = []
        if st and st.get("rerequested_at"):
            facts.append(f"Your review was re-requested at {local(st['rerequested_at'])} "
                         "and you haven't reviewed since.")
        facts += _followup_facts({"jobs": e.get("jobs") or []})
        facts += _review_facts(st) if st else []
        out.append({"url": url, "title": e.get("title", ""),
                    "section": P.SECTION_NAMES[P.FOLLOWUP], "priority": _priority(e),
                    "why": why(e), "facts": facts})
    for e in sections["owed"]:
        st = e["st"]
        out.append({"url": st["pr_url"], "title": st.get("title", ""),
                    "section": "Reviews you owe", "priority": _priority(e),
                    "why": why(e) + f" (triage: {st.get('reason', '')})",
                    "facts": _review_facts(st)})
    for e in sections["other"]:
        url = e["url"]
        if url in reviews:
            facts = _review_facts(reviews[url])
        else:
            facts = [f"Author: {(e.get('st') or {}).get('author', '?')}. "
                     "Not a review candidate and not yours."]
        out.append({"url": url, "title": e.get("title", ""),
                    "section": P.SECTION_NAMES[P.OTHER], "priority": _priority(e),
                    "why": why(e), "facts": facts})

    seen, unique = set(), []
    for e in out:
        if e["url"] and e["url"] not in seen:
            seen.add(e["url"])
            unique.append({**e, "key": key_of(e["url"])})
    return unique


def render_file(e: dict, generated: str) -> str:
    lines = [f"# {e['key']} — {e['title']}", "", e["url"], "",
             f"Status as of {generated}.", "",
             f"**Menu section:** {e['section']}", f"**Priority:** {e['priority']}",
             f"**Why:** {e['why']}", "", "## Facts", ""]
    lines += [f"- {f}" if not f.startswith("  ") else f"  - {f.strip()}" for f in e["facts"]]
    if e.get("comments"):
        lines += ["", "## Comments to answer", ""] + e["comments"]
    return "\n".join(lines).rstrip() + "\n"


def render_index(es: list[dict], generated: str) -> str:
    lines = [f"# PR status, {generated}", "",
             "One line per PR in the menu. Each links to its own file with the facts.", ""]
    for section in SECTIONS:
        group = [e for e in es if e["section"] == section]
        lines += [f"## {section} ({len(group)})", ""]
        lines += [f"- {_dot(e)}[{e['key']}]({e['key']}.md) — {e['title']} — {e['why']}"
                  for e in group]
        lines.append("")
    return "\n".join(lines)


def write(sections: dict, data: dict, nick: dict[str, str] | None = None,
          directory: Path = STATUS_DIR, required: int = 2) -> Path:
    generated = local(datetime.now().astimezone().isoformat())
    es = entries(sections, data, nick, required)
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
