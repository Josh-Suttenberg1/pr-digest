"""PR activity watcher. launchd runs `tick` every poll_interval seconds.

    python -m watcher tick [--dry-run] [--since 24h]   one poll (dry-run: print, send nothing)
    python -m watcher status                           last poll, backoff, follow-ups
    python -m watcher post <job>[,<job>…] | dismiss <job>[,<job>…]   menu actions on follow-ups
    python -m watcher followup-run <job>               one follow-up (started by tick)
    python -m watcher approve <PR id> <head>           approve someone else's PR (the menu's
                                                       "Approve"; re-checks it's still ready)
    python -m watcher settle                           one tick once any running tick ends;
                                                       every menu action starts it (`settle`)
    python -m watcher autofix-run <job | comment link> [--dry-run]
                                                       one auto-fix (started by tick); --dry-run
                                                       runs it up to the commit and prints it
    python -m watcher drafts [--dry-run] [--limit N] [--only <PR url>] [--print-popups]
                                                       one pass of the automatic review drafts
                                                       (review/auto_draft.py), on its own
    python -m watcher draft-run <repo>#<n>             one draft or update (started by a pass)
    python -m watcher notify-test [--withdraw-after S]  a red alert and a yellow banner, each with
                                                       its sound; optionally withdrawn S seconds later
    python -m watcher alert-app                        build the app red alerts go through
                                                       (~/Applications/PR Digest Alerts.app)
    python -m watcher install                          write and load the launchd jobs (the
                                                       watcher, and the weekly lessons refresh)

The first tick with no saved state records everything as seen and sends nothing.

Notifications go out per PR, not per event (`notifications`). After a tick's events,
each PR that had any is classified through triage/priority.py: your PRs from a fresh
triage/mine.py row, others' PRs from their follow-up jobs and any pending re-request.
A PR notifies only when its color rises above the color it last notified at
(`state["notified"]`), so all of a tick's events make one notification, a later event
at the same or a lower color only updates the menu, and yellow → red pops up again.
The stored color is dropped once the PR has no color (the row cleared: you answered,
posted the drafted replies, re-reviewed) or you dismissed its menu row
(state/watcher/cleared.json); the next response then notifies afresh. The PR's popup
is withdrawn then too. A menu action (Approve, Post, Dismiss) runs a tick right away
(`settle`), so that happens seconds after the click; what you do on github.com or in
a Claude session is picked up by the next scheduled tick. Uncolored
activity, green review requests and approvals never notify.

On others' PRs, the author's response to your standing change request outside any
thread (real commits, a top-level comment or a review body; watcher/events.py
`_diff_change_request`) queues a change-request follow-up like a thread response: the
PR goes red, its one popup waits for Claude's verdict on whether the change request is
addressed, and a new review of yours, a comment, or the change request's dismissal
clears it. Real commits are judged by `_real_work` (triage/reviews.py's rule, with your
own commits ruled out).

Each fetch of someone else's PR saves what your approval waits on as its state's
`approval` (watcher/approval.py `needs`). Once Claude's follow-ups find your change
request and blocking comments addressed, the PR's row and popup say "Ready to approve",
and the menu's "Approve" posts the approval (`approve`, followup.approve).

Comments on your PRs get drafted replies (watcher/followup.py answer jobs,
`plan_answers`): each comment newly waiting on you that no auto-fix is working on
queues one, the PR's popup waits for them like a follow-up's, and then opens them.

Auto-fixes (watcher/autofix.py), while `[autofix] enabled` is on: a new nit or
suggestion that opens a thread on your PR (events.opened_threads) queues a fix before
the PR is classified, and a comment being fixed doesn't count, so it pops nothing up.
The fix's run sends its own quiet "✅ Fixed for you", or a red alert when it escalates.

Automatic review drafts (review/auto_draft.py), while `[review] auto_draft` is on: each
tick ends with a pass that starts a draft for every review you owe that has none,
updates drafts the author has pushed past, and sends a review's one notification once
its draft is ready (or failed, or late).

Every PR popup goes out through watcher/popups.py, which records it: red as an alert
through the alert app (notify.py), the rest as banners. Each tick fetches every PR with
a popup out and withdraws the popups whose item is done (`settle_popups`): the PR has
no color left, the review you owed was given, the escalated auto-fix was answered, or
the PR merged or closed. The menu refetches when one is withdrawn.

Each tick moves the Jira ticket of every PR of yours that merged into main since the
last tick, however it was merged to the status that follows a merge
(watcher/jira_merge.py, `jira_merge_pass`): a quiet ⚪ FYI when it moves or waits on
another open PR, a red alert when the ticket is in a status the merge doesn't expect.
`watcher jira-merge --dry-run --since 30d` replays a window and changes nothing.

Each tick also finds FYIs (watcher/fyi.py): approvals of your PRs that ask nothing
(`approval_fyis`), a PR of yours becoming Ready, and reviews you owed that stopped being
owed without you (`owed_fyis`). Each is a ⚪ menu row and one quiet banner.

A notification only links to something that exists. Yours open a Claude session on
the PR's comments (prdigest://respond). A follow-up or mention waits in
`state["pending"]` until Claude's drafted replies are written (`hold_for_drafts`),
then links to them as a rendered page; if the drafts never come (claude logged out,
a run failing) it goes out after PENDING_MAX_MINUTES linking to the comment on
GitHub. A re-request on its own links to the PR.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gh_prs  # noqa: E402
import names  # noqa: E402
import notify  # noqa: E402
import settings  # noqa: E402
from watcher import events as ev  # noqa: E402
from triage import mine as mine_triage  # noqa: E402
from triage import comment_kind  # noqa: E402
from triage import priority  # noqa: E402
from watcher import approval, autofix, followup, fyi, gh, jira_merge, popups, render, state  # noqa: E402

LABEL = "com.josh.pr-digest-watcher"
PLIST = Path(f"~/Library/LaunchAgents/{LABEL}.plist").expanduser()
LOG = Path("~/.cache/pr-digest/watcher.log").expanduser()
# The weekly review-lessons refresh (review/harvest_lessons.py --apply), Mondays 07:00.
LESSONS_LABEL = "com.josh.pr-digest-lessons"
LESSONS_PLIST = Path(f"~/Library/LaunchAgents/{LESSONS_LABEL}.plist").expanduser()
LESSONS_LOG = Path("~/.cache/pr-digest/lessons.log").expanduser()
SWEEP_EVERY = 900
# How long a follow-up notification waits for its drafted replies before it goes out
# linking to the comment on GitHub instead.
PENDING_MAX_MINUTES = 30
UNFINISHED = ("queued", "running")
FIRST_SIGHT_GRACE = timedelta(minutes=5)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _server_now(date_header: str | None) -> datetime:
    try:
        return parsedate_to_datetime(date_header)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _parse_since(text: str) -> timedelta:
    unit = text[-1]
    n = int(text.rstrip("hmd").lstrip("-"))
    return {"h": timedelta(hours=n), "m": timedelta(minutes=n), "d": timedelta(days=n)}[unit]


def _paths_changed(tok: str):
    def changed(repo: str, base: str, head: str) -> set[str] | None:
        try:
            data = gh_prs._get(f"{gh_prs.API_ROOT}/repos/avetta/{repo}/compare/{base}...{head}", tok)
        except gh_prs.DigestError:
            return None
        return {f["filename"] for f in data.get("files") or []}
    return changed


def _real_work(tok: str, org: str, me: str):
    """ctx.real_work: whether real commits by someone other than you landed between two
    shas (triage/reviews.py `_new_work_since`, which needs the repo cloned and errs
    toward True). None when GitHub can't be read."""
    def check(repo: str, number: int, base: str, head: str) -> bool | None:
        try:
            data = gh_prs._get(f"{gh_prs.API_ROOT}/repos/{org}/{repo}/compare/{base}...{head}", tok)
        except gh_prs.DigestError:
            return None
        commits = data.get("commits") or []
        mine = {(c.get("sha") or "").lower() for c in commits
                if ((c.get("author") or {}).get("login") or "") == me}
        if commits and len(mine) == len(commits):
            return False
        from triage import reviews
        return reviews._new_work_since(f"{org}/{repo}", number, base, head,
                                       passes=lambda _git, sha: sha.lower() in mine)
    return check


def pr_priority(pr: dict, cfg: dict, jobs: list[dict],
                groups: dict[str, dict] | None = None) -> tuple[str | None, str]:
    """(color, reason) for one PR snapshot, through triage/priority.py. Your PRs take
    a fresh triage/mine.py row (one gh call); others' take their open follow-up jobs
    (`jobs`, including this tick's new responses) and any pending re-request. A PR of
    yours also leaves its sub-header, reason and the comments waiting on you in
    `groups` (PR id -> {group, reason, asks}, followup.answer_asks)."""
    nick = names.book(fetch=True)
    if pr["kind"] == "mine":
        try:
            row = mine_triage.row_for(cfg["org"], pr["repo"], pr["number"], pr["title"],
                                      pr["url"], cfg["me"], set(cfg["watcher"]["bots"]))
        except (mine_triage.GhError, KeyError, ValueError, TypeError):
            return None, ""
        fixes = [j for j in autofix.all_jobs() if (j.get("pr") or {}).get("id") == pr["id"]]
        p = priority.mine(row, nick, cfg["watcher"]["required_approvals"],
                          stale_after_days=cfg["priority"]["stale_after_days"], autofix=fixes)
        if groups is not None:
            groups[pr["id"]] = {"group": p["group"], "reason": p["reason"],
                                "asks": followup.answer_asks(row)}
        return p["color"], p["reason"]
    pr_jobs = [j for j in jobs if (j.get("pr") or {}).get("id") == pr["id"]]
    f = priority.followup(pr_jobs, nick, author=pr["author"],
                          rerequested_at=ev.pending_rerequest(pr, cfg["me"]),
                          approvals=len(pr.get("approved_by") or []),
                          required=cfg["watcher"]["required_approvals"],
                          approval=approval.status(approval.needs(pr, cfg["me"]), pr_jobs))
    return (f["color"], f["reason"]) if f else (None, "")


JOB_EVENTS = ("thread", "mention", "change_request")


def _event_job(e: dict, now: str) -> dict:
    """A thread, mention or change-request event in the shape of the follow-up job it
    queues, so a dry run classifies this tick's responses the same way as a real one."""
    trigger = e["type"] if e["type"] in ("mention", "change_request") else e.get("trigger")
    return {"id": followup.event_job_id(e), "status": "queued", "created_at": now,
            "pr": e["pr"], "trigger": trigger,
            **{k: e.get(k) for k in ("actor", "thread_id", "arm_id", "path", "line", "url",
                                     "blocking", "where", "comment_id", "body", "mentioned_at",
                                     "review_id", "triggers", "my_comment")}}


def event_jobs(events: list[dict], jobs: list[dict], now: str) -> list[dict]:
    """Jobs for this tick's thread, mention and change-request events that aren't
    queued yet."""
    known = {j["id"] for j in jobs}
    return [_event_job(e, now) for e in events
            if e["type"] in JOB_EVENTS and followup.event_job_id(e) not in known]


def notifications(detail: dict[str, dict], events: list[dict], jobs: list[dict],
                  notified: dict[str, dict], cleared: dict[str, str], now: str,
                  classify, seed: bool = False) -> list[tuple[dict, str, str, list[dict]]]:
    """The tick's notifications, [(pr, color, reason, events)], updating `notified`
    (PR id -> {"color", "at", "url"}) in place. `classify(pr, jobs)` gives a PR's
    (color, reason). `seed` records every PR's color without notifying."""
    by_pr: dict[str, list[dict]] = {}
    for e in events:
        by_pr.setdefault(e["pr"]["id"], []).append(e)
    extra = event_jobs(events, jobs, now)
    out = []
    for pid, pr in detail.items():
        pev = by_pr.get(pid, [])
        prior = notified.get(pid)
        if prior and (cleared.get(pr["url"]) or "") > (prior.get("at") or ""):
            notified.pop(pid, None)
            prior = None
        if not pev and not prior and not seed:
            continue
        color, reason = classify(pr, jobs + extra)
        if not color:
            notified.pop(pid, None)
            continue
        before = (prior or {}).get("color")
        if seed or (pev and priority.rank(color) > priority.rank(before)):
            notified[pid] = {"color": color, "at": now, "url": pr["url"]}
            if not seed:
                out.append((pr, color, reason, pev))
        elif prior and priority.rank(color) < priority.rank(before):
            prior["color"] = color      # so a later rise notifies again
    return out


_EVENT_KEYS = ("type", "actor", "state", "first_line", "count", "trigger", "path",
               "blocking", "url")


def hold_for_drafts(notes: list[tuple[dict, str, str, list[dict]]], pending: dict[str, dict],
                    jobs: list[dict], now: str, nick: dict[str, str], me: str,
                    max_minutes: int = PENDING_MAX_MINUTES, required: int = 2,
                    still_colored: set[str] | None = None,
                    needs: dict[str, dict | None] | None = None) -> list[tuple]:
    """Which notifications go out now, as [(pr, color, reason, events, pr_jobs)].

    A PR whose drafts are still being written waits in `pending` (PR id -> {pr, events,
    rerequested_at, since, color, reason}): a follow-up or mention on someone else's PR,
    or comments on yours whose replies Claude is drafting (answer jobs). Each tick
    releases the ones whose jobs have all finished, or that waited longer than
    `max_minutes`. Someone else's PR is reclassified from its jobs then; yours goes out
    at the color it was held at, unless its row cleared meanwhile (it's no longer in
    `still_colored`, the PR ids in `notified`), which drops it. `needs` (PR id ->
    watcher/approval.py needs) lets a released popup say the PR is ready to approve."""
    def pr_jobs(pid: str) -> list[dict]:
        return [j for j in jobs if (j.get("pr") or {}).get("id") == pid]

    out, held_now = [], set()
    for pr, color, reason, evs in notes:
        js = pr_jobs(pr["id"])
        if any(j.get("status") in UNFINISHED for j in js):
            pending[pr["id"]] = {
                "pr": {k: pr.get(k) for k in ("id", "kind", "repo", "number", "title", "url",
                                              "author", "head")},
                "events": [{k: e.get(k) for k in _EVENT_KEYS} for e in evs],
                "rerequested_at": (ev.pending_rerequest(pr, me)
                                   if "reviews" in pr and pr["kind"] == "others" else ""),
                "since": now, "color": color, "reason": reason}
            held_now.add(pr["id"])
        else:
            out.append((pr, color, reason, evs, js))
    for pid, p in list(pending.items()):
        if pid in held_now:
            continue
        js = pr_jobs(pid)
        waited = (datetime.fromisoformat(now.replace("Z", "+00:00"))
                  - datetime.fromisoformat(p["since"].replace("Z", "+00:00")))
        if any(j.get("status") in UNFINISHED for j in js) and waited < timedelta(minutes=max_minutes):
            continue
        pending.pop(pid)
        if p["pr"].get("kind") == "mine":
            if still_colored is None or pid in still_colored:
                out.append((p["pr"], p.get("color"), p.get("reason") or "", p["events"], js))
            continue
        known = "approved_by" in p["pr"]
        f = priority.followup(js, nick, author=p["pr"]["author"],
                              rerequested_at=p.get("rerequested_at") or "",
                              approvals=len(p["pr"]["approved_by"]) if known else None,
                              required=required,
                              approval=approval.status((needs or {}).get(pid), js))
        if f and f["color"]:
            out.append((p["pr"], f["color"], f["reason"], p["events"], f["jobs"]))
    return out


def _report_page(pr: dict, ready: list[dict], cfg: dict) -> str:
    """Claude's drafted replies for a PR as one rendered page (file:// URL). Several
    reports are joined into <repo>-<n>-responses.md in the reports folder first."""
    from review import view_draft
    ready = sorted(ready, key=lambda j: (j.get("trigger") != "mention", not j.get("blocking")))
    if len(ready) == 1:
        md = Path(ready[0]["report"])
    else:
        md = settings.path_of(cfg["watcher"]["reports_dir"]) / f"{pr['repo']}-{pr['number']}-responses.md"
        parts = [f"# Claude's drafted replies: {render.subtitle(pr)}", "", f"🔗 {pr['url']}", ""]
        for j in ready:
            parts += ["---", "", Path(j["report"]).read_text(errors="replace"), ""]
        md.write_text("\n".join(parts))
    return view_draft.render(md).as_uri()


def notification_href(pr: dict, pr_jobs: list[dict], cfg: dict, dry_run: bool) -> str:
    """Where clicking the notification goes, always somewhere that exists: Claude's
    drafted replies as a page, for comments on your PR or a follow-up or mention on
    someone else's; with no drafts ready, your PR opens a Claude session on its
    comments, and a re-request or responses whose drafts failed open GitHub."""
    ready = [j for j in pr_jobs if j.get("status") == "ready" and j.get("report")
             and Path(j["report"]).exists()]
    if pr["kind"] == "mine" and not ready:
        import session
        return session.respond_url(pr["url"])
    if ready:
        if dry_run:
            return f"(page of Claude's drafted replies: {', '.join(j['report'] for j in ready)})"
        return _report_page(pr, ready, cfg)
    first = next((j.get("url") for j in pr_jobs if j.get("url")), None)
    return first or pr["url"]


def deliver(notes: list[tuple], cfg: dict, dry_run: bool) -> None:
    """One popup per PR, red first, in its color's style and with its sound
    (watcher/popups.py send, which records it so it can be withdrawn once resolved);
    past max_notifs_per_tick, one summary banner. `notes` are (pr, color, reason,
    events, pr_jobs)."""
    nick = names.book(fetch=True)
    shown = [{**render.render_pr(pr, color, reason, evs, nick,
                                 notification_href(pr, js, cfg, dry_run)), "pr": pr}
             for pr, color, reason, evs, js in notes]
    shown.sort(key=lambda n: -priority.rank(n["color"]))
    room = cfg["watcher"]["max_notifs_per_tick"]
    for n in shown[:room]:
        if dry_run:
            print(f"[{n['color']}] {n['title']}\n    {n['subtitle']}\n    {n['body']}\n    {n['href']}")
            continue
        sent = popups.send(popups.PR, n["pr"], n["color"], n["title"], n["subtitle"], n["body"],
                           cfg, href=n["href"])
        _log_sent({**n, "style": sent["style"]})
    overflow = shown[room:]
    if overflow:
        msg = f"{len(overflow)} more PR{'s' if len(overflow) != 1 else ''} need you"
        if dry_run:
            print(f"[banner] {msg}")
        else:
            notify.banner("📬 More PR activity", msg, "Open the menu bar for the list")
            _log_sent({"style": "banner", "title": "📬 More PR activity", "subtitle": msg,
                       "body": "", "href": None})


def _log_sent(n: dict) -> None:
    """One line per notification handed to macOS, on stdout, which launchd appends to
    LOG. It records what was sent; whether macOS showed it is up to Focus and the
    notification settings."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{stamp} sent {n['style']}: {n['title']} | {n['subtitle']} | {n['body']} "
          f"→ {n.get('href') or '-'}", flush=True)


def _notice(cfg: dict, title: str, body: str, style: str = "banner") -> None:
    """A watcher health notice (not about a PR)."""
    n = render.render({"type": "notice", "title": title, "body": body, "style": style}, {})
    if style == "alert":
        notify.alert(n["title"], n["subtitle"], n["body"], href=n["href"],
                     plugin=cfg["notify"]["swiftbar_plugin"], **notify.sound_options(cfg))
    else:
        notify.banner(n["title"], n["subtitle"], n["body"], href=n["href"], group=n["group"])
    _log_sent(n)


def tick(cfg: dict, dry_run: bool = False, since: str | None = None) -> int:
    held = state.lock()
    if held is None:
        return 0
    st = state.load()
    now = time.time()
    if not dry_run and st["backoff"]["next_at"] > now:
        followup.start_queued(cfg)
        autofix.start_queued(cfg)
        return 0

    me, bots = cfg["me"], set(cfg["watcher"]["bots"])
    try:
        tok = gh.token()
        found, date = gh.search(me, cfg["org"], tok)
        if found["viewer"] != me:
            raise gh.GhError(f"GitHub token is for {found['viewer']}, config says {me}", 401)
        replay = since is not None
        prev = {} if replay else st["prs"]
        # The first tick that tracks notified colors records every PR's current color.
        seed_colors = not replay and "notified" not in st
        sweep = replay or seed_colors or now - (st.get("last_sweep_at") or 0) > SWEEP_EVERY
        # Every PR with a popup out is fetched on every tick, so its popup is withdrawn
        # within a tick of the work being done (watcher/popups.py).
        watched = set() if replay else popups.watched_ids(popups.load())
        todo = {pid: v["kind"] for pid, v in found["prs"].items()
                if sweep or pid in watched or pid not in prev
                or prev[pid].get("updated_at") != v["updated_at"]
                or prev[pid].get("head") != v["head"]}
        detail = gh.details(todo, bots, tok) if todo else {}
        gone = [pid for pid in prev if pid not in found["prs"]]
        # PRs whose follow-ups are still open but that the search no longer returns:
        # a PR that merged on an earlier tick while a job on it was running.
        orphans = (followup.orphaned_prs(followup.all_jobs(), found["prs"])
                   | autofix.orphaned_prs(autofix.all_jobs(), found["prs"])) - set(gone)
        check = gone + sorted(orphans)
        gone_states = gh.states(check, tok) if check else {}
    except gh.GhError as exc:
        if dry_run:
            print(f"GitHub error: {exc}", file=sys.stderr)
            return 1
        b = st["backoff"]
        b["fails"] += 1
        b["next_at"] = now + state.backoff_delay(b["fails"])
        b["last_error"] = str(exc)[:200]
        if (exc.status == 401 or b["fails"] >= 6) and not b["alerted"]:
            _notice(cfg, "⚠️ PR watcher can't reach GitHub", str(exc)[:200], "alert")
            b["alerted"] = True
        state.save(st)
        return 1

    server_now = _server_now(date)
    seeding = st["seeded_at"] is None and not replay
    if replay:
        cutoff = _iso(server_now - _parse_since(since))
    else:
        last_ok = st.get("last_ok_at")
        cutoff = _iso(datetime.fromisoformat(last_ok.replace("Z", "+00:00")) - FIRST_SIGHT_GRACE) if last_ok else None
    ctx = ev.Ctx(me=me, seeding=seeding, cutoff=cutoff, paths_changed=_paths_changed(tok),
                 real_work=_real_work(tok, cfg["org"], me))

    events, prs = [], dict(prev)
    for pid, pr in detail.items():
        found_events, prs[pid] = ev.diff_pr(pr, prev.get(pid), ctx)
        if pr["kind"] == "others":
            prs[pid]["approval"] = approval.needs(pr, me)
        events += found_events
    closed: dict[str, str] = {}     # PR id -> MERGED or CLOSED, for withdrawing its popups
    for pid in gone:
        if gone_states.get(pid) in ("CLOSED", "MERGED") or prs[pid].get("missing_ticks", 0) >= 30:
            prs.pop(pid, None)
            closed[pid] = gone_states.get(pid) or "CLOSED"
            if not dry_run:
                followup.cancel_for_pr(pid, gone_states.get(pid) or "CLOSED")
                autofix.cancel_for_pr(pid, gone_states.get(pid) or "CLOSED", cfg)
        else:
            prs[pid]["missing_ticks"] = prs[pid].get("missing_ticks", 0) + 1
    for pid in orphans:
        if gone_states.get(pid) in ("CLOSED", "MERGED"):
            closed[pid] = gone_states[pid]
            if not dry_run:
                followup.cancel_for_pr(pid, gone_states[pid])
                autofix.cancel_for_pr(pid, gone_states[pid], cfg)

    # Follow-ups you have answered yourself (replied, resolved, re-reviewed) stop counting.
    now_iso = _iso(server_now)
    jobs = followup.all_jobs()
    answered = [c for pr in detail.values() if pr["kind"] == "others"
                for c in followup.cleared_jobs(pr, jobs, me)]
    gone_jobs = {jid for jid, _ in answered}
    fixes_answered = [c for pr in detail.values() if pr["kind"] == "mine"
                      for c in autofix.cleared_jobs(pr, autofix.all_jobs(), me)]
    to_fix = autofix_candidates(detail, prev, ctx, cfg)
    if not dry_run:
        for jid, why in fixes_answered:
            autofix.clear(jid, why)
        for pr, t in to_fix:
            autofix.enqueue(pr, t, cfg)
        for jid, why in answered:
            followup.clear(jid, why)
        for e in events:
            if e["type"] == "thread":
                followup.enqueue(e)
            elif e["type"] == "mention":
                followup.enqueue_mention(e)
            elif e["type"] == "change_request":
                followup.enqueue_change_request(e)
    jobs = [j for j in followup.all_jobs() if j["id"] not in gone_jobs]
    rechecked = followup.recheck_reapprovals(
        [pr for pr in detail.values() if pr["kind"] == "others"], jobs, cfg, dry_run)
    notified = {} if replay else st.get("notified", {})
    groups_now: dict[str, dict] = {}
    notes = notifications(detail, events, jobs, notified, state.load_cleared(), now_iso,
                          lambda pr, js: pr_priority(pr, cfg, js, groups_now),
                          seed=seeding or seed_colors)
    new_answers, stale = plan_answers(detail, groups_now, jobs, autofix.all_jobs(), now_iso)
    if not dry_run:
        for jid, why in stale:
            followup.clear(jid, why)
        for job in new_answers:
            followup.enqueue_answer(job)
    stale_ids = {jid for jid, _ in stale}
    jobs = [j for j in jobs if j["id"] not in stale_ids] + new_answers
    pending = {} if replay else st.get("pending", {})
    ready = hold_for_drafts(notes, pending, jobs + event_jobs(events, jobs, now_iso), now_iso,
                            names.book(fetch=True), me,
                            required=cfg["watcher"]["required_approvals"],
                            still_colored=set(notified),
                            needs={pid: v.get("approval") for pid, v in prs.items()})
    deliver(ready, cfg, dry_run)
    owed_urls = set(st["owed"]) if isinstance(st.get("owed"), dict) else None
    settled = {} if replay else settle_popups(popups.load(), set(detail), set(notified), closed,
                                              me, tok, cfg, owed=owed_urls)
    fyis = []
    if not (replay or seeding or seed_colors):
        nick = names.book(fetch=True)
        fyis = approval_fyis(detail, events, groups_now, st.get("mine_groups") or {},
                             {n[0]["id"] for n in ready}, nick, cfg)
        fyis += owed_fyis(st, cfg, me, tok, nick, now_iso)
    st["mine_groups"] = {pid: g for pid, g in {**(st.get("mine_groups") or {}),
                                               **{k: v["group"] for k, v in groups_now.items()}}.items()
                         if pid in prs}
    if dry_run:
        for group, why in settled.items():
            print(f"-- would withdraw popup {group}: {why}")
        for it in fyis:
            print(f"-- would add FYI ⚪ {it['headline']} ({it['id']})")
        for pid, p in pending.items():
            print(f"[{notified.get(pid, {}).get('color')} · waits for Claude's drafted replies] "
                  f"{p['pr']['repo']}#{p['pr']['number']} · {p['pr']['title']}")
        for jid, why in answered + stale:
            print(f"-- would clear follow-up {jid}: {why}")
        for job in new_answers:
            print(f"-- would draft a reply to {job['actor']}'s {job['kind']} on "
                  f"{job['pr']['repo']}#{job['pr']['number']} ({job.get('url')})")
        for jid, outcome in rechecked:
            print(f"-- re-approval re-check for follow-up {jid}: {outcome}")
        for jid, why in fixes_answered:
            print(f"-- would clear auto-fix {jid}: {why}")
        for pr, t in to_fix:
            print(f"-- would queue an auto-fix: {pr['repo']}#{pr['number']} {t['author']}'s "
                  f"{t['kind']} on {t.get('path')} ({t.get('url')})")
        print(f"-- {len(events)} event(s) on {len({e['pr']['id'] for e in events})} PR(s); "
              f"{len(ready)} notification(s) now, {len(pending)} waiting for drafts; "
              f"{len(todo)} PR(s) fetched in detail; cutoff {cutoff}")
        jira_merge_pass(cfg, tok, dry_run=True)
        drafts_pass(cfg, dry_run=True)
        return 0

    st["notified"] = {pid: n for pid, n in notified.items() if pid in prs}
    st["pending"] = pending
    if st["backoff"]["alerted"]:
        _notice(cfg, "✅ PR watcher reconnected", "GitHub is reachable again")
    st.update(prs=prs, last_ok_at=_iso(server_now), rate=found["rate"],
              backoff={"fails": 0, "next_at": 0, "alerted": False})
    if sweep:
        st["last_sweep_at"] = now
    if seeding:
        st["seeded_at"] = _iso(server_now)
    started, logged_out = followup.start_queued(cfg)
    _, fixes_logged_out = autofix.start_queued(cfg)
    logged_out = logged_out or fixes_logged_out
    if logged_out and not st["claude_auth_alerted"]:
        _notice(cfg, "⚠️ claude CLI is logged out", "Follow-ups are waiting. Run: claude auth login", "alert")
    st["claude_auth_alerted"] = logged_out
    state.save(st)
    if settled:
        popups.withdraw(settled, log=lambda m: print(m, flush=True))
        _refetch_menu()
    for it in fyi.add(fyis, cfg):
        print(f"{_iso(datetime.now(timezone.utc))} sent fyi: {it['headline']} → "
              f"{it['pr'].get('url')}", flush=True)
    if jira_merge_pass(cfg, tok):
        _refetch_menu()
    drafts_pass(cfg)
    _refresh_menu(cfg)
    return 0


def jira_merge_pass(cfg: dict, tok: str, dry_run: bool = False, since: str | None = None) -> list[dict]:
    """Move the Jira ticket of each PR of yours that merged since the last tick
    (watcher/jira_merge.py). Never breaks the tick."""
    stamp = _iso(datetime.now(timezone.utc))
    try:
        return jira_merge.run(cfg, tok, dry_run=dry_run, since=since,
                              log=lambda m: print(f"{stamp} {m}", flush=True))
    except Exception as exc:  # noqa: BLE001 — a Jira or GitHub hiccup waits for the next tick
        print(f"{stamp} jira-merge failed: {exc}", flush=True)
        return []


def settle_popups(entries: dict[str, dict], checked: set[str], still_colored: set[str],
                  closed: dict[str, str], me: str, tok: str, cfg: dict,
                  owed: set[str] | None = None) -> dict[str, str]:
    """group -> why, for each popup whose item is done (watcher/popups.py): its PR
    merged or closed; a PR row classified this tick (`checked`) has no color left
    (`still_colored`, the PR ids in `notified`); an escalated auto-fix isn't escalated
    any more; or you reviewed a PR whose review-owed popup is out, which one GraphQL
    call reads for all of them, or (a teammate's PR) it left `owed`, the reviews you
    owe as of the last menu fetch (`st["owed"]`, kept by `owed_fyis`)."""
    out = popups.resolved_closed(entries, closed)
    out.update({g: w for g, w in popups.resolved_rows(entries, checked, still_colored).items()
                if g not in out})
    for g, e in entries.items():
        if e.get("kind") == popups.AUTOFIX and g not in out:
            try:
                job = autofix.load_job(e.get("job") or "")
            except (OSError, ValueError):
                job = None
            why = popups.autofix_resolution(e, job)
            if why:
                out[g] = why
    reviews = {g: e for g, e in popups.review_entries(entries).items() if g not in out}
    if reviews:
        try:
            live = gh.review_states([e["pr"]["url"] for e in reviews.values()], me, tok)
        except gh.GhError as exc:
            print(f"popups: couldn't read review states: {str(exc)[:200]}", flush=True)
            live = {}
        for g, e in reviews.items():
            why = popups.review_resolution(e, live.get((e["pr"]["url"] or "").rstrip("/")), me,
                                           owed)
            if why:
                out[g] = why
    return out


def _pr_of(pr: dict) -> dict:
    return {"url": pr["url"].rstrip("/"), "repo": pr["repo"], "number": pr["number"],
            "title": pr.get("title") or "", "id": pr.get("id")}


def _fyi_item(kind: str, pr: dict, headline: str, row: str, detail: str = "",
              who: str = "") -> dict:
    rest = priority.TICKET_RE.sub("", pr.get("title") or "").strip(" []:·-")
    return {"id": fyi.item_id(kind, pr, who), "kind": kind, "pr": _pr_of(pr),
            "headline": headline, "row": row, "detail": detail, "subtitle": rest}


def approval_fyis(detail: dict[str, dict], events: list[dict], groups_now: dict[str, dict],
                  groups_before: dict[str, str], delivered: set[str], nick: dict[str, str],
                  cfg: dict, praise=None) -> list[dict]:
    """FYIs for this tick's approvals of your PRs (watcher/fyi.py): one per approval
    that asks nothing (no inline comments, and a body that's empty or only praises),
    or, when the PR became Ready this tick, one Ready item instead. A PR that got an
    action popup this tick (`delivered`) gets none: that popup already says it.

    Became Ready: its sub-header is Ready now (`groups_now`, from this tick's
    classification) and wasn't at the last classification (`groups_before`), or,
    with no earlier classification, it had fewer than the required approvals before
    this tick's approvals."""
    praise = praise or (lambda body: fyi.praise_only(body, cfg))
    required = cfg["watcher"]["required_approvals"]
    by_pr: dict[str, list[dict]] = {}
    for e in events:
        if e["type"] == "decision" and e.get("state") == "APPROVED":
            by_pr.setdefault(e["pr"]["id"], []).append(e)
    out = []
    for pid, approvals in by_pr.items():
        pr = detail.get(pid)
        if not pr or pr["kind"] != "mine" or pid in delivered:
            continue
        key = priority.ticket(pr["title"], pr["url"])
        quiet = [e for e in approvals if not e.get("count") and praise(e.get("body") or "")]
        now = groups_now.get(pid) or {}
        before = groups_before.get(pid)
        if before is None:
            was_ready = len(pr.get("approved_by") or []) - len(approvals) >= required
        else:
            was_ready = before == priority.READY
        if now.get("group") == priority.READY and not was_ready and len(quiet) == len(approvals):
            who = priority.names([e["actor"] for e in approvals], nick)
            out.append(_fyi_item(fyi.READY, pr, f"{key} · {now.get('reason') or 'Ready'}",
                                 f"{now.get('reason') or 'Ready'} · {who} approved",
                                 f"{who} approved"))
            continue
        for e in quiet:
            who = priority.who(e["actor"], nick)
            said = fyi.strip_footer(e.get("body") or "")
            if said:
                line = comment_kind.summary(said, 60)
                out.append(_fyi_item(fyi.APPROVED, pr, f"{who} approved {key}: “{line}”",
                                     f"{who} approved: “{line}”", who=e["actor"]))
            else:
                out.append(_fyi_item(fyi.APPROVED, pr, f"{who} approved {key}, no comment",
                                     f"{who} approved, no comment", who=e["actor"]))
    return out


def owed_fyis(st: dict, cfg: dict, me: str, tok: str, nick: dict[str, str],
              now_iso: str) -> list[dict]:
    """FYIs for reviews you owed that stopped being owed without you: the PR merged or
    closed, or you were removed as a reviewer (popups.review_outcome). Reads the
    reviews you owe from the menu's cached triage (review/auto_draft.py
    classify_owed), once per new fetch, and keeps the last set in `st["owed"]` (url ->
    {since, title, team}); the first look only records it. One GraphQL call reads the
    PRs that left the set. A GitHub error leaves the set as it was, to retry next tick.
    A teammate's PR (`team`, triage/priority.py team_pr) never requested you, so for it
    only the PR closing counts: leaving the set otherwise (approvals in, a change
    request waiting on the author) is no news."""
    from review import auto_draft
    cache, _ = auto_draft.cached_triage()
    stamp = cache.get("fetched_at")
    if not stamp or stamp == st.get("owed_fetched_at") or "reviews" not in cache:
        return []
    now_dt = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    owed_now = {e["url"].rstrip("/"): e
                for e in auto_draft.classify_owed(cache, cfg, [], nick, now_dt, None)}
    before = st.get("owed")
    vanished = {u: v for u, v in (before or {}).items() if u not in owed_now}
    live = {}
    if vanished:
        try:
            live = gh.review_states(list(vanished), me, tok)
        except gh.GhError as exc:
            print(f"fyi: couldn't read the reviews no longer owed: {str(exc)[:200]}", flush=True)
            return []
    st["owed_fetched_at"] = stamp
    st["owed"] = {u: {**((before or {}).get(u) or {"since": now_iso, "title": e.get("title") or ""}),
                      "team": bool((e.get("st") or {}).get("team_ask"))}
                  for u, e in owed_now.items()}
    out = []
    for url, v in vanished.items():
        outcome = popups.review_outcome(live.get(url), v.get("since") or "", bool(v.get("team")))
        if not outcome or outcome[0] == "reviewed":
            continue
        parts = url.split("/")
        pr = {"url": url, "repo": parts[-3], "number": int(parts[-1]), "title": v.get("title") or ""}
        key = priority.ticket(pr["title"], url)
        out.append(_fyi_item(fyi.NOT_OWED, pr, f"Review no longer needed: {key} · {outcome[1]}",
                             f"Review no longer needed: {outcome[1]}"))
    return out


def _refetch_menu() -> None:
    """Start the menu's background fetch now (unless one is running), so a row whose
    popup was just withdrawn leaves the menu in the same minute."""
    from review import auto_draft
    auto_draft.refresh_triage()


def plan_answers(detail: dict[str, dict], groups_now: dict[str, dict], jobs: list[dict],
                 fix_jobs: list[dict], now: str) -> tuple[list[dict], list[tuple[str, str]]]:
    """(answer jobs to queue, (job id, why) to clear) for your PRs classified this tick
    (`groups_now` carries each one's asks): a reply draft for each comment newly waiting
    on you that no auto-fix is working on, and clearing for each whose comment no longer
    waits on you (watcher/followup.py)."""
    new, stale = [], []
    for pid, g in groups_now.items():
        pr = detail.get(pid)
        if not pr or pr["kind"] != "mine":
            continue
        asks = g.get("asks") or []
        fixing = {j.get("thread_id") for j in fix_jobs
                  if (j.get("pr") or {}).get("id") == pid and j.get("status") in UNFINISHED}
        roots = {t["id"]: (t["comments"][0]["id"] if t.get("comments") else None)
                 for t in pr.get("threads") or []}
        for ask in followup.answers_to_queue(pr, asks, jobs, fixing):
            new.append(followup.answer_job(ev._pr_ref(pr), ask, roots.get(ask["thread_id"]), now))
        stale += followup.stale_answers(pid, asks, jobs)
    return new, stale


def drafts_pass(cfg: dict, dry_run: bool = False, **kw) -> list[dict]:
    """The review-request pass: while `[review] auto_draft` is on, the automatic
    drafts (review/auto_draft.py tick), whose popups come with each draft; while it's
    off, a popup for each new review you owe, with no draft (auto_draft.announce). A
    failure is logged and never stops the watcher."""
    from review import auto_draft
    log = lambda m: print(m, flush=True)  # noqa: E731
    try:
        if auto_draft.enabled(cfg):
            return auto_draft.tick(cfg, dry_run, log=log, **kw)
        return auto_draft.announce(cfg, dry_run, log=log)
    except Exception as exc:  # noqa: BLE001
        print(f"drafts: the pass failed: {str(exc)[:200]}", flush=True)
        return []


def autofix_candidates(detail: dict[str, dict], prev: dict, ctx: ev.Ctx,
                       cfg: dict) -> list[tuple[dict, dict]]:
    """(PR, thread) for each thread a nit or suggestion opened on your PRs this tick
    (events.opened_threads, autofix.candidates); none while `[autofix] enabled` is off."""
    if not cfg["autofix"]["enabled"]:
        return []
    return [(pr, t) for pid, pr in detail.items() if pr["kind"] == "mine"
            for t in autofix.candidates(ev.opened_threads(pr, prev.get(pid), ctx),
                                        cfg["autofix"]["categories"])]


SETTLE_WAIT = 120   # seconds `settle` waits for a running tick to finish


def settle(cfg: dict, wait: float = SETTLE_WAIT, sleep=time.sleep) -> int:
    """One tick right after a menu action (Approve, Post this reply, Dismiss), so the
    popup for what you just did is withdrawn, and the menu row updated, within seconds
    instead of at the next scheduled tick. The tick does the deciding: it re-reads the
    PR, clears the follow-ups you answered, and withdraws the popup only when nothing
    is left on you (`settle_popups`); a PR that still needs you keeps it.

    A tick already running may have read the PR before the action, so this waits for
    it to finish (up to `wait` seconds) and then runs its own. If a scheduled tick
    takes the lock in between, that tick started after the action and does the same."""
    deadline = time.time() + wait
    while (held := state.lock()) is None:
        if time.time() > deadline:
            return 0
        sleep(1)
    held.close()
    return tick(cfg)


def settle_soon(popen=subprocess.Popen) -> None:
    """Start `settle` detached, logging to LOG, so the menu action returns at once."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as log:
        popen([sys.executable, "-m", "watcher", "settle"], cwd=str(Path(__file__).resolve().parent.parent),
              stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
              start_new_session=True)


def _refresh_menu(cfg: dict) -> None:
    subprocess.run(["open", "-g", f"swiftbar://refreshplugin?name={cfg['notify']['swiftbar_plugin']}"],
                   capture_output=True)


def status(cfg: dict) -> int:
    st = state.load()
    jobs = followup.all_jobs()
    counts: dict[str, int] = {}
    for j in jobs:
        counts[j["status"]] = counts.get(j["status"], 0) + 1
    print(json.dumps({
        "seeded_at": st["seeded_at"], "last_ok_at": st["last_ok_at"],
        "backoff": st["backoff"], "rate": st["rate"],
        "prs_tracked": len(st["prs"]),
        "threads_armed": sum(len(p.get("threads", {})) for p in st["prs"].values()),
        "followups": counts,
        "autofixes": {s: sum(1 for j in autofix.all_jobs() if j["status"] == s)
                      for s in sorted({j["status"] for j in autofix.all_jobs()})},
        "autofix_enabled": cfg["autofix"]["enabled"], "autofix_dry_run": cfg["autofix"]["dry_run"],
        "auto_draft": cfg["review"]["auto_draft"],
        "drafts": _draft_counts(),
        "launchd_loaded": subprocess.run(["launchctl", "print", f"gui/{_uid()}/{LABEL}"],
                                         capture_output=True).returncode == 0,
    }, indent=2))
    return 0


def _draft_counts() -> dict[str, int]:
    from review import auto_draft
    out: dict[str, int] = {}
    for job in auto_draft.all_jobs().values():
        out[job.get("status") or "?"] = out.get(job.get("status") or "?", 0) + 1
    return out


def _uid() -> int:
    import os
    return os.getuid()


def install(cfg: dict) -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "watcher", "tick"],
        "WorkingDirectory": str(settings.REPO_DIR),
        "StartInterval": int(cfg["watcher"]["poll_interval"]),
        "RunAtLoad": True,
        "Nice": 5,
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
        "EnvironmentVariables": {
            "PATH": f"{Path('~/.local/bin').expanduser()}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        },
    }
    lessons = {
        "Label": LESSONS_LABEL,
        "ProgramArguments": [sys.executable, str(settings.REPO_DIR / "review" / "harvest_lessons.py"),
                             "--apply"],
        "WorkingDirectory": str(settings.REPO_DIR),
        "StartCalendarInterval": {"Weekday": 1, "Hour": 7, "Minute": 0},
        "Nice": 10,
        "StandardOutPath": str(LESSONS_LOG),
        "StandardErrorPath": str(LESSONS_LOG),
        "EnvironmentVariables": plist["EnvironmentVariables"],
    }
    rc = 0
    for label, path, body in ((LABEL, PLIST, plist), (LESSONS_LABEL, LESSONS_PLIST, lessons)):
        path.write_bytes(plistlib.dumps(body))
        subprocess.run(["launchctl", "bootout", f"gui/{_uid()}/{label}"], capture_output=True)
        proc = subprocess.run(["launchctl", "bootstrap", f"gui/{_uid()}", str(path)],
                              capture_output=True, text=True)
        print(f"wrote {path}; launchctl bootstrap exit {proc.returncode} {proc.stderr.strip()}")
        rc = rc or proc.returncode
    return rc


def notify_test(cfg: dict, withdraw_after: int = 0) -> int:
    """A red popup, then a yellow one, each in its style and with its sound, so you can
    see and hear both. With `withdraw_after`, both are withdrawn that many seconds
    later, as a resolved item's popup would be."""
    groups = []
    for color, title, body in (
            ("red", "🔴 ABC-123 · Test: someone answered your blocking comment",
             "Red: needs you now. This one stays until you close it."),
            ("yellow", "🟡 ABC-124 · Test: someone answered your nit",
             "Yellow: act soon. It slides away, with the softer chime.")):
        group = f"pr-digest-test-{color}"
        style = notify.popup(color, title, "Test notification from the PR watcher", body, cfg,
                             group=group)
        print(f"sent {color} as {'an alert' if style == 'alert' else 'a banner'} ({group})")
        groups.append(group)
        time.sleep(2)
    if not notify.alert_notifier():
        print("The alert app isn't installed, so red went through SwiftBar and can't be "
              "withdrawn. Build it with: bin/watcher alert-app")
    if withdraw_after:
        time.sleep(withdraw_after)
        for group in groups:
            notify.withdraw(group)
        print(f"withdrew both after {withdraw_after}s")
    return 0


def alert_app(cfg: dict) -> int:
    """Build the alert app (notify.install_alert_app), then send one alert through it
    so macOS asks to allow its notifications."""
    try:
        path = notify.install_alert_app()
    except (OSError, RuntimeError) as exc:
        print(f"couldn't build the alert app: {exc}", file=sys.stderr)
        return 1
    print(f"built {path}")
    notify.alert("🔴 PR Digest Alerts is set up", "Red PR notifications come from this app",
                 "Allow its notifications, and keep its style on Alerts in System Settings → "
                 "Notifications → PR Digest Alerts.", plugin=cfg["notify"]["swiftbar_plugin"],
                 group="pr-digest-alert-app", **notify.sound_options(cfg))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="watcher")
    sub = parser.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tick")
    t.add_argument("--dry-run", action="store_true")
    t.add_argument("--since", help="with --dry-run: replay this window, e.g. 24h")
    sub.add_parser("status")
    sub.add_parser("settle", help="one tick after a menu action, once a running tick ends")
    nt = sub.add_parser("notify-test")
    nt.add_argument("--withdraw-after", type=int, default=0, metavar="SECONDS",
                    help="withdraw both test popups this many seconds later")
    sub.add_parser("alert-app")
    sub.add_parser("install")
    for name in ("post", "dismiss", "followup-run"):
        sub.add_parser(name).add_argument("job")
    ap = sub.add_parser("approve", help="approve someone else's PR (the menu's Approve)")
    ap.add_argument("pr", help="the PR's GraphQL node id")
    ap.add_argument("head", help="the head commit the menu showed as ready")
    d = sub.add_parser("drafts")
    d.add_argument("--dry-run", action="store_true", help="say what it would do; change nothing")
    d.add_argument("--limit", type=int, help="start at most this many drafts or updates")
    d.add_argument("--only", help="act on this PR url only")
    d.add_argument("--print-popups", action="store_true",
                   help="print the notifications instead of sending them")
    sub.add_parser("draft-run").add_argument("job", help="<repo>#<n>")
    a = sub.add_parser("autofix-run")
    a.add_argument("job", help="a job id, or (with --dry-run) a review comment link")
    a.add_argument("--dry-run", action="store_true",
                   help="assess, edit, scope-check and validate, then stop and print")
    jm = sub.add_parser("jira-merge", help="move the Jira tickets of PRs merged since the last look")
    jm.add_argument("--dry-run", action="store_true", help="say what it would do; change nothing")
    jm.add_argument("--since", help="with --dry-run: replay merges in this window, e.g. 30d")
    args = parser.parse_args(argv)

    cfg = settings.load()
    if not cfg["me"]:
        print("Set `me` in ~/.config/pr-digest/config.toml", file=sys.stderr)
        return 2
    if args.cmd == "tick":
        if args.since and not args.dry_run:
            parser.error("--since only works with --dry-run")
        return tick(cfg, args.dry_run, args.since)
    if args.cmd == "status":
        return status(cfg)
    if args.cmd == "settle":
        return settle(cfg)
    if args.cmd == "notify-test":
        return notify_test(cfg, args.withdraw_after)
    if args.cmd == "alert-app":
        return alert_app(cfg)
    if args.cmd == "install":
        return install(cfg)
    if args.cmd == "post":
        # One job id, or several joined by commas ("Post the drafted replies").
        rc = max(followup.post(jid) for jid in args.job.split(",") if jid)
        _refresh_menu(cfg)
        settle_soon()
        return rc
    if args.cmd == "dismiss":
        # One job id, or several joined by commas (a PR's row dismisses all its jobs).
        rc = max(followup.dismiss(jid) for jid in args.job.split(",") if jid)
        _refresh_menu(cfg)
        settle_soon()
        return rc
    if args.cmd == "approve":
        rc = followup.approve(args.pr, args.head, cfg)
        _refresh_menu(cfg)
        settle_soon()
        return rc
    if args.cmd == "followup-run":
        rc = followup.run(args.job, cfg)
        _refresh_menu(cfg)
        return rc
    if args.cmd == "drafts":
        from review import auto_draft
        if not auto_draft.enabled(cfg):
            print("[review] auto_draft is off (or reviewer_agent is empty)", file=sys.stderr)
            return 2
        deliver = None
        if args.print_popups:
            deliver = lambda ns: auto_draft.notify_reviews(ns, cfg, dry_run=True)  # noqa: E731
        auto_draft.tick(cfg, args.dry_run, limit=args.limit, only=args.only, deliver=deliver)
        if not args.dry_run:
            _refresh_menu(cfg)
        return 0
    if args.cmd == "draft-run":
        from review import auto_draft
        rc = auto_draft.run_job(args.job, cfg)
        # Send the review's notification now rather than at the next tick, unless a
        # tick holds the lock (it sends it then), so the row and the popup arrive together.
        job = auto_draft.load_job(args.job)
        if job and state.lock() is not None:
            drafts_pass(cfg, only=job["pr"]["url"])
        _refresh_menu(cfg)
        return rc
    if args.cmd == "jira-merge":
        if args.since and not args.dry_run:
            parser.error("--since only works with --dry-run")
        since = _iso(datetime.now(timezone.utc) - _parse_since(args.since)) if args.since else None
        if not args.dry_run and state.lock() is None:
            print("a tick is running; it does this pass itself", file=sys.stderr)
            return 0
        jira_merge_pass(cfg, gh.token(), dry_run=args.dry_run, since=since)
        return 0
    if args.cmd == "autofix-run":
        rc = autofix.run_command(args.job, cfg, args.dry_run)
        if not args.dry_run:
            _refresh_menu(cfg)
        return rc
    return 2


if __name__ == "__main__":
    sys.exit(main())
