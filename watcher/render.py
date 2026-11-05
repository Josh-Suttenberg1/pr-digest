"""Turn watcher events into notification text. Pure.

A PR gets one notification per tick, at its priority color (triage/priority.py):

  title     the dot, the ticket and the PR's reason, e.g.
            "🔴 ABC-123 · Mykal answered 2 of your comments, 1 blocking"
  subtitle  the PR title without its ticket key
  body      what happened this tick, in words ("Woods: Rename `t` to `total`")

The caller picks the click target (watcher/__main__.py notification_href), always
something that already exists. Watcher health notices (can't reach GitHub, claude
logged out) keep their own plain banners.
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


def _event_line(event: dict, nicknames: dict[str, str]) -> str:
    """One event in words; '' for one that adds nothing (a bare approval)."""
    name = who(event.get("actor") or "", nicknames)
    kind = event["type"]
    from triage import comment_kind
    line = comment_kind.summary(event.get("first_line") or "", 120)
    if kind == "decision" and event["state"] == "CHANGES_REQUESTED":
        return f"{name} requested changes" + (f": {line}" if line else "")
    if kind in ("decision", "comments"):
        return f"{name}: {line}" if line and event.get("count") else ""
    if kind == "thread":
        if event["trigger"] == "reply":
            return f"{name} replied: {line}" if line else f"{name} replied"
        where = event.get("path") or "the file"
        return f"the code changed under your comment on {where}"
    if kind == "rerequest":
        return f"{name} re-requested your review"
    if kind == "mention":
        return f"{name} mentioned you: {line}" if line else f"{name} mentioned you"
    return ""


def render_pr(pr: dict, color: str, reason: str, events: list[dict],
              nicknames: dict[str, str], href: str) -> dict:
    """The one notification for a PR this tick: {style, color, title, subtitle, body,
    href, group}. `href` is chosen by the caller and always exists already."""
    from triage import priority
    key = priority.ticket(pr["title"], pr["url"])
    rest = priority.TICKET_RE.sub("", pr["title"]).strip(" []:·-")
    if len(rest) > TITLE_MAX * 2:
        rest = rest[:TITLE_MAX * 2 - 1] + "…"
    lines = [x for x in (_event_line(e, nicknames) for e in events) if x]
    body = " · ".join(dict.fromkeys(lines))
    if len(body) > 200:
        body = body[:199] + "…"
    return {"style": "alert", "color": color,
            "title": f"{priority.dot(color)} {key} · {reason}",
            "subtitle": rest or key, "body": body or " ", "href": href,
            "group": f"pr-{pr['repo']}-{pr['number']}"}


def render(event: dict, nicknames: dict[str, str], required_approvals: int = 2) -> dict:
    """A watcher notice (type "notice"): {"style", "title", "subtitle", "body", "href",
    "group"}."""
    if event["type"] != "notice":
        raise ValueError(f"PR events go through render_pr, not {event['type']}")
    return {"style": event.get("style", "banner"), "title": event["title"],
            "subtitle": event.get("subtitle", ""), "body": event.get("body", " "),
            "href": event.get("url"), "group": "pr-digest-watcher"}
