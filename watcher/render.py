"""Turn watcher events into notification text. Pure.

Title says who did what, subtitle names the PR (ticket key + short title), body says
what's left for you. Approvals, change requests and follow-up verdicts are alerts
(they stay until dismissed); comments and replies are banners.

On your own PRs an approval's title carries its place in line ("✅ Woods approved
your PR · 2/2"), and the body counts the comments to answer by kind ("8 comments to
answer: 3 suggestions, 5 nits") and names any change request still open, so an
approval never reads as done while one stands. When a notification leaves you
something to answer, clicking it opens a Claude session on the PR's comments
(session.respond_url); otherwise it opens the review on GitHub.
"""

from __future__ import annotations

import re

from triage import comment_kind

TICKET_RE = re.compile(r"\b(?:CONN|WS)-\d+\b")
TITLE_MAX = 40


def who(login: str, nicknames: dict[str, str]) -> str:
    return nicknames.get(login, login)


def subtitle(pr: dict) -> str:
    title = pr["title"]
    m = TICKET_RE.search(title)
    key = m.group(0) if m else f"{pr['repo']}#{pr['number']}"
    rest = TICKET_RE.sub("", title).strip(" []:·-")
    if len(rest) > TITLE_MAX:
        rest = rest[:TITLE_MAX - 1] + "…"
    return f"{key} · {rest}" if rest else key


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _answer_phrase(event: dict) -> str:
    """'8 comments to answer: 3 suggestions, 5 nits'; '' when there are none."""
    n = event.get("count") or 0
    if not n:
        return ""
    kinds = event.get("kinds") or []
    detail = comment_kind.phrase(kinds) if set(kinds) - {"comment"} else ""
    return f"{_plural(n, 'comment')} to answer" + (f": {detail}" if detail else "")


def _still_open(event: dict, nicknames: dict[str, str]) -> str:
    """"Mykal's change request is still open"; '' when none is."""
    names = [who(a, nicknames) for a in event.get("changes_requested_by") or []]
    if not names:
        return ""
    if len(names) == 1:
        return f"{names[0]}'s change request is still open"
    return f"{', '.join(names[:-1])} and {names[-1]}'s change requests are still open"


def _respond(pr: dict) -> str:
    import session
    return session.respond_url(pr["url"])


def render(event: dict, nicknames: dict[str, str], required_approvals: int = 2) -> dict:
    """{"style": alert|banner, "title", "subtitle", "body", "href", "group"}."""
    if event["type"] == "notice":
        return {"style": event.get("style", "banner"), "title": event["title"],
                "subtitle": event.get("subtitle", ""), "body": event.get("body", " "),
                "href": event.get("url"), "group": "pr-digest-watcher"}
    name = who(event.get("actor") or "", nicknames)
    pr = event["pr"]
    out = {"subtitle": subtitle(pr), "href": event.get("url") or pr["url"],
           "group": f"pr-{pr['repo']}-{pr['number']}-{event.get('actor')}"}
    kind = event["type"]
    if kind == "decision" and event["state"] == "APPROVED":
        answer = _answer_phrase(event)
        body = " · ".join(x for x in (answer or "No comments", _still_open(event, nicknames)) if x)
        return {**out, "style": "alert",
                "title": f"✅ {name} approved your PR · {event['approvals']}/{required_approvals}",
                "body": body, "href": _respond(pr) if answer else out["href"]}
    if kind == "decision":
        answer = _answer_phrase(event)
        body = answer or event["first_line"] or "See the review"
        return {**out, "style": "alert", "title": f"✏️ {name} requested changes",
                "body": body, "href": _respond(pr)}
    if kind == "comments":
        title = (f"💬 {name} commented" if event["count"] <= 1
                 else f"💬 {name} left {event['count']} comments")
        kinds = comment_kind.phrase(event.get("kinds") or [])
        lead = f"{kinds} — " if event["count"] > 1 and kinds else ""
        return {**out, "style": "banner", "title": title,
                "body": f"{lead}{event['first_line']}".strip() or " ", "href": _respond(pr)}
    if kind == "thread":
        if event["trigger"] == "reply":
            title = f"↩️ {name} replied to your comment"
            body = event["first_line"] or " "
        else:
            title = f"🔧 {name} changed the code under your comment"
            body = f"{event['path']}" + (f":{event['line']}" if event.get("line") else "")
        return {**out, "style": "banner", "title": title, "body": body}
    if kind == "verdict":
        verdict = {"addressed": "addressed your comment", "partly": "partly addressed your comment",
                   "not_addressed": "didn't address your comment"}.get(event["verdict"], event["verdict"])
        return {**out, "style": "alert", "title": f"🔎 {name} {verdict}",
                "body": "Reply drafted — review it in the menu bar"}
    raise ValueError(f"unknown event type {kind}")
