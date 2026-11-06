"""PR activity watcher. launchd runs `tick` every poll_interval seconds.

    python -m watcher tick [--dry-run] [--since 24h]   one poll (dry-run: print, send nothing)
    python -m watcher status                           last poll, backoff, follow-ups
    python -m watcher post <job>[,<job>…] | dismiss <job>[,<job>…]   menu actions on follow-ups
    python -m watcher followup-run <job>               one follow-up (started by tick)
    python -m watcher notify-test                      a red and a yellow alert, each with its sound
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
(state/watcher/cleared.json); the next response then notifies afresh. Uncolored
activity, green review requests and approvals never notify.

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
from triage import priority  # noqa: E402
from watcher import followup, gh, render, state  # noqa: E402

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


def pr_priority(pr: dict, cfg: dict, jobs: list[dict]) -> tuple[str | None, str]:
    """(color, reason) for one PR snapshot, through triage/priority.py. Your PRs take
    a fresh triage/mine.py row (one gh call); others' take their open follow-up jobs
    (`jobs`, including this tick's new responses) and any pending re-request."""
    nick = names.book(fetch=True)
    if pr["kind"] == "mine":
        try:
            row = mine_triage.row_for(cfg["org"], pr["repo"], pr["number"], pr["title"],
                                      pr["url"], cfg["me"], set(cfg["watcher"]["bots"]))
        except (mine_triage.GhError, KeyError, ValueError, TypeError):
            return None, ""
        p = priority.mine(row, nick, cfg["watcher"]["required_approvals"],
                          stale_after_days=cfg["priority"]["stale_after_days"])
        return p["color"], p["reason"]
    f = priority.followup([j for j in jobs if (j.get("pr") or {}).get("id") == pr["id"]],
                          nick, author=pr["author"],
                          rerequested_at=ev.pending_rerequest(pr, cfg["me"]),
                          approvals=len(pr.get("approved_by") or []),
                          required=cfg["watcher"]["required_approvals"])
    return (f["color"], f["reason"]) if f else (None, "")


def _event_job(e: dict, now: str) -> dict:
    """A thread or mention event in the shape of the follow-up job it queues, so a dry
    run classifies this tick's responses the same way as a real one."""
    return {"id": followup.event_job_id(e), "status": "queued", "created_at": now,
            "pr": e["pr"], "trigger": "mention" if e["type"] == "mention" else e.get("trigger"),
            **{k: e.get(k) for k in ("actor", "thread_id", "arm_id", "path", "line", "url",
                                     "blocking", "where", "comment_id", "body", "mentioned_at")}}


def event_jobs(events: list[dict], jobs: list[dict], now: str) -> list[dict]:
    """Jobs for this tick's thread and mention events that aren't queued yet."""
    known = {j["id"] for j in jobs}
    return [_event_job(e, now) for e in events
            if e["type"] in ("thread", "mention") and followup.event_job_id(e) not in known]


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
                    max_minutes: int = PENDING_MAX_MINUTES, required: int = 2) -> list[tuple]:
    """Which notifications go out now, as [(pr, color, reason, events, pr_jobs)].

    A follow-up or mention whose drafts are still being written waits in `pending`
    (PR id -> {pr, events, rerequested_at, since}). Each tick releases the ones whose
    jobs have all finished, reclassified from those jobs, or that waited longer than
    `max_minutes`; one whose row cleared meanwhile is dropped."""
    def pr_jobs(pid: str) -> list[dict]:
        return [j for j in jobs if (j.get("pr") or {}).get("id") == pid]

    out, held_now = [], set()
    for pr, color, reason, evs in notes:
        js = pr_jobs(pr["id"])
        if pr["kind"] == "others" and any(j.get("status") in UNFINISHED for j in js):
            pending[pr["id"]] = {
                "pr": {k: pr[k] for k in ("id", "kind", "repo", "number", "title", "url",
                                          "author", "head")},
                "events": [{k: e.get(k) for k in _EVENT_KEYS} for e in evs],
                "rerequested_at": ev.pending_rerequest(pr, me) if "reviews" in pr else "",
                "since": now}
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
        known = "approved_by" in p["pr"]
        f = priority.followup(js, nick, author=p["pr"]["author"],
                              rerequested_at=p.get("rerequested_at") or "",
                              approvals=len(p["pr"]["approved_by"]) if known else None,
                              required=required)
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
    """Where clicking the notification goes, always somewhere that exists: your PRs
    open a Claude session on the comments; a follow-up or mention opens Claude's
    drafted replies; a re-request, or responses whose drafts failed, open GitHub."""
    if pr["kind"] == "mine":
        import session
        return session.respond_url(pr["url"])
    ready = [j for j in pr_jobs if j.get("status") == "ready" and j.get("report")
             and Path(j["report"]).exists()]
    if ready:
        if dry_run:
            return f"(page of Claude's drafted replies: {', '.join(j['report'] for j in ready)})"
        return _report_page(pr, ready, cfg)
    first = next((j.get("url") for j in pr_jobs if j.get("url")), None)
    return first or pr["url"]


def deliver(notes: list[tuple], cfg: dict, dry_run: bool) -> None:
    """One alert per PR, red first, with its color's sound; past max_notifs_per_tick,
    one summary banner. `notes` are (pr, color, reason, events, pr_jobs)."""
    nick = names.book(fetch=True)
    shown = [render.render_pr(pr, color, reason, evs, nick,
                              notification_href(pr, js, cfg, dry_run))
             for pr, color, reason, evs, js in notes]
    shown.sort(key=lambda n: -priority.rank(n["color"]))
    room = cfg["watcher"]["max_notifs_per_tick"]
    for n in shown[:room]:
        if dry_run:
            print(f"[{n['color']}] {n['title']}\n    {n['subtitle']}\n    {n['body']}\n    {n['href']}")
            continue
        notify.alert(n["title"], n["subtitle"], n["body"], href=n["href"],
                     plugin=cfg["notify"]["swiftbar_plugin"], group=n["group"],
                     **notify.sound_options(cfg, n["color"]))
        _log_sent(n)
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
        todo = {pid: v["kind"] for pid, v in found["prs"].items()
                if sweep or pid not in prev or prev[pid].get("updated_at") != v["updated_at"]
                or prev[pid].get("head") != v["head"]}
        detail = gh.details(todo, bots, tok) if todo else {}
        gone = [pid for pid in prev if pid not in found["prs"]]
        # PRs whose follow-ups are still open but that the search no longer returns:
        # a PR that merged on an earlier tick while a job on it was running.
        orphans = followup.orphaned_prs(followup.all_jobs(), found["prs"]) - set(gone)
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
    ctx = ev.Ctx(me=me, seeding=seeding, cutoff=cutoff, paths_changed=_paths_changed(tok))

    events, prs = [], dict(prev)
    for pid, pr in detail.items():
        found_events, prs[pid] = ev.diff_pr(pr, prev.get(pid), ctx)
        events += found_events
    for pid in gone:
        if gone_states.get(pid) in ("CLOSED", "MERGED") or prs[pid].get("missing_ticks", 0) >= 30:
            prs.pop(pid, None)
            if not dry_run:
                followup.cancel_for_pr(pid, gone_states.get(pid) or "CLOSED")
        else:
            prs[pid]["missing_ticks"] = prs[pid].get("missing_ticks", 0) + 1
    for pid in orphans:
        if gone_states.get(pid) in ("CLOSED", "MERGED") and not dry_run:
            followup.cancel_for_pr(pid, gone_states[pid])

    # Follow-ups you have answered yourself (replied, resolved, re-reviewed) stop counting.
    now_iso = _iso(server_now)
    jobs = followup.all_jobs()
    answered = [c for pr in detail.values() if pr["kind"] == "others"
                for c in followup.cleared_jobs(pr, jobs, me)]
    gone_jobs = {jid for jid, _ in answered}
    if not dry_run:
        for jid, why in answered:
            followup.clear(jid, why)
        for e in events:
            if e["type"] == "thread":
                followup.enqueue(e)
            elif e["type"] == "mention":
                followup.enqueue_mention(e)
    jobs = [j for j in followup.all_jobs() if j["id"] not in gone_jobs]
    rechecked = followup.recheck_reapprovals(
        [pr for pr in detail.values() if pr["kind"] == "others"], jobs, cfg, dry_run)
    notified = {} if replay else st.get("notified", {})
    notes = notifications(detail, events, jobs, notified, state.load_cleared(), now_iso,
                          lambda pr, js: pr_priority(pr, cfg, js), seed=seeding or seed_colors)
    pending = {} if replay else st.get("pending", {})
    ready = hold_for_drafts(notes, pending, jobs + event_jobs(events, jobs, now_iso), now_iso,
                            names.book(fetch=True), me,
                            required=cfg["watcher"]["required_approvals"])
    deliver(ready, cfg, dry_run)
    if dry_run:
        for pid, p in pending.items():
            print(f"[{notified.get(pid, {}).get('color')} · waits for Claude's drafted replies] "
                  f"{p['pr']['repo']}#{p['pr']['number']} · {p['pr']['title']}")
        for jid, why in answered:
            print(f"-- would clear follow-up {jid}: {why}")
        for jid, outcome in rechecked:
            print(f"-- re-approval re-check for follow-up {jid}: {outcome}")
        print(f"-- {len(events)} event(s) on {len({e['pr']['id'] for e in events})} PR(s); "
              f"{len(ready)} notification(s) now, {len(pending)} waiting for drafts; "
              f"{len(todo)} PR(s) fetched in detail; cutoff {cutoff}")
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
    if logged_out and not st["claude_auth_alerted"]:
        _notice(cfg, "⚠️ claude CLI is logged out", "Follow-ups are waiting. Run: claude auth login", "alert")
    st["claude_auth_alerted"] = logged_out
    state.save(st)
    _refresh_menu(cfg)
    return 0


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
        "launchd_loaded": subprocess.run(["launchctl", "print", f"gui/{_uid()}/{LABEL}"],
                                         capture_output=True).returncode == 0,
    }, indent=2))
    return 0


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


def notify_test(cfg: dict) -> int:
    """A red alert, then a yellow one, each with its sound, so you can hear both."""
    for color, title, body in (
            ("red", "🔴 ABC-123 · Test: someone answered your blocking comment",
             "Red: needs you now. This one stays until you close it."),
            ("yellow", "🟡 ABC-124 · Test: someone answered your nit",
             "Yellow: waiting a while. The softer chime.")):
        notify.alert(title, "Test notification from the PR watcher", body,
                     plugin=cfg["notify"]["swiftbar_plugin"],
                     **notify.sound_options(cfg, color))
        time.sleep(2)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="watcher")
    sub = parser.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tick")
    t.add_argument("--dry-run", action="store_true")
    t.add_argument("--since", help="with --dry-run: replay this window, e.g. 24h")
    sub.add_parser("status")
    sub.add_parser("notify-test")
    sub.add_parser("install")
    for name in ("post", "dismiss", "followup-run"):
        sub.add_parser(name).add_argument("job")
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
    if args.cmd == "notify-test":
        return notify_test(cfg)
    if args.cmd == "install":
        return install(cfg)
    if args.cmd == "post":
        # One job id, or several joined by commas ("Post the drafted replies").
        rc = max(followup.post(jid) for jid in args.job.split(",") if jid)
        _refresh_menu(cfg)
        return rc
    if args.cmd == "dismiss":
        # One job id, or several joined by commas (a PR's row dismisses all its jobs).
        rc = max(followup.dismiss(jid) for jid in args.job.split(",") if jid)
        _refresh_menu(cfg)
        return rc
    if args.cmd == "followup-run":
        rc = followup.run(args.job, cfg)
        _refresh_menu(cfg)
        return rc
    return 2


if __name__ == "__main__":
    sys.exit(main())
