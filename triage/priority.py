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
      yellow   only nits and suggestions are waiting.
      red      also when an auto-fix (watcher/autofix.py) couldn't fix a comment:
               "Couldn't auto-fix: <reason>" leads the row.
      no dot   every comment waiting on you is being auto-fixed ("Claude is
               fixing …"); a comment being fixed never counts (`autofix_overlay`).
    Waiting on others
      no dot   waiting on a re-review (a reviewer you re-requested after their change
               request or approval) or on a review, naming who and since when.
      yellow   the same, once the wait passes `stale_after_days`: a cue to nudge.
      no dot   a GitHub draft with nothing to answer, or no reviewer requested.
    Ready
      no dot   `required` approvals, nothing to answer, no change request standing. In
               a repo that requires an approval of the latest push, one with none on
               its head commit says to get one before merging.
  Every row ends with its approval tally (`tally`) and, while it waits, its age.

  Other PRs, everyone else's open PRs. Each entry keeps the kind it was classified as
  (`section`: FOLLOWUP, OWED or OTHER); the menu shows them under Needs you (follow-ups
  and reviews you owe, `group` NEEDS), Waiting on others (`group` WAITING) and a
  collapsed Nothing needed (`group` NOTHING).

  Follow-ups and mentions (`FOLLOWUP`), others' PRs you reviewed or commented on where
  someone responded since your last comment, and any PR where someone @mentioned you:
    red      someone @mentioned you; the author re-requested your review (whatever
             your earlier decision); a response landed in one of your blocking
             threads: a reply, or the code under it changed (the watcher's reply /
             outdated / moved triggers); or, while your change request stands (it is
             your latest decision), the author answered it outside any thread: real
             commits since your last review or comment, a top-level comment, or a
             review with a body (the watcher's change_request jobs).
    yellow   the only responses are on non-blocking threads.
  Each clears when you answer it: a new review of yours clears them all, and a
  change request's response also clears when you comment on the PR or the change
  request is dismissed (watcher/followup.py cleared_jobs).
  A PR you engaged with that has none of these is Waiting on others: the author is
  still working. That covers commits that touch none of your threads, or a comment
  that doesn't mention you, on a PR you only commented on.

  Reviews you owe (`OWED`), others' PRs you haven't reviewed, or whose review of yours
  was dismissed. A teammate's PR (`[triage] team`, `team_pr`) is one of them whether or
  not GitHub requests you, from when it was ready for review:
    yellow   stalled: waiting on you more than `stale_after_days` (calendar hours,
             weekends included, from reviews._waiting_since), fewer than `required`
             approvals, and at most `max_engaged` people other than the author and bots
             have approved or commented.
    green    everything else. Shown, but not counted in the menu-bar title.
    red      instead, from the start, when the author is in `[triage] pod`: a pod
             member's PR is yours to review first. It stays red once others approve it;
             only failing CI parks it.

  Everything else (`OTHER`): Waiting on others, with no dot, when you left comments or
  a change request and the author hasn't responded to them; otherwise Nothing needed,
  ⚪ low, with the reason in words (parked, approved, drafts). Never counted and never
  notified.

  Done for you (`done_for_you`), ✅, what pr-digest did on its own today: auto-fixes it
  pushed and automatic re-approvals it posted. Never counted, kept until dismissed or
  the next day.

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
# Something pr-digest did for you (Done for you): shown, never counted, never a popup
# of its own color.
DONE = "done"
# The colors a row can carry besides LOW, which never counts and never notifies.
COLORS = (RED, YELLOW, GREEN)
# The colors the menu-bar title counts: things to act on. A new review request (green)
# shows in the menu but leaves the title alone.
COUNTED = (RED, YELLOW)
DOTS = {RED: "🔴", YELLOW: "🟡", GREEN: "🟢", LOW: "⚪", DONE: "✅"}
LEGEND = "🔴 act now · 🟡 act soon · 🟢 new · ⚪ no action needed · ✅ done for you"
_RANK = {None: 0, LOW: 0, DONE: 0, GREEN: 1, YELLOW: 2, RED: 3}

MINE, FOLLOWUP, OWED, OTHER, DONE_SECTION = "mine", "followups", "owed", "other", "done"
SECTION_NAMES = {MINE: "My PRs", FOLLOWUP: "Follow-ups and mentions",
                 OWED: "Reviews you owe", OTHER: "Non action requiring updates",
                 DONE_SECTION: "Done for you"}

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


def team_pr(author: str, title: str, team, tickets) -> bool:
    """A teammate's PR that counts as asking for your review whether or not GitHub
    requests you (`[triage] team`): its author is in `team` and, when `tickets` names
    any Jira project keys, its title carries one of them as a ticket key (the project
    key, a dash and a number)."""
    if not author or author not in set(team or ()):
        return False
    keys = [k for k in tickets or () if k]
    return not keys or any(re.search(rf"\b{re.escape(k)}-\d+\b", title or "") for k in keys)


def team_search(org: str, me: str, team) -> str:
    """The GitHub search for your teammates' open, non-draft PRs. Repeated `author:`
    qualifiers match any of them. '' when there's no team."""
    authors = " ".join(f"author:{a}" for a in sorted(set(team or ())) if a and a != me)
    return f"is:pr is:open org:{org} draft:false {authors}" if authors else ""


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
                   mentioned: bool = False, change_request: bool = False) -> str | None:
    """The follow-up rule on its own: red for a mention, a re-request, a response to
    your standing change request, or any response in a blocking thread; yellow for
    responses only in non-blocking ones."""
    if mentioned or rerequested or change_request or any(blocking):
        return RED
    return YELLOW if blocking else None


# --- Your PRs ---------------------------------------------------------------------

# Sub-headers (an entry's `group`), in menu order. My PRs use the first three; Other
# PRs use Needs you and Waiting on others, and list NOTHING in a collapsed submenu.
NEEDS, WAITING, READY, NOTHING = "needs_you", "waiting", "ready", "nothing"
MINE_GROUPS = {NEEDS: "Needs you", WAITING: "Waiting on others", READY: "Ready"}
MINE_GROUP_ORDER = {NEEDS: 0, WAITING: 1, READY: 2}
OTHERS_NAME = "Other PRs"
NOTHING_NAME = "Nothing needed"
# other_reason orders that mean the author hasn't responded to you yet: they pushed
# commits that miss your comments or commented elsewhere (0), or nothing since (1).
WAITING_ORDERS = (0, 1)
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


def tally(n: int, required: int) -> str:
    """'1 of 2 approvals'; past the requirement, just the count ('3 approvals')."""
    return f"{n} of {required} approvals" if n <= required else f"{n} approvals"


def _with_age(text: str, since, now: datetime) -> str:
    a = age(since, now)
    return f"{text} · {a}" if a else text


def _possessive(login: str, nick: dict[str, str]) -> str:
    return who(login, nick) + "'s"


# Auto-fix job statuses (watcher/autofix.py) that change a row of yours: a fix in
# flight takes its comment off your plate, and an escalation puts it back, in red.
FIXING_JOBS = ("queued", "running")
ESCALATED_JOB = "escalated"
REASON_MAX = 80


def autofix_overlay(row: dict, jobs) -> tuple[dict, list[dict], list[tuple[dict, dict]]]:
    """(row, fixing, escalated) for one of your PRs and its auto-fix jobs. `fixing` are
    the open threads a fix is queued or running for, taken out of the row's
    threads_waiting (and `waiting` recomputed); `escalated` pairs each open thread
    with the job that couldn't fix it. A job run as a dry run changes nothing."""
    live = [j for j in jobs or () if not j.get("dry_run")]
    if not live:
        return row, [], []
    fixing_ids = {j.get("thread_id") for j in live if j.get("status") in FIXING_JOBS}
    stuck = {j.get("thread_id"): j for j in live if j.get("status") == ESCALATED_JOB}
    kept, fixing, escalated = [], [], []
    for t in row.get("threads_waiting") or []:
        tid = t.get("thread_id")
        if tid and tid in fixing_ids:
            fixing.append(t)
            continue
        kept.append(t)
        if tid and tid in stuck:
            escalated.append((t, stuck[tid]))
    if fixing:
        row = {**row, "threads_waiting": kept,
               "waiting": bool(kept or row.get("blockers") or row.get("unanswered_comments"))}
    return row, fixing, escalated


def _short(text: str, limit: int = REASON_MAX) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _fixing_phrase(fixing: list[dict], nick: dict[str, str]) -> str:
    """'Claude is fixing Woods's nit' / 'Claude is fixing 3 comments'."""
    authors = _unique(t.get("author") or "?" for t in fixing)
    kinds = comment_kind.phrase(t.get("kind") or "comment" for t in fixing)
    if len(authors) == 1 and len(fixing) == 1:
        return f"Claude is fixing {_possessive(authors[0], nick)} {fixing[0].get('kind') or 'comment'}"
    return f"Claude is fixing {kinds} from {names(authors, nick)}"


def mine(row: dict, nick: dict[str, str], required: int = 2,
         dismissed_at: str | None = None, *, now: datetime | None = None,
         stale_after_days: float = 2, autofix=()) -> dict:
    """One of your PRs: {section, group, color, reason, why, waiting_since,
    activity_at, dismissed}. `now` defaults to the current time. `autofix` is the PR's
    auto-fix jobs (`autofix_overlay`): while a fix runs its comment doesn't count, and
    a row whose comments are all being fixed waits in Needs you with no dot; a fix that
    escalated makes the row red."""
    now = now or datetime.now(timezone.utc)
    row, fixing, escalated = autofix_overlay(row, autofix)
    if row.get("waiting"):
        p = _mine_needs_you(row, nick, required, escalated, fixing)
    elif fixing:
        p = {"group": NEEDS, "color": None,
             "reason": f"{_fixing_phrase(fixing, nick)} · "
                       f"{tally(len(row.get('approvals') or []), required)}",
             "why": "Why no dot: Claude is fixing the comments waiting on you; the row turns "
                    "red if it can't",
             "waiting_since": "", "activity_at": "", "aged": False}
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


def _mine_needs_you(row: dict, nick: dict[str, str], required: int,
                    escalated=(), fixing=()) -> dict:
    """Something on the PR is waiting on you: red for a change request, any comment
    that isn't a nit or a suggestion, or a comment an auto-fix couldn't fix; yellow
    when only nits and suggestions wait."""
    asks = _asks(row)
    blockers = row.get("blockers") or []
    stamps = [a["at"] for a in asks if a["at"]] + [b.get("at") or "" for b in blockers]
    stamps += [j.get("escalated_at") or "" for _, j in escalated]
    stamps = [x for x in stamps if x]
    cr = _unique(b["author"] for b in blockers)
    authors = _unique(a["author"] for a in asks)
    kinds = [a["kind"] for a in asks]
    n = len(asks)
    red = (bool(cr) or bool(escalated) or not kinds
           or any(k not in YELLOW_KINDS for k in kinds))

    parts, why = [], []
    if escalated:
        first = escalated[0][1].get("reason") or "it stopped"
        parts.append(f"Couldn't auto-fix: {_short(first)}" if len(escalated) == 1
                     else f"Couldn't auto-fix {len(escalated)} comments")
        why += [f"Claude couldn't auto-fix {_possessive(t.get('author') or '?', nick)} "
                f"{t.get('kind') or 'comment'}: {j.get('reason') or 'it stopped'}"
                for t, j in escalated]
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
    parts = parts or ["Something is waiting on you"]
    if fixing:
        parts.append(_fixing_phrase(fixing, nick))
    parts.append(tally(len(row.get("approvals") or []), required))
    return {"group": NEEDS, "color": RED if red else YELLOW,
            "reason": " · ".join(parts),
            "why": f"Why it's {'red' if red else 'yellow'}: " + "; ".join(why),
            "waiting_since": min(stamps, default=""),
            "activity_at": max(stamps, default="")}


def _mine_not_on_you(row: dict, nick: dict[str, str], required: int, now: datetime,
                     stale_after_days: float) -> dict:
    """Nothing to answer: waiting on a re-review or a review (yellow once the wait
    passes `stale_after_days`), or Ready. Ready is `required` approvals and no change
    request standing; in a repo whose rules require an approval of the latest push
    (`last_push_required`), one with no approval on its head commit says to get one
    before merging, still with no dot."""
    approvals = row.get("approvals") or []
    requested = row.get("requested") or []
    since_of = row.get("requested_since") or {}
    answered = row.get("answered_change_requests") or []
    on_head = [a for a in approvals if a.get("on_head", True)]
    reapprove = bool(row.get("last_push_required")) and not on_head
    count = tally(len(approvals), required)
    reapprove_why = "GitHub needs an approval of the latest push here, and none is on it yet"

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

    reapprovers = [a for a in approvals if a["author"] in requested]
    if answered or reapprovers:
        people = _unique([b["author"] for b in answered] + [a["author"] for a in reapprovers])
        stamps = [b.get("rerequested_at") or "" for b in answered]
        stamps += [since_of.get(a["author"]) or "" for a in reapprovers]
        why = [f"{who(b['author'], nick)} requested changes {ago(b.get('at'), now)} and you "
               f"re-requested them {ago(b.get('rerequested_at'), now)}" for b in answered]
        why += [f"you re-requested {who(a['author'], nick)} after their approval"
                for a in reapprovers]
        if reapprove:
            why.append(reapprove_why)
        return waiting(f"Waiting on re-review: {names(people, nick)} · {count}", people,
                       min((x for x in stamps if x), default=""), "; ".join(why))

    if len(approvals) >= required:
        if reapprove:
            return {"group": READY, "color": None,
                    "reason": f"Ready · re-approve the latest push before merge · {count}",
                    "why": f"Why: {reapprove_why}", "waiting_since": "", "activity_at": "",
                    "aged": False}
        return {"group": READY, "color": None, "reason": f"Ready: {count}",
                "why": "", "waiting_since": "", "activity_at": "", "aged": False}

    if row.get("isDraft"):
        return {"group": WAITING, "color": None, "reason": "GitHub draft, nothing to answer",
                "why": "", "waiting_since": "", "activity_at": "", "aged": False}

    decided = {a["author"] for a in approvals}
    pending = [r for r in requested if r not in decided]
    if not pending:
        return {"group": WAITING, "color": None,
                "reason": f"No reviewer requested · {count}", "why": "",
                "waiting_since": "", "activity_at": "", "aged": False}
    since = min((since_of.get(r) or "" for r in pending if since_of.get(r)), default="")
    return waiting(f"Waiting on review: {names(pending, nick)} · {count}", pending, since, "")


# --- Follow-ups on your reviews -----------------------------------------------------


def followup(jobs: list[dict], nick: dict[str, str], *, author: str = "",
             rerequested_at: str = "", dismissed_at: str | None = None,
             approvals: int | None = None, required: int = 2) -> dict | None:
    """Others' PR with something for you: the responses since your last comment and
    the @mentions of you (the watcher's follow-up jobs for the PR, any status) and a
    pending re-request. None when there's none of these, so it isn't a follow-up.
    `approvals`, when known, ends the reason with the PR's approval tally."""
    every = [j for j in jobs if j.get("status") in ACTIVE_JOBS]
    if not every and not rerequested_at:
        return None
    author = author or ((every[0].get("pr") or {}).get("author") if every else "")
    mentions = [j for j in every if j.get("trigger") == "mention"]
    answers = [j for j in every if j.get("trigger") == "change_request"]
    active = [j for j in every if j.get("trigger") not in ("mention", "change_request")]
    blocking = [job_blocking(j) for j in active]
    color = followup_color(blocking, bool(rerequested_at), bool(mentions), bool(answers))
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
    if answers:
        words, said = _cr_response(answers, author, nick)
        if rerequested_at and not active and parts:
            parts[-1] += " and " + words.split(" ", 1)[1]
        else:
            parts.append(words)
        why.append(said)
    if mentions:
        who_ = names(_unique(j.get("actor") or "" for j in mentions), nick)
        n = len(mentions)
        parts.append(f"{who_} mentioned you" + (f" {n} times" if n > 1 else ""))
        why.insert(0, f"{who_} mentioned you")
    if approvals is not None:
        parts.append(tally(approvals, required))
    reason = " · ".join(parts)
    dismissed = bool(dismissed_at and activity and activity <= dismissed_at)
    label = {RED: "red", YELLOW: "yellow"}.get(color, "")
    return {"section": FOLLOWUP, "color": None if dismissed else color, "reason":
            (f"Dismissed · {reason}" if dismissed else reason),
            "why": ("You dismissed this; it comes back when something new arrives"
                    if dismissed else f"Why it's {label}: " + "; ".join(why)),
            "waiting_since": min(stamps, default=""), "activity_at": activity,
            "dismissed": dismissed, "jobs": every}


_CR_VERBS = {"pushed": "pushed commits", "commented": "commented",
             "review": "answered in a review"}
_VERDICT_WORDS = {"addressed": "addressed", "partly": "partly addressed",
                  "not_addressed": "not addressed"}


def _cr_response(jobs: list[dict], author: str, nick: dict[str, str]) -> tuple[str, str]:
    """(row words, why) for the author's responses to your change request, from its
    newest job (each newer one covers everything since your last touch):
    'Daniel pushed commits and commented since your change request · Claude: partly
    addressed'."""
    job = max(jobs, key=lambda j: j.get("created_at") or "")
    triggers = [t for t in _CR_VERBS if any(t in (j.get("triggers") or [j.get("trigger")])
                                            for j in jobs)]
    actor = who(job.get("actor") or author, nick)
    verbs = [_CR_VERBS[t] for t in triggers] or ["responded"]
    did = verbs[0] if len(verbs) == 1 else ", ".join(verbs[:-1]) + " and " + verbs[-1]
    words = f"{actor} {did} since your change request"
    if job.get("status") == "ready" and job.get("verdict") in _VERDICT_WORDS:
        words += f" · Claude: {_VERDICT_WORDS[job['verdict']]}"
    return words, f"{actor} {did} while your change request stands"


# --- Reviews you owe ----------------------------------------------------------------


def owed(st: dict, nick: dict[str, str], now: datetime, *, required: int = 2,
         stale_after_days: float = 2, max_engaged: int = 1,
         dismissed_at: str | None = None, pod: bool = False,
         draft: str | None = None) -> dict:
    """A PR waiting on your first review (or a fresh one after yours was dismissed).
    `pod` (the author is in `[triage] pod`) makes it red from the start: a pod member's
    PR is yours to review first, however long it has waited.

    `draft` is the state of its automatic review draft (review/auto_draft.py: "draft
    ready", "draft outdated", "draft failed", "no draft"), given while `[review]
    auto_draft` is on. The row then names who approved and says the draft's state:
    'Review it · Mykal approved (1 of 2) · draft ready · 5d'."""
    since = st.get("waiting_since") or st.get("created_at") or ""
    engaged = list(st.get("engaged") or [])
    n = len(st.get("approvals") or [])
    waited = days_waiting(since, now)
    stale = waited > stale_after_days and n < required and len(engaged) <= max_engaged
    color = RED if pod else YELLOW if stale else GREEN
    approvals = "no approvals" if not n else _plural(n, "approval")
    dismissed_review = (st.get("mine") or [None])[0] == "DISMISSED"

    if draft:
        approvers = sorted(st.get("approvals") or [])
        count = f"{n} of {required}" if n <= required else f"{n} approvals"
        approved = (f"{names(approvers, nick)} approved ({count})" if approvers
                    else tally(n, required))
        reason = _with_age(f"Review it · {approved} · {draft}", since, now)
    else:
        reason = _with_age(f"Review it · {tally(n, required)}", since, now)
        if color == GREEN and len(engaged) > max_engaged:
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
    label = {RED: "red", YELLOW: "yellow", GREEN: "green"}[color]
    lead = (f"{who(st.get('author') or '', nick)} is in your pod, so you review it first · "
            if pod else "")
    if st.get("team_ask"):
        lead += (f"{who(st.get('author') or '', nick)} is your teammate, so it counts as "
                 f"asking you though GitHub doesn't request you · ")
        asked_words = f"ready for review {asked}"
    else:
        asked_words = f"requested {asked}"
    why = f"Why it's {label}: {lead}{asked_words} · {approvals} · {people}"
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
    return {**base, "section": OTHER, "group": NOTHING, "color": LOW, "reason": words,
            "why": f"Why: {detail}" if detail and detail != words else "",
            "order": order, "waiting_since": "", "activity_at": "", "dismissed": False,
            "jobs": []}


def _not_on_you(base: dict, st: dict, now: datetime, required: int = 2) -> dict:
    """Someone else's PR with nothing for you. One you left comments or a change
    request on, whose author hasn't responded to them (other_reason orders 0 and 1),
    is Waiting on others: no dot, aged from your latest review. The rest is Nothing
    needed (⚪). While your change request stands, the author's commits or comments
    reach here only once you've answered them (a new review or a comment); until then
    the watcher's change_request job makes the PR a follow-up."""
    e = _low(base, *other_reason(st))
    if e["order"] not in WAITING_ORDERS:
        return e
    since = (st.get("my_review") or ["", ""])[0] or ""
    reason = f"{e['reason']} · {tally(len(st.get('approvals') or []), required)}"
    return {**e, "group": WAITING, "color": None, "waiting_since": since,
            "reason": _with_age(reason, since, now)}


# --- Done for you ---------------------------------------------------------------------


def jobs_by_url(jobs) -> dict[str, list[dict]]:
    """Jobs grouped by their PR's url (no trailing slash)."""
    out: dict[str, list[dict]] = {}
    for j in jobs:
        url = ((j.get("pr") or {}).get("url") or "").rstrip("/")
        if url:
            out.setdefault(url, []).append(j)
    return out


def _on_day(epoch, day) -> bool:
    try:
        return bool(epoch) and datetime.fromtimestamp(int(epoch), timezone.utc).astimezone().date() == day
    except (TypeError, ValueError, OverflowError, OSError):
        return False


def _fixed_phrase(fixes: list[dict], nick: dict[str, str]) -> str:
    """'Fixed Woods's nit', 'Fixed 3 of Woods's nits, Mykal's suggestion'."""
    by: dict[str, list[str]] = {}
    for j in fixes:
        by.setdefault(j.get("reviewer") or "?", []).append(j.get("kind") or "comment")
    parts = []
    for login, kinds in by.items():
        if len(kinds) == 1:
            parts.append(f"{_possessive(login, nick)} {kinds[0]}")
            continue
        word = (comment_kind.phrase(kinds).split(" ", 1)[1] if len(set(kinds)) == 1
                else "comments")
        parts.append(f"{len(kinds)} of {_possessive(login, nick)} {word}")
    return "Fixed " + ", ".join(parts)


def _fix_proof(fixes: list[dict]) -> str:
    """'pushed a1b2c3d, replied, resolved'."""
    shas = [(j.get("commit_sha") or "")[:7] for j in fixes]
    pushed = f"pushed {shas[0]}" if len(shas) == 1 else f"pushed {len(shas)} commits"
    n, k = len(fixes), sum(1 for j in fixes if j.get("resolved"))
    resolved = "resolved" if k == n else "not resolved" if not k else f"resolved {k} of {n}"
    return f"{pushed}, replied, {resolved}"


def done_for_you(autofix_jobs: list[dict], followup_jobs: list[dict], nick: dict[str, str],
                 now: datetime) -> list[dict]:
    """What pr-digest did for you, one entry per PR, newest first: finished auto-fixes
    (watcher/autofix.py status done) and automatic re-approvals (watcher/followup.py
    status reapproved). Each shows on the local day it happened until you dismiss it
    (`done_dismissed_at`). Never counted. An entry is {url, title, section, color,
    reason, why, done_at (epoch seconds), fixes, reapprovals, waiting_since,
    activity_at, dismissed}."""
    today = now.astimezone().date()
    fixes = [j for j in autofix_jobs if j.get("status") == "done"
             and not j.get("done_dismissed_at") and _on_day(j.get("done_at"), today)]
    reapprovals = [j for j in followup_jobs if j.get("status") == "reapproved"
                   and not j.get("done_dismissed_at")
                   and _on_day(j.get("reapproved_at") or j.get("finished_at"), today)]
    groups: dict[str, dict] = {}
    for key, js in (("fixes", fixes), ("reapprovals", reapprovals)):
        for url, items in jobs_by_url(js).items():
            g = groups.setdefault(url, {"fixes": [], "reapprovals": [],
                                        "title": (items[0].get("pr") or {}).get("title") or ""})
            g[key] += items
    out = []
    for url, g in groups.items():
        parts = []
        if g["fixes"]:
            parts += [_fixed_phrase(g["fixes"], nick), _fix_proof(g["fixes"])]
        if g["reapprovals"]:
            who_ = names(_unique(j.get("actor") or "?" for j in g["reapprovals"]), nick)
            parts.append(f"Re-approved: {who_} did what you asked")
        stamps = [j.get("done_at") or 0 for j in g["fixes"]]
        stamps += [j.get("reapproved_at") or j.get("finished_at") or 0 for j in g["reapprovals"]]
        out.append({"url": url, "title": g["title"], "section": DONE_SECTION, "color": DONE,
                    "reason": " · ".join(parts), "why": "", "done_at": max(stamps),
                    "fixes": g["fixes"], "reapprovals": g["reapprovals"],
                    "waiting_since": "", "activity_at": "", "dismissed": False})
    out.sort(key=lambda e: -e["done_at"])
    return out


# --- the whole menu ------------------------------------------------------------------


def _owed_review(st: dict) -> bool:
    """A first review you owe: the triage says it's on you and it isn't parked."""
    return st.get("bucket") == "needs_you" and st.get("tier") != PARKED_TIER


def _owed_pod_review(st: dict) -> bool:
    """A pod member's PR stays a review you owe even once others have approved it
    (parked on approvals, or in the well-approved bucket). Failing CI still parks it."""
    if any(t.startswith("CI failing") for t in st.get("tags") or []):
        return False
    return st.get("bucket") in ("needs_you", "well_approved")


def classify(mine_rows: list[dict], reviews: dict[str, list[dict]], jobs: list[dict], *,
             nick: dict[str, str], now: datetime, required: int = 2,
             stale_after_days: float = 2, max_engaged: int = 1,
             dismissed: dict[str, str] | None = None,
             autofix: list[dict] | None = None, pod=frozenset(),
             drafts: dict[str, dict] | None = None) -> list[dict]:
    """One entry per PR: {url, title, section, color, reason, why, waiting_since,
    activity_at, dismissed, row | st, jobs}. `pod` holds the logins whose PRs are
    red reviews from the start (`owed`). `reviews` is review_triage's buckets
    (needs_you in ranked order); `jobs` every follow-up job; `dismissed` url → the
    activity time it was dismissed at; `autofix` every auto-fix job, which only your
    PRs carry (an entry's `autofix`).

    Answer jobs (follow-up jobs with `trigger` "answer", drafted replies to comments on
    your PRs) ride on your PR's entry as `answers`, never as follow-ups; while one is
    being written the row has no dot ("Claude is drafting replies").

    `drafts` (PR url → {"state", "tooltip"}, review/auto_draft.py states) is given
    while automatic drafts are on. A review you owe then carries its draft state in
    its reason and its `tooltip`, and one with no state yet (its draft is still being
    written) gets no entry at all: no row, no count, until the draft is ready."""
    dismissed = dismissed or {}
    fixes = jobs_by_url(autofix or [])
    answers = jobs_by_url(j for j in jobs if j.get("trigger") == "answer")
    jobs = [j for j in jobs if j.get("trigger") != "answer"]
    out: list[dict] = []
    for row in mine_rows:
        url = row["url"].rstrip("/")
        p = mine(row, nick, required, dismissed.get(row["url"]), now=now,
                 stale_after_days=stale_after_days, autofix=fixes.get(url, []))
        if p["group"] == NEEDS and p["color"] and any(
                j.get("status") in ("queued", "running") for j in answers.get(url, [])):
            p = {**p, "color": None, "reason": f"Claude is drafting replies · {p['reason']}",
                 "why": "Why no dot yet: Claude is drafting your replies; the dot and the "
                        "popup come when they're ready"}
        out.append({**p, "url": row["url"], "title": row.get("title", ""), "row": row,
                    "st": None, "jobs": [], "autofix": fixes.get(url, []),
                    "answers": answers.get(url, [])})

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
                         dismissed_at=dismissed.get(url),
                         approvals=len(st.get("approvals") or []), required=required)
            mine_state = (st.get("mine") or [None])[0]
            fresh = not (st.get("mine") or st.get("commented")) or mine_state == "DISMISSED"
            if f:
                out.append({**base, **f, "group": NEEDS})
            elif fresh and (_owed_review(st) or (st.get("author") in pod
                                                  and _owed_pod_review(st))):
                d = None if drafts is None else (drafts.get(url.rstrip("/")) or {})
                if d is not None and not d.get("state"):
                    continue
                out.append({**base, "jobs": [], "group": NEEDS,
                            "tooltip": (d or {}).get("tooltip") or "", **owed(
                                st, nick, now, required=required,
                                stale_after_days=stale_after_days, max_engaged=max_engaged,
                                dismissed_at=dismissed.get(url), pod=st.get("author") in pod,
                                draft=(d or {}).get("state"))})
            else:
                out.append(_not_on_you(base, st, now, required))

    # Responses on a PR the review triage didn't return (it closed between fetches, or
    # the search missed it) still show as follow-ups.
    for url, js in by_url.items():
        if url in placed:
            continue
        f = followup(js, nick, dismissed_at=dismissed.get(url))
        if f:
            placed.add(url)
            out.append({**f, "url": url, "title": (js[0].get("pr") or {}).get("title", ""),
                        "row": None, "st": None, "group": NEEDS})
    return out


def counts(entries: list[dict]) -> dict[str, int]:
    """How many PRs carry each color."""
    return {c: sum(1 for e in entries if e.get("color") == c) for c in COLORS}


def title(entries: list[dict]) -> str:
    """The menu-bar title: '🔴2 🟡1', counting only COUNTED colors and omitting those
    with none; '✓' when nothing is red or yellow."""
    n = counts(entries)
    return " ".join(f"{DOTS[c]}{n[c]}" for c in COUNTED if n[c]) or "✓"


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
