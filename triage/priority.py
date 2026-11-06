"""Each PR's place in the menu: its section, its priority color, and why. Pure.

No network, no files, no clock: the caller passes `now` (mine() falls back to the
current time when it isn't given). The menu, the watcher's notifications, the status
files and the personal readouts all classify through here, so they never disagree
about a PR's color.

Every PR lands in exactly one section, with at most one color:

  My PRs (`MINE`), every open PR you author, under one of three sub-headers (`group`):
    Needs you
      red      a change request whose reviewer you haven't re-requested since, or a
               comment to answer (an open thread whose last human comment isn't yours,
               or an unanswered top-level comment) that isn't a nit or a suggestion.
      yellow   only nits and suggestions are waiting; or the PR needs re-approval:
               it has `required` approvals, but fewer than that on the head commit.
    Waiting on others
      no dot   waiting on a re-review (a reviewer you re-requested after their change
               request or approval) or on a review, naming who and since when.
      yellow   the same, once the wait passes `stale_after_days`: a cue to nudge.
      no dot   a GitHub draft with nothing to answer, or no reviewer requested.
    Ready
      no dot   `required` approvals on the head commit, nothing to answer, no change
               request standing.

  Follow-ups and mentions (`FOLLOWUP`), others' PRs you reviewed or commented on where
  someone responded since your last comment, and any PR where someone @mentioned you:
    red      someone @mentioned you; the author re-requested your review (whatever
             your earlier decision); or a response landed in one of your blocking
             threads: a reply, or the code under it changed (the watcher's reply /
             outdated / moved triggers).
    yellow   the only responses are on non-blocking threads.
  A PR you engaged with that has none of these (say the author pushed commits that
  touch none of your threads, or commented without mentioning you) goes to Non action
  requiring updates: the author is still working.

  Reviews you owe (`OWED`), others' PRs you haven't reviewed, or whose review of yours
  was dismissed:
    yellow   stalled: waiting on you more than `stale_after_days` (calendar hours,
             weekends included, from reviews._waiting_since), fewer than `required`
             approvals, and at most `max_engaged` people other than the author and bots
             have approved or commented.
    green    everything else.

  Non action requiring updates (`OTHER`), always ⚪ low: the rest, each with the
  reason in words (still working, waiting on the author, parked, approved, drafts).
  Never counted and never notified.

A comment of yours is blocking when comment_kind tags it blocking, or when it was part
of a CHANGES_REQUESTED review and isn't tagged nit or suggestion (`is_blocking`).

A dismissed row (⌥ Dismiss in the menu) keeps its section, loses its dot and sorts
last, until activity newer than the dismissal arrives.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from triage import comment_kind

RED, YELLOW, GREEN, LOW = "red", "yellow", "green", "low"
# The colors the menu-bar title counts; LOW never counts and never notifies.
COLORS = (RED, YELLOW, GREEN)
DOTS = {RED: "🔴", YELLOW: "🟡", GREEN: "🟢", LOW: "⚪"}
LEGEND = "🔴 act now · 🟡 act soon · 🟢 new · ⚪ no action needed"
_RANK = {None: 0, LOW: 0, GREEN: 1, YELLOW: 2, RED: 3}

MINE, FOLLOWUP, OWED, OTHER = "mine", "followups", "owed", "other"
SECTION_NAMES = {MINE: "My PRs", FOLLOWUP: "Follow-ups and mentions",
                 OWED: "Reviews you owe", OTHER: "Non action requiring updates"}

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
    """'Ada', 'Ada and Alan', 'Ada, Alan and Grace'."""
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


def followup_color(blocking: list[bool], rerequested: bool,
                   mentioned: bool = False) -> str | None:
    """The follow-up rule on its own: red for a mention, a re-request or any response
    in a blocking thread, yellow for responses only in non-blocking ones."""
    if mentioned or rerequested or any(blocking):
        return RED
    return YELLOW if blocking else None


# --- Your PRs ---------------------------------------------------------------------

# My PRs' sub-headers, in menu order.
NEEDS, WAITING, READY = "needs_you", "waiting", "ready"
MINE_GROUPS = {NEEDS: "Needs you", WAITING: "Waiting on others", READY: "Ready"}
MINE_GROUP_ORDER = {NEEDS: 0, WAITING: 1, READY: 2}
# Comments on your PR that only make it yellow; any other kind makes it red.
YELLOW_KINDS = frozenset({"nit", "suggestion"})


def _asks(row: dict) -> list[dict]:
    """Every comment waiting on you: {author, kind, at}."""
    out = [{"author": t.get("author") or t.get("last_author") or "?",
            "kind": t.get("kind") or "comment", "at": t.get("last_at") or ""}
           for t in row.get("threads_waiting") or []]
    out += [{"author": c.get("author") or "?", "kind": c.get("kind") or "comment",
             "at": c.get("at") or ""} for c in row.get("unanswered_comments") or []]
    return out


def age(since, now: datetime) -> str:
    """'today' or '5d', by local calendar date; '' when unknown."""
    then = _parse(since)
    if not then:
        return ""
    days = (now.astimezone().date() - then.astimezone().date()).days
    return "today" if days <= 0 else f"{days}d"


def _with_age(text: str, since, now: datetime) -> str:
    a = age(since, now)
    return f"{text} · {a}" if a else text


def _possessive(login: str, nick: dict[str, str]) -> str:
    return who(login, nick) + "'s"


def mine(row: dict, nick: dict[str, str], required: int = 2,
         dismissed_at: str | None = None, *, now: datetime | None = None,
         stale_after_days: float = 2) -> dict:
    """One of your PRs: {section, group, color, reason, why, waiting_since,
    activity_at, dismissed}. `now` defaults to the current time."""
    now = now or datetime.now(timezone.utc)
    if row.get("waiting"):
        p = _mine_needs_you(row, nick)
    else:
        p = _mine_not_on_you(row, nick, required, now, stale_after_days)
    # A row that waits on someone ends with how long; Ready, drafts and "no reviewer
    # requested" have nothing to age (`aged` False).
    reason = (_with_age(p["reason"], p["waiting_since"], now) if p.pop("aged", True)
              else p["reason"])
    activity = p["activity_at"]
    dismissed = bool(p["color"] and dismissed_at and activity and activity <= dismissed_at)
    if dismissed:
        return {**p, "section": MINE, "color": None, "reason": f"Dismissed · {reason}",
                "why": "You dismissed this; it comes back when something new arrives",
                "dismissed": True}
    return {**p, "section": MINE, "reason": reason, "dismissed": False}


def _mine_needs_you(row: dict, nick: dict[str, str]) -> dict:
    """Something on the PR is waiting on you: red for a change request or any comment
    that isn't a nit or a suggestion, yellow when only nits and suggestions wait."""
    asks = _asks(row)
    blockers = row.get("blockers") or []
    stamps = [a["at"] for a in asks if a["at"]] + [b.get("at") or "" for b in blockers]
    stamps = [x for x in stamps if x]
    cr = _unique(b["author"] for b in blockers)
    authors = _unique(a["author"] for a in asks)
    kinds = [a["kind"] for a in asks]
    n = len(asks)
    red = bool(cr) or not kinds or any(k not in YELLOW_KINDS for k in kinds)

    parts, why = [], []
    if cr:
        parts.append(f"Answer {_possessive(cr[0], nick)} change request" if len(cr) == 1
                     else f"Answer change requests from {names(cr, nick)}")
        why.append(f"{names(cr, nick)} requested changes and you haven't re-requested them")
    if asks:
        if cr and set(authors) <= set(cr) and len(authors) == 1:
            parts[-1] += f": {comment_kind.phrase(kinds)}"
        else:
            parts.append(f"Reply to {names(authors, nick)}: {comment_kind.phrase(kinds)}")
        why.append(f"{_plural(n, 'comment')} from {names(authors, nick)} "
                   f"{'is' if n == 1 else 'are'} unanswered")
    if not red:
        why.append("only nits and suggestions are waiting")
    return {"group": NEEDS, "color": RED if red else YELLOW,
            "reason": " · ".join(parts) or "Something is waiting on you",
            "why": f"Why it's {'red' if red else 'yellow'}: " + "; ".join(why),
            "waiting_since": min(stamps, default=""),
            "activity_at": max(stamps, default="")}


def _mine_not_on_you(row: dict, nick: dict[str, str], required: int, now: datetime,
                     stale_after_days: float) -> dict:
    """Nothing to answer: waiting on a re-review or a review (yellow once the wait
    passes `stale_after_days`), Ready on the latest push, or needing re-approval
    because the approvals predate it (yellow)."""
    approvals = row.get("approvals") or []
    requested = row.get("requested") or []
    since_of = row.get("requested_since") or {}
    answered = row.get("answered_change_requests") or []
    on_head = [a for a in approvals if a.get("on_head", True)]
    before_head = [a for a in approvals if not a.get("on_head", True)]
    reapprovers = [a for a in approvals if a["author"] in requested]

    def waiting(reason: str, who_: list[str], since: str, why: str) -> dict:
        stale = (bool(since) and not row.get("isDraft")
                 and days_waiting(since, now) > stale_after_days)
        if stale:
            why = (f"Why it's yellow: waiting {_plural(int(days_waiting(since, now)), 'day')}"
                   f", time to nudge {names(who_, nick)}" + (f"; {why}" if why else ""))
        elif why:
            why = "Why: " + why
        return {"group": WAITING, "color": YELLOW if stale else None, "reason": reason,
                "why": why, "waiting_since": since, "activity_at": since}

    if answered or reapprovers:
        people = _unique([b["author"] for b in answered] + [a["author"] for a in reapprovers])
        stamps = [b.get("rerequested_at") or "" for b in answered]
        stamps += [since_of.get(a["author"]) or "" for a in reapprovers]
        why = [f"{who(b['author'], nick)} requested changes {ago(b.get('at'), now)} and you "
               f"re-requested them {ago(b.get('rerequested_at'), now)}" for b in answered]
        why += [f"you re-requested {who(a['author'], nick)} after their approval"
                for a in reapprovers]
        if before_head:
            why.append(f"{len(on_head)} of {required} approvals are on the latest push")
        return waiting(f"Waiting on re-review: {names(people, nick)}", people,
                       min((x for x in stamps if x), default=""), "; ".join(why))

    if len(on_head) >= required:
        return {"group": READY, "color": None,
                "reason": f"Ready: approved {len(on_head)} of {required} on the latest push",
                "why": "", "waiting_since": "", "activity_at": "", "aged": False}

    if len(approvals) >= required:
        stale_names = names([a["author"] for a in before_head], nick)
        head_at = row.get("head_at") or ""
        older = (f"{_possessive(before_head[0]['author'], nick)} approval predates it"
                 if len(before_head) == 1
                 else f"the approvals from {stale_names} predate it")
        return {"group": NEEDS, "color": YELLOW,
                "reason": f"Needs re-approval from {stale_names}",
                "why": (f"Why it's yellow: {len(on_head)} of {required} approvals are on "
                        f"the latest push; {older}"),
                "waiting_since": head_at, "activity_at": head_at}

    if row.get("isDraft"):
        return {"group": WAITING, "color": None, "reason": "GitHub draft, nothing to answer",
                "why": "", "waiting_since": "", "activity_at": "", "aged": False}

    decided = {a["author"] for a in approvals}
    pending = [r for r in requested if r not in decided]
    tally = f"{len(approvals)} of {required} approvals"
    if not pending:
        return {"group": WAITING, "color": None,
                "reason": f"No reviewer requested · {tally}", "why": "",
                "waiting_since": "", "activity_at": "", "aged": False}
    since = min((since_of.get(r) or "" for r in pending if since_of.get(r)), default="")
    return waiting(f"Waiting on review: {names(pending, nick)} · {tally}", pending, since, "")


# --- Follow-ups on your reviews -----------------------------------------------------


def followup(jobs: list[dict], nick: dict[str, str], *, author: str = "",
             rerequested_at: str = "", dismissed_at: str | None = None) -> dict | None:
    """Others' PR with something for you: the responses since your last comment and
    the @mentions of you (the watcher's follow-up jobs for the PR, any status) and a
    pending re-request. None when there's none of these, so it isn't a follow-up."""
    every = [j for j in jobs if j.get("status") in ACTIVE_JOBS]
    if not every and not rerequested_at:
        return None
    author = author or ((every[0].get("pr") or {}).get("author") if every else "")
    mentions = [j for j in every if j.get("trigger") == "mention"]
    active = [j for j in every if j.get("trigger") != "mention"]
    blocking = [job_blocking(j) for j in active]
    color = followup_color(blocking, bool(rerequested_at), bool(mentions))
    stamps = [j.get("created_at") or "" for j in every] + [rerequested_at]
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
    if mentions:
        who_ = names(_unique(j.get("actor") or "" for j in mentions), nick)
        n = len(mentions)
        parts.append(f"{who_} mentioned you" + (f" {n} times" if n > 1 else ""))
        why.insert(0, f"{who_} mentioned you")
    reason = " · ".join(parts)
    dismissed = bool(dismissed_at and activity and activity <= dismissed_at)
    label = {RED: "red", YELLOW: "yellow"}.get(color, "")
    return {"section": FOLLOWUP, "color": None if dismissed else color, "reason":
            (f"Dismissed · {reason}" if dismissed else reason),
            "why": ("You dismissed this; it comes back when something new arrives"
                    if dismissed else f"Why it's {label}: " + "; ".join(why)),
            "waiting_since": min(stamps, default=""), "activity_at": activity,
            "dismissed": dismissed, "jobs": every}


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


def other_reason(st: dict) -> tuple[str, str, int]:
    """Why a review candidate needs nothing from you: (words for the row, the triage's
    detail, order inside the section). Updates where something happened come first."""
    bucket = st.get("bucket")
    reason = st.get("reason", "")
    if st.get("unreadable"):
        return "Couldn't read it from GitHub; it will be retried", reason, 7
    if bucket == "needs_you":
        if st.get("tier") == PARKED_TIER:
            parked = [t for t in st.get("tags") or []
                      if t.startswith("CI failing") or "approvals already" in t]
            words = [re.sub(r"^CI failing: (.*)$", r"CI failing (\1)", t) for t in parked]
            return ("Parked: " + ("; ".join(words) or "your review can't unblock it yet"),
                    parked_reason(st), 2)
        if "commits landed" in reason or "pushed since" in reason:
            return ("The author pushed commits that touch none of your comments", reason, 0)
        if "commented on the PR" in reason:
            return "The author commented, not to you", reason, 0
        if "replied in a review" in reason:
            return "The author replied in a review, not to you", reason, 0
        return "Something moved, nothing aimed at you", reason, 0
    if bucket == "author":
        return "Waiting on the author", reason, 1
    if bucket == "done":
        return "You approved", reason, 3
    if bucket == "well_approved":
        n = len(st.get("approvals") or [])
        return f"{n} approvals, no change requests", reason, 4
    return reason or "Not on you", reason, 6


def legacy(pr: dict, me: str) -> tuple[str, str, int]:
    """The same for a PR only GitHub's searches found (not triaged): (row words,
    detail, order)."""
    sources = ", ".join(s.lower() for s in pr.get("sources") or []) or "search"
    if (pr.get("author") or "").lower() == (me or "").lower():
        return "Your PR, not triaged (check this)", f"matched: {sources}", 6
    if pr.get("draft"):
        return "GitHub draft, not ready for review", "GitHub draft PR", 5
    if "Review requested" in (pr.get("sources") or []):
        return ("Review requested but not triaged (check this)", f"matched: {sources}", 6)
    return "Not on you", f"matched only: {sources}", 6


def _low(base: dict, words: str, detail: str, order: int) -> dict:
    return {**base, "section": OTHER, "color": LOW, "reason": words,
            "why": f"Why: {detail}" if detail and detail != words else "",
            "order": order, "waiting_since": "", "activity_at": "", "dismissed": False,
            "jobs": []}


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
        p = mine(row, nick, required, dismissed.get(row["url"]), now=now,
                 stale_after_days=stale_after_days)
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
                out.append(_low(base, *other_reason(st)))
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
                out.append(_low(base, *other_reason(st)))

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


def other_entry(pr: dict, me: str) -> dict:
    """A ⚪ entry for a PR only GitHub's searches found."""
    url = (pr.get("url") or "").rstrip("/")
    item = {"pr_url": url, "title": pr.get("title", ""), "author": pr.get("author", "")}
    return _low({"url": url, "title": pr.get("title", ""), "row": None, "st": item},
                *legacy(pr, me))


def sort_key(entry: dict, order: int = 0) -> tuple:
    """Red, then yellow, then green, then no dot; inside a color the longest wait
    first (or `order`, the caller's ranking, for Reviews you owe)."""
    return (-rank(entry.get("color")), order, entry.get("waiting_since") or "~")
