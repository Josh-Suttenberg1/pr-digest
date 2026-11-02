"""Turn watcher events into notification text. Pure.

Title says who did what, subtitle names the PR (ticket key + short title), body is
the first line of the comment or the count, and the click opens the exact comment.
Approvals, change requests and follow-up verdicts are alerts (they stay until
dismissed); comments and replies are banners.
"""

from __future__ import annotations

import re

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
        extra = f" · {_plural(event['count'], 'comment')}" if event["count"] else ""
        return {**out, "style": "alert", "title": f"✅ {name} approved your PR",
                "body": f"{event['approvals']} of {required_approvals} approvals{extra}"}
    if kind == "decision":
        lead = f"{_plural(event['count'], 'comment')}: " if event["count"] else ""
        return {**out, "style": "alert", "title": f"✏️ {name} requested changes",
                "body": f"{lead}{event['first_line']}".strip() or "See the review"}
    if kind == "comments":
        title = (f"💬 {name} commented" if event["count"] <= 1
                 else f"💬 {name} left {event['count']} comments")
        return {**out, "style": "banner", "title": title, "body": event["first_line"] or " "}
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
