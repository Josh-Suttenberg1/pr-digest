"""Turn watcher events into notification text. Pure.

A PR gets one notification per tick, at its priority color (triage/priority.py):

  title     the dot and what set it off, naming who and the ticket, e.g.
            "🟡 Mykal approved ABC-123" (`trigger`)
  subtitle  the PR title without its ticket key
  body      where the PR stands now, with its approval tally (the priority reason),
            then what was said this tick ("Woods: Rename `t` to `total`")

The caller picks the click target (watcher/__main__.py notification_href), always
something that already exists. Watcher health notices (can't reach GitHub, claude
logged out) keep their own plain banners.
"""

from __future__ import annotations

from triage.priority import TICKET_RE

TITLE_MAX = 40


def who(login: str, people: dict[str, str]) -> str:
    return people.get(login, login)


def subtitle(pr: dict) -> str:
    title = pr["title"]
    m = TICKET_RE.search(title)
    key = m.group(0) if m else f"{pr['repo']}#{pr['number']}"
    rest = TICKET_RE.sub("", title).strip(" []:·-")
    if len(rest) > TITLE_MAX:
        rest = rest[:TITLE_MAX - 1] + "…"
    return f"{key} · {rest}" if rest else key


def _event_line(event: dict, people: dict[str, str]) -> str:
    """One event in words; '' for one that adds nothing (a bare approval)."""
    name = who(event.get("actor") or "", people)
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
    if kind == "change_request":
        return f"{name}: {line}" if line else f"{name} pushed commits"
    return ""


# Which event names the notification when a tick brings several: the one that most
# needs an answer first.
_TRIGGER_ORDER = {"CHANGES_REQUESTED": 0, "mention": 1, "rerequest": 2,
                  "change_request": 3, "thread": 4, "comments": 5, "APPROVED": 6}


def _trigger_rank(event: dict) -> tuple[int, bool]:
    """Sort key: the event type's place in _TRIGGER_ORDER, then a response under one of
    your blocking comments before one under a comment that isn't."""
    kind = event.get("state") if event["type"] == "decision" else event["type"]
    return _TRIGGER_ORDER.get(kind, 9), not event.get("blocking")


def _comments(n: int) -> str:
    return f"{n} comment" + ("" if n == 1 else "s")


def _trigger(event: dict, people: dict[str, str], key: str) -> str:
    """One event as who did what to which ticket: 'Mykal approved ABC-1'; '' for an
    event type it doesn't know."""
    name = who(event.get("actor") or "", people)
    kind, n = event["type"], event.get("count") or 0
    with_comments = f" with {_comments(n)}" if n else ""
    if kind == "decision" and event.get("state") == "APPROVED":
        return f"{name} approved {key}{with_comments}"
    if kind == "decision" and event.get("state") == "CHANGES_REQUESTED":
        return f"{name} requested changes on {key}{with_comments}"
    if kind == "comments":
        return f"{name} left {_comments(n or 1)} on {key}"
    if kind == "thread":
        if event.get("trigger") == "reply":
            return f"{name} replied to your comment on {key}"
        return f"{name} changed the code under your comment on {key}"
    if kind == "rerequest":
        return f"{name} re-requested your review on {key}"
    if kind == "mention":
        return f"{name} mentioned you on {key}"
    if kind == "change_request":
        if event.get("trigger") == "pushed":
            return f"{name} pushed commits since your change request on {key}"
        return f"{name} answered your change request on {key}"
    return ""


def trigger(events: list[dict], people: dict[str, str], key: str) -> str:
    """What set the notification off: the most pressing event (_TRIGGER_ORDER) in
    words, plus how many more came with it; the ticket alone when there's none."""
    said = [t for t in (_trigger(e, people, key)
                        for e in sorted(events, key=_trigger_rank)) if t]
    if not said:
        return key
    return said[0] + (f" (+{len(said) - 1} more)" if len(said) > 1 else "")


def render_pr(pr: dict, color: str, reason: str, events: list[dict],
              people: dict[str, str], href: str) -> dict:
    """The one notification for a PR this tick: {style, color, title, subtitle, body,
    href, group}. The title says what happened (`trigger`), the subtitle is the PR's
    title, and the body says where the PR stands now (`reason`, which carries its
    approval tally), then what was said. `href` is chosen by the caller and always
    exists already."""
    from triage import priority
    key = priority.ticket(pr["title"], pr["url"])
    rest = priority.TICKET_RE.sub("", pr["title"]).strip(" []:·-")
    if len(rest) > TITLE_MAX * 2:
        rest = rest[:TITLE_MAX * 2 - 1] + "…"
    lines = [x for x in (_event_line(e, people) for e in events) if x]
    body = " · ".join([reason] + list(dict.fromkeys(lines))) if reason else " · ".join(
        dict.fromkeys(lines))
    if len(body) > 200:
        body = body[:199] + "…"
    return {"style": "alert", "color": color,
            "title": f"{priority.dot(color)} {trigger(events, people, key)}",
            "subtitle": rest or key, "body": body or " ", "href": href,
            "group": f"pr-{pr['repo']}-{pr['number']}"}


def render(event: dict, people: dict[str, str], required_approvals: int = 2) -> dict:
    """A watcher notice (type "notice"): {"style", "title", "subtitle", "body", "href",
    "group"}."""
    if event["type"] != "notice":
        raise ValueError(f"PR events go through render_pr, not {event['type']}")
    return {"style": event.get("style", "banner"), "title": event["title"],
            "subtitle": event.get("subtitle", ""), "body": event.get("body", " "),
            "href": event.get("url"), "group": "pr-digest-watcher"}
