"""PR activity watcher. launchd runs `tick` every poll_interval seconds.

    python -m watcher tick [--dry-run] [--since 24h]   one poll (dry-run: print, send nothing)
    python -m watcher status                           last poll, backoff, follow-ups
    python -m watcher post <job> | dismiss <job>[,<job>…]   menu bar actions on follow-ups
    python -m watcher followup-run <job>               one follow-up (started by tick)
    python -m watcher notify-test                      one alert and one banner
    python -m watcher install                          write and load the launchd jobs (the
                                                       watcher, and the weekly lessons refresh)

The first tick with no saved state records everything as seen and sends nothing.
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
import notify  # noqa: E402
import settings  # noqa: E402
from watcher import events as ev  # noqa: E402
from watcher import followup, gh, render, state  # noqa: E402

LABEL = "com.josh.pr-digest-watcher"
PLIST = Path(f"~/Library/LaunchAgents/{LABEL}.plist").expanduser()
LOG = Path("~/.cache/pr-digest/watcher.log").expanduser()
# The weekly review-lessons refresh (review/harvest_lessons.py --apply), Mondays 07:00.
LESSONS_LABEL = "com.josh.pr-digest-lessons"
LESSONS_PLIST = Path(f"~/Library/LaunchAgents/{LESSONS_LABEL}.plist").expanduser()
LESSONS_LOG = Path("~/.cache/pr-digest/lessons.log").expanduser()
SWEEP_EVERY = 900
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


def deliver(events: list[dict], cfg: dict, dry_run: bool) -> None:
    nick = cfg.get("nicknames", {})
    req = cfg["watcher"]["required_approvals"]
    shown = [(e, render.render(e, nick, req)) for e in events]
    alerts = [s for s in shown if s[1]["style"] == "alert"]
    banners = [s for s in shown if s[1]["style"] == "banner"]
    room = max(cfg["watcher"]["max_notifs_per_tick"] - len(alerts), 0)
    overflow = banners[room:]
    for e, n in alerts + banners[:room]:
        if dry_run:
            print(f"[{n['style']}] {n['title']}\n    {n['subtitle']}\n    {n['body']}\n    {n['href']}")
            continue
        if n["style"] == "alert":
            notify.alert(n["title"], n["subtitle"], n["body"], href=n["href"],
                         plugin=cfg["notify"]["swiftbar_plugin"], sound=cfg["notify"]["alert_sound"])
        else:
            notify.banner(n["title"], n["subtitle"], n["body"], href=n["href"], group=n["group"])
        _log_sent(n)
    if overflow:
        prs = {e["pr"]["id"] for e, _ in overflow}
        msg = f"{len(overflow)} more updates on {len(prs)} PR{'s' if len(prs) != 1 else ''}"
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
    deliver([{"type": "notice", "title": title, "body": body, "style": style}], cfg, False)


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
        sweep = replay or now - (st.get("last_sweep_at") or 0) > SWEEP_EVERY
        todo = {pid: v["kind"] for pid, v in found["prs"].items()
                if sweep or pid not in prev or prev[pid].get("updated_at") != v["updated_at"]
                or prev[pid].get("head") != v["head"]}
        detail = gh.details(todo, bots, tok) if todo else {}
        gone = [pid for pid in prev if pid not in found["prs"]]
        gone_states = gh.states(gone, tok) if gone else {}
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
                followup.cancel_for_pr(pid)
        else:
            prs[pid]["missing_ticks"] = prs[pid].get("missing_ticks", 0) + 1

    deliver(events, cfg, dry_run)
    if dry_run:
        print(f"-- {len(events)} event(s); {len(todo)} PR(s) fetched in detail; cutoff {cutoff}")
        return 0

    for e in events:
        if e["type"] == "thread":
            followup.enqueue(e)
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
    notify.alert("✅ Test alert from the PR watcher", "Stays until you close it",
                 "Approvals, change requests and verdicts look like this",
                 plugin=cfg["notify"]["swiftbar_plugin"], sound=cfg["notify"]["alert_sound"])
    notify.banner("💬 Test banner from the PR watcher", "Slides away on its own",
                  "Comments and replies look like this")
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
        rc = followup.post(args.job)
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
