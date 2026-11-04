"""Each PR's place in the menu: its section, its priority color, and why. Pure.

No network, no files, no clock: the caller passes `now`. The menu, the watcher's
notifications, the status files and the personal readouts all classify through here,
so they never disagree about a PR's color.

Every PR lands in exactly one section, with at most one color:

  Your PRs (`MINE`), every open PR you author:
    red      something is waiting on you (triage/mine.py `waiting`): an open thread
             whose last human comment isn't yours, an unanswered top-level comment, or
             a change request whose reviewer you haven't re-requested since. Nits and
             suggestions count.
    no dot   waiting on reviewers, approved, or a GitHub draft with nothing to answer.

  Follow-ups on your reviews (`FOLLOWUP`), others' PRs you reviewed or commented on,
  where someone responded since your last comment:
    red      the author re-requested your review (whatever your earlier decision), or
             a response landed in one of your blocking threads: a reply, or the code
             under it changed (the watcher's reply / outdated / moved triggers).
    yellow   the only responses are on non-blocking threads.
  A PR you engaged with that has neither (say the author pushed commits that touch none
  of your threads) goes to Everything else: the author is still working.

  Reviews you owe (`OWED`), others' PRs you haven't reviewed, or whose review of yours
  was dismissed:
    yellow   stalled: waiting on you more than `stale_after_days` (calendar hours,
             weekends included, from reviews._waiting_since), fewer than `required`
             approvals, and at most `max_engaged` people other than the author and bots
             have approved or commented.
    green    everything else.

  Everything else (`OTHER`), never colored: the rest, each with the reason.

A comment of yours is blocking when comment_kind tags it blocking, or when it was part
of a CHANGES_REQUESTED review and isn't tagged nit or suggestion (`is_blocking`).

A dismissed row (⌥ Dismiss in the menu) keeps its section, loses its dot and sorts
last, until activity newer than the dismissal arrives.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from triage import comment_kind

RED, YELLOW, GREEN = "red", "yellow", "green"
COLORS = (RED, YELLOW, GREEN)
DOTS = {RED: "🔴", YELLOW: "🟡", GREEN: "🟢"}
LEGEND = "🔴 needs you now · 🟡 waiting a while · 🟢 new"
_RANK = {None: 0, GREEN: 1, YELLOW: 2, RED: 3}

MINE, FOLLOWUP, OWED, OTHER = "mine", "followups", "owed", "other"

# Follow-up jobs that still stand for a response (watcher/followup.py statuses).
ACTIVE_JOBS = ("queued", "running", "ready", "failed")

PARKED_TIER = 2
TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")


def rank(color: str | None) -> int:
    """0 for no dot, then green < yellow < red."""
    return _RANK.get(color, 0)


def dot(color: str | None) -> str:
    return DOTS.get(color, "")


# --- words ------------------------------------------------------------------------


def who(login: str, nick: dict[str, str]) -> str:
    if login.startswith("team:"):
        return login[len("team:"):]
    return nick.get(login, login)


def names(logins, nick: dict[str, str]) -> str:
    """'Woods', 'Joshua and Mykal', 'Gator, DaVinci and Kyler'."""
    out = [who(x, nick) for x in logins]
    if len(out) <= 1:
        return "".join(out)
    return ", ".join(out[:-1]) + " and " + out[-1]


def _unique(items) -> list:
    seen, out = set(), []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def ticket(title: str, url: str) -> str:
    """The PR's ticket key, or repo#number when its title has none."""
    m = TICKET_RE.search(title or "")
    if m:
        return m.group(0)
    parts = (url or "").rstrip("/").split("/")
    return f"{parts[-3]}#{parts[-1]}" if len(parts) >= 3 else url


def _parse(stamp) -> datetime | None:
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def days_waiting(since, now: datetime) -> float:
    then = _parse(since)
    return max(0.0, (now - then).total_seconds() / 86400) if then else 0.0


def ago(since, now: datetime) -> str:
    """'today', 'yesterday' or '3 days ago', by local calendar date."""
    then = _parse(since)
    if not then:
        return "at an unknown time"
    days = (now.astimezone().date() - then.astimezone().date()).days
    if days <= 0:
        return "today"
    return "yesterday" if days == 1 else f"{days} days ago"


# --- blocking ---------------------------------------------------------------------


def is_blocking(kind: str, review_state: str | None) -> bool:
    """Whether a review comment of yours blocks: tagged blocking, or part of a
    CHANGES_REQUESTED review without a nit or suggestion tag."""
    return kind == "blocking" or (review_state == "CHANGES_REQUESTED"
                                  and kind not in ("nit", "suggestion"))


def job_blocking(job: dict) -> bool:
    """A follow-up job's thread is blocking. A job without the `blocking` field falls
    back to the comment's own tag."""
    if "blocking" in job:
        return bool(job["blocking"])
    return comment_kind.kind(job.get("my_comment") or "") == "blocking"


def followup_color(blocking: list[bool], rerequested: bool) -> str | None:
    """The follow-up rule on its own, for the watcher: red for a re-request or any
    response in a blocking thread, yellow for responses only in non-blocking ones."""
    if rerequested or any(blocking):
        return RED
    return YELLOW if blocking else None


# --- Your PRs ---------------------------------------------------------------------


def _asks(row: dict) -> list[dict]:
    """Every comment waiting on you: {author, kind, at}."""
    out = [{"author": t.get("author") or t.get("last_author") or "?",
            "kind": t.get("kind") or "comment", "at": t.get("last_at") or ""}
           for t in row.get("threads_waiting") or []]
    out += [{"author": c.get("author") or "?", "kind": c.get("kind") or "comment",
             "at": c.get("at") or ""} for c in row.get("unanswered_comments") or []]
    return out


def _kinds(kinds: list[str]) -> str:
    """'(3 suggestions, 5 nits)'; '' when every comment is a plain comment."""
    return f" ({comment_kind.phrase(kinds)})" if set(kinds) - {"comment"} else ""


def mine(row: dict, nick: dict[str, str], required: int = 2,
         dismissed_at: str | None = None) -> dict:
    """One of your PRs: {section, color, reason, why, waiting_since, activity_at,
    dismissed}."""
    asks = _asks(row)
    blockers = row.get("blockers") or []
    stamps = [a["at"] for a in asks if a["at"]] + [b.get("at") or "" for b in blockers]
    stamps = [s for s in stamps if s]
    activity = max(stamps, default="")
    waiting = bool(row.get("waiting"))
    dismissed = bool(waiting and dismissed_at and activity <= dismissed_at)

    if waiting:
        cr = _unique(b["author"] for b in blockers)
        authors = _unique(a["author"] for a in asks)
        kinds = _kinds([a["kind"] for a in asks])
        n = len(asks)
        parts, why = [], []
        if cr:
            parts.append(f"{names(cr, nick)} requested changes")
            why.append(f"{names(cr, nick)} requested changes and you haven't "
                       "re-requested them")
        if asks:
            if cr and set(authors) <= set(cr) and len(authors) == 1:
                parts[-1] += f" with {_plural(n, 'comment')}{kinds}"
            elif len(authors) == 1:
                parts.append(f"{who(authors[0], nick)} left {_plural(n, 'comment')}{kinds}")
            else:
                parts.append(f"{_plural(n, 'comment')} to answer from "
                             f"{names(authors, nick)}{kinds}")
            why.append(f"{_plural(n, 'comment')} from {names(authors, nick)} "
                       f"{'is' if n == 1 else 'are'} unanswered")
        reason = " · ".join(parts) or "Something is waiting on you"
        if dismissed:
            return {"section": MINE, "color": None, "reason": f"Dismissed · {reason}",
                    "why": "You dismissed this; it comes back when something new arrives",
                    "waiting_since": min(stamps, default=""), "activity_at": activity,
                    "dismissed": True}
        return {"section": MINE, "color": RED, "reason": reason,
                "why": "Why it's red: " + "; ".join(why),
                "waiting_since": min(stamps, default=""), "activity_at": activity,
                "dismissed": False}

    approvals = row.get("approvals") or []
    requested = row.get("requested") or []
    rereview = _unique([b["author"] for b in row.get("answered_change_requests") or []]
                       + [a["author"] for a in approvals if a["author"] in requested])
    n = len(approvals)
    if rereview:
        reason = f"Waiting on {names(rereview, nick)} to re-review"
    elif n >= required:
        reason = f"Approved, {n} of {required}"
    elif row.get("isDraft"):
        reason = "GitHub draft, nothing to answer"
    else:
        reason = f"Waiting on reviews, {n} of {required} approvals"
    return {"section": MINE, "color": None, "reason": reason, "why": "",
            "waiting_since": "", "activity_at": activity, "dismissed": False}


# --- Follow-ups on your reviews -----------------------------------------------------


def followup(jobs: list[dict], nick: dict[str, str], *, author: str = "",
             rerequested_at: str = "", dismissed_at: str | None = None) -> dict | None:
    """Others' PR you engaged with: the responses since your last comment (the
    watcher's follow-up jobs for the PR, any status) and a pending re-request. None
    when nothing has responded, so the PR isn't a follow-up."""
    active = [j for j in jobs if j.get("status") in ACTIVE_JOBS]
    if not active and not rerequested_at:
        return None
    author = author or ((active[0].get("pr") or {}).get("author") if active else "")
    blocking = [job_blocking(j) for j in active]
    color = followup_color(blocking, bool(rerequested_at))
    stamps = [j.get("created_at") or "" for j in active] + [rerequested_at]
    stamps = [s for s in stamps if s]
    activity = max(stamps, default="")

    parts, why = [], []
    if rerequested_at:
        parts.append(f"{who(author, nick)} re-requested your review")
        why.append(f"{who(author, nick)} re-requested your review")
    if active:
        actors = _unique(j.get("actor") or author for j in active)
        triggers = {j.get("trigger") for j in active}
        verb = ("answered" if triggers == {"reply"}
                else "changed the code under" if "reply" not in triggers else "responded to")
        n, nb = len(active), sum(blocking)
        if n == 1:
            what = "your blocking comment" if nb else "your comment"
        else:
            what = f"{n} of your comments"
            if nb == n:
                what += ", both blocking" if n == 2 else ", all blocking"
            elif nb:
                what += f", {nb} blocking"
        actor = names(actors, nick)
        if rerequested_at and actors == [author]:
            parts[-1] += f" and {verb} {what}"
        else:
            parts.append(f"{actor} {verb} {what}")
        pairs = list(zip(active, blocking))
        repliers = _unique(j.get("actor") or author for j, b in pairs
                           if b and j.get("trigger") == "reply")
        replied = sum(1 for j, b in pairs if b and j.get("trigger") == "reply")
        changed = sum(1 for j, b in pairs if b and j.get("trigger") != "reply")
        if replied:
            why.append(f"{names(repliers, nick)} replied to your blocking comment"
                       + ("s" if replied > 1 else ""))
        if changed:
            why.append("the code changed under your blocking comment"
                       + ("s" if changed > 1 else ""))
        if color == YELLOW:
            why.append("the responses are all on comments that weren't blocking")
    reason = " · ".join(parts)
    dismissed = bool(dismissed_at and activity and activity <= dismissed_at)
    label = {RED: "red", YELLOW: "yellow"}.get(color, "")
    return {"section": FOLLOWUP, "color": None if dismissed else color, "reason":
            (f"Dismissed · {reason}" if dismissed else reason),
            "why": ("You dismissed this; it comes back when something new arrives"
                    if dismissed else f"Why it's {label}: " + "; ".join(why)),
            "waiting_since": min(stamps, default=""), "activity_at": activity,
            "dismissed": dismissed, "jobs": active}


# --- Reviews you owe ----------------------------------------------------------------


def owed(st: dict, nick: dict[str, str], now: datetime, *, required: int = 2,
         stale_after_days: float = 2, max_engaged: int = 1,
         dismissed_at: str | None = None) -> dict:
    """A PR waiting on your first review (or a fresh one after yours was dismissed)."""
    since = st.get("waiting_since") or st.get("created_at") or ""
    engaged = list(st.get("engaged") or [])
    n = len(st.get("approvals") or [])
    waited = days_waiting(since, now)
    stale = waited > stale_after_days and n < required and len(engaged) <= max_engaged
    color = YELLOW if stale else GREEN
    approvals = "no approvals" if not n else _plural(n, "approval")
    dismissed_review = (st.get("mine") or [None])[0] == "DISMISSED"

    if stale:
        reason = f"Waiting {_plural(int(waited), 'day')}, {approvals}"
    else:
        reason = "Requested " + (f"{_plural(int(waited), 'day')} ago" if waited >= 2
                                  else ago(since, now))
        if len(engaged) > max_engaged:
            reason += f" · {names(engaged, nick)} are on it"
    if dismissed_review:
        reason = "Your review was dismissed · " + reason

    if not engaged:
        people = "no one else has reviewed or commented"
    elif len(engaged) == 1:
        people = f"only {who(engaged[0], nick)} has reviewed or commented"
    else:
        people = f"{names(engaged, nick)} have reviewed or commented"
    asked = (f"{_plural(int(waited), 'day')} ago" if waited >= 2 else ago(since, now))
    why = (f"Why it's {'yellow' if stale else 'green'}: requested {asked} · "
           f"{approvals} · {people}")
    dismissed = bool(dismissed_at and since and since <= dismissed_at)
    if dismissed:
        return {"section": OWED, "color": None, "reason": f"Dismissed · {reason}",
                "why": "You dismissed this; it comes back when something new arrives",
                "waiting_since": since, "activity_at": since, "dismissed": True}
    return {"section": OWED, "color": color, "reason": reason, "why": why,
            "waiting_since": since, "activity_at": since, "dismissed": False}


# --- everything else ------------------------------------------------------------------


def parked_reason(st: dict) -> str:
    parked = [t for t in st.get("tags") or []
              if t.startswith("CI failing") or "approvals already" in t]
    return "parked — " + ("; ".join(parked) or "your review can't unblock it yet")


def other_reason(st: dict) -> str:
    """Why a review candidate isn't counted."""
    bucket = st.get("bucket")
    reason = st.get("reason", "")
    if st.get("unreadable"):
        return "couldn't read it from GitHub; it will be retried"
    if bucket == "needs_you":
        if st.get("tier") == PARKED_TIER:
            return parked_reason(st)
        if "commits landed" in reason or "pushed since" in reason:
            return ("the author pushed commits that touch none of your comments and "
                    "hasn't re-requested you; still working — " + reason)
        return ("not a reply in one of your threads or a re-request — " + reason)
    return {"author": "waiting on the author — ",
            "well_approved": "enough approvals — ",
            "done": "you approved — "}.get(bucket, "") + reason


# --- the whole menu ------------------------------------------------------------------


def classify(mine_rows: list[dict], reviews: dict[str, list[dict]], jobs: list[dict], *,
             nick: dict[str, str], now: datetime, required: int = 2,
             stale_after_days: float = 2, max_engaged: int = 1,
             dismissed: dict[str, str] | None = None) -> list[dict]:
    """One entry per PR: {url, title, section, color, reason, why, waiting_since,
    activity_at, dismissed, row | st, jobs}. `reviews` is review_triage's buckets
    (needs_you in ranked order); `jobs` every follow-up job; `dismissed` url → the
    activity time it was dismissed at."""
    dismissed = dismissed or {}
    out: list[dict] = []
    for row in mine_rows:
        p = mine(row, nick, required, dismissed.get(row["url"]))
        out.append({**p, "url": row["url"], "title": row.get("title", ""), "row": row,
                    "st": None, "jobs": []})

    by_url: dict[str, list[dict]] = {}
    for j in jobs:
        url = ((j.get("pr") or {}).get("url") or "").rstrip("/")
        if url:
            by_url.setdefault(url, []).append(j)
    placed = {e["url"] for e in out}

    for bucket in ("needs_you", "author", "well_approved", "done", "unreadable"):
        for st in reviews.get(bucket) or []:
            url = st.get("pr_url", "")
            if not url or url in placed:
                continue
            placed.add(url)
            base = {"url": url, "title": st.get("title", ""), "row": None, "st": st}
            if st.get("unreadable"):
                out.append({**base, "section": OTHER, "color": None, "jobs": [],
                            "reason": other_reason(st), "why": "", "waiting_since": "",
                            "activity_at": "", "dismissed": False})
                continue
            f = followup(by_url.get(url, []), nick, author=st.get("author", ""),
                         rerequested_at=st.get("rerequested_at") or "",
                         dismissed_at=dismissed.get(url))
            mine_state = (st.get("mine") or [None])[0]
            fresh = not (st.get("mine") or st.get("commented")) or mine_state == "DISMISSED"
            if f:
                out.append({**base, **f})
            elif (fresh and st.get("bucket") == "needs_you"
                  and st.get("tier") != PARKED_TIER):
                out.append({**base, "jobs": [], **owed(
                    st, nick, now, required=required, stale_after_days=stale_after_days,
                    max_engaged=max_engaged, dismissed_at=dismissed.get(url))})
            else:
                out.append({**base, "section": OTHER, "color": None, "jobs": [],
                            "reason": other_reason(st), "why": "", "waiting_since": "",
                            "activity_at": "", "dismissed": False})

    # Responses on a PR the review triage didn't return (it closed between fetches, or
    # the search missed it) still show as follow-ups.
    for url, js in by_url.items():
        if url in placed:
            continue
        f = followup(js, nick, dismissed_at=dismissed.get(url))
        if f:
            placed.add(url)
            out.append({**f, "url": url, "title": (js[0].get("pr") or {}).get("title", ""),
                        "row": None, "st": None})
    return out


def counts(entries: list[dict]) -> dict[str, int]:
    """How many PRs carry each color."""
    return {c: sum(1 for e in entries if e.get("color") == c) for c in COLORS}


def title(entries: list[dict]) -> str:
    """The menu-bar title: '🔴2 🟡1 🟢2', omitting colors with none; '✓' when empty."""
    n = counts(entries)
    return " ".join(f"{DOTS[c]}{n[c]}" for c in COLORS if n[c]) or "✓"


def sort_key(entry: dict, order: int = 0) -> tuple:
    """Red, then yellow, then green, then no dot; inside a color the longest wait
    first (or `order`, the caller's ranking, for Reviews you owe)."""
    return (-rank(entry.get("color")), order, entry.get("waiting_since") or "~")
