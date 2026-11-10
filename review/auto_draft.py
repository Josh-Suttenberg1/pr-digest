"""Review drafts that start themselves, and update themselves on new commits.

While `[review] auto_draft` is on (and review.reviewer_agent isn't empty), every watcher
tick (`tick`) runs this over the reviews you owe: the OWED entries triage/priority.py
classify gives the menu's cached triage (swiftbar_plugin's cache.json), in the order
the review inbox ranks them. A PR with two approvals is never one of them unless its
author is in `[triage] pod`. For each:

  no draft yet          start a full draft (`bin/review-pr <PR url>`, headless), with no
                        limit on how many run at once. A failed draft isn't retried for
                        the same head; a new push retries it once pushes stop.
  draft for an older    the head moved. As soon as the tick sees a new head it computes
  head                  the interdiff (review/interdiff.py). An empty one (a merge from
                        the base that changes nothing the PR changes) carries the draft
                        forward to the new head, so it never reads as outdated. Otherwise,
                        once the head has been still for `quiet_minutes`, an update starts:
                        an update review (review/update_review.py) of the interdiff alone,
                        or a full review when the interdiff passes `update_max_share` of
                        the PR's changed lines or touches files the draft never saw.
  draft current         nothing to do.

Each PR's scheduling state is one file, state/drafts/<repo>#<n>.json: when the request
was first seen (`seen_at`), the head and when it first appeared (`head`), the run
(`status` idle | running | done | failed, `kind`, `run_head`, `pid`, `error`), the
last head whose interdiff was checked (`checked_head`), and when the review's
notification went out (`announced_at`, `announced_state`). What a draft reviewed lives
in the PR's record, review/record.py.

The menu row and the notification arrive together, once the row has a draft state
(`row_state`): draft ready, draft outdated, draft failed, or, when no draft is ready
`fallback_minutes` after the request was seen, no draft. Until then the review is
hidden: no row, no count, no popup. A review is announced once; an update finishing
turns the row back to "draft ready" without another popup.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import settings  # noqa: E402
from review import interdiff  # noqa: E402
from review import record as rec_mod  # noqa: E402
from triage import priority  # noqa: E402

DRAFTS_STATE = settings.STATE_DIR / "drafts"
MANUAL_STATE = settings.REPO_DIR / "review-drafter" / "state"   # inflight-<repo>#<n>
# A cached triage older than this is refreshed in the background.
CACHE_MAX_AGE = 15 * 60
# A cached triage that lacks a PR requesting your review is refreshed once it's this old.
NEW_REQUEST_AGE = 5 * 60

READY, OUTDATED, FAILED, NO_DRAFT = "draft ready", "draft outdated", "draft failed", "no draft"

REQUESTED_QUERY = """
query($q: String!) {
  search(type: ISSUE, query: $q, first: 100) { nodes { ... on PullRequest {
    url number title headRefOid baseRefName repository { name } author { login } } } }
}
"""


def enabled(cfg: dict) -> bool:
    r = cfg.get("review") or {}
    return bool(r.get("auto_draft")) and bool(r.get("reviewer_agent"))


# --- the per-PR scheduling state ------------------------------------------------------


def job_path(key: str, state_dir: Path | None = None) -> Path:
    return (state_dir or DRAFTS_STATE) / f"{key}.json"


def load_job(key: str, state_dir: Path | None = None) -> dict | None:
    try:
        return json.loads(job_path(key, state_dir).read_text())
    except (OSError, ValueError):
        return None


def save_job(job: dict, state_dir: Path | None = None) -> None:
    path = job_path(job["key"], state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, indent=2) + "\n")
    os.replace(tmp, path)


def all_jobs(state_dir: Path | None = None) -> dict[str, dict]:
    folder = state_dir or DRAFTS_STATE
    out = {}
    for p in sorted(folder.glob("*.json")) if folder.exists() else []:
        try:
            job = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        out[job.get("key") or p.stem] = job
    return out


def _alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def running(job: dict | None) -> bool:
    return bool(job) and job.get("status") == "running" and _alive(job.get("pid"))


def manual_running(key: str, folder: Path | None = None) -> bool:
    """A review started from the menu (review-drafter/menubar.sh) is running."""
    path = (folder or MANUAL_STATE) / f"inflight-{key}"
    if not path.exists():
        return False
    try:
        return _alive(int(path.read_text().strip()))
    except (OSError, ValueError):
        return True


def pr_key(url: str) -> str:
    parts = url.rstrip("/").split("/")
    return f"{parts[-3]}#{parts[-1]}"


def _epoch(stamp) -> float | None:
    if stamp is None or stamp == "":
        return None
    if isinstance(stamp, (int, float)):
        return float(stamp)
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


# --- pure decisions ----------------------------------------------------------------------


def row_state(job: dict | None, rec: dict | None, head: str, since, now: float,
              fallback_minutes: float = 45) -> str | None:
    """The draft state a review's menu row shows, or None while it stays hidden: it
    hasn't been shown yet, no draft covers its head, nothing has failed, and the
    request is younger than fallback_minutes. `since` is when the review was
    requested, for a review the scheduler hasn't seen yet. Once shown, a review never
    hides again: new commits make its draft "draft outdated" until the update lands."""
    if rec and rec_mod.covers(rec, head):
        return READY
    if job and job.get("status") == "failed" and (not rec or job.get("run_head") == head):
        return FAILED
    if job and job.get("announced_at"):
        return OUTDATED if rec else NO_DRAFT
    base = _epoch((job or {}).get("seen_at")) or _epoch(since)
    if base is not None and now - base < fallback_minutes * 60:
        return None
    return OUTDATED if rec else NO_DRAFT


def next_step(job: dict | None, rec: dict | None, head: str, now: float,
              quiet_minutes: float = 10) -> str | None:
    """What to start for one review you owe: "full", "check" (compute the interdiff
    now, carrying the draft forward when it's empty), "update", or None."""
    if running(job) or not head:
        return None
    job = job or {}
    quiet = now - (_epoch((job.get("head") or {}).get("at")) or 0) >= quiet_minutes * 60
    failed_here = job.get("status") == "failed" and job.get("run_head") == head
    if rec is None:
        if job.get("status") in (None, "idle"):
            return "full"
        if job.get("status") == "failed" and not failed_here and quiet:
            return "full"
        return None
    if rec_mod.covers(rec, head) or failed_here:
        return None
    if job.get("checked_head") != head:
        return "check"
    return "update" if quiet else None


def tooltip(rec: dict | None, job: dict | None) -> str:
    """The row's hover text: what the draft cost, and why the last run failed."""
    bits = []
    if rec:
        hist = rec.get("history") or [{"kind": "full", "cost_usd": rec.get("cost_usd")}]
        total = sum(h.get("cost_usd") or 0 for h in hist)
        fulls = [h for h in hist if h.get("kind") == "full"]
        updates = [h for h in hist if h.get("kind") == "update"]
        parts = []
        if fulls:
            parts.append(f"{'full review' if len(fulls) == 1 else f'{len(fulls)} full reviews'} "
                         f"${sum(h.get('cost_usd') or 0 for h in fulls):.2f}")
        if updates:
            parts.append(f"{len(updates)} update{'s' if len(updates) != 1 else ''} "
                         f"${sum(h.get('cost_usd') or 0 for h in updates):.2f}")
        bits.append(f"Draft cost ${total:.2f}" + (f" ({', '.join(parts)})" if parts else ""))
    if job and job.get("status") == "failed" and job.get("error"):
        bits.append(f"Last run failed: {job['error'][:120]}")
    return " · ".join(bits)


def shown(job: dict | None, now: float, fallback_minutes: float = 45) -> bool:
    """Whether the menu may show a review that has a draft state: the scheduler has
    sent its notification, or it isn't handling it, or it has had longer than the
    fallback to, so the row and the notification arrive together."""
    if not job or job.get("announced_at"):
        return True
    seen = _epoch(job.get("seen_at"))
    return seen is None or now - seen >= fallback_minutes * 60


def states(sts: list[dict], jobs: dict[str, dict], recs: dict[str, dict | None], now: float,
           fallback_minutes: float = 45, menu: bool = False) -> dict[str, dict]:
    """PR url -> {"state", "tooltip"} for each review candidate (`sts`, triage
    records), for triage/priority.py classify's `drafts`. `jobs` and `recs` are keyed
    by <repo>#<n>; the head is the scheduler's latest, else the triage's. With `menu`,
    a review whose notification hasn't gone out yet has no state (`shown`)."""
    out = {}
    for st in sts:
        url = (st.get("pr_url") or "").rstrip("/")
        if not url:
            continue
        key = pr_key(url)
        job, rec = jobs.get(key), recs.get(key)
        head = ((job or {}).get("head") or {}).get("sha") or st.get("head_sha") or ""
        state = row_state(job, rec, head, st.get("waiting_since") or st.get("created_at"),
                          now, fallback_minutes)
        if menu and not shown(job, now, fallback_minutes):
            state = None
        out[url] = {"state": state, "tooltip": tooltip(rec, job)}
    return out


def candidates(data: dict) -> list[dict]:
    """The triage records a review you owe can come from, in ranked order."""
    reviews = data.get("reviews") or {}
    return list(reviews.get("needs_you") or []) + list(reviews.get("well_approved") or [])


def records_for(sts: list[dict], index_dir: Path | None = None) -> dict[str, dict | None]:
    out = {}
    for st in sts:
        url = st.get("pr_url") or ""
        if url:
            repo, num = pr_key(url).split("#")
            out[pr_key(url)] = rec_mod.load(repo, int(num), index_dir)
    return out


def classify_owed(data: dict, cfg: dict, followup_jobs: list[dict], nick: dict[str, str],
                  now: datetime, drafts: dict[str, dict] | None) -> list[dict]:
    """The OWED entries the menu shows for `data` (the cached triage), in ranked order,
    classified exactly as the menu classifies them."""
    entries = priority.classify(
        [], data.get("reviews") or {}, followup_jobs, nick=nick, now=now,
        required=cfg["watcher"]["required_approvals"],
        stale_after_days=cfg["priority"]["stale_after_days"],
        max_engaged=cfg["priority"]["max_engaged_for_stale"],
        pod=frozenset(cfg["triage"].get("pod") or ()), drafts=drafts)
    return [e for e in entries if e["section"] == priority.OWED]


def owes_draft(st: dict, cfg: dict) -> bool:
    """A review you owe gets a draft unless it already has the required approvals
    and its author isn't in your pod."""
    pod = set(cfg["triage"].get("pod") or ())
    return (len(st.get("approvals") or []) < cfg["watcher"]["required_approvals"]
            or st.get("author") in pod)


# --- GitHub and the cached triage ------------------------------------------------------


def requested_heads(cfg: dict) -> dict[str, dict]:
    """PR url -> {head, base, title, author} for every open PR requesting your review,
    from one GraphQL search."""
    from watcher import gh
    q = (f"is:pr is:open org:{cfg['org']} review-requested:{cfg['me']} "
         f"-author:{cfg['me']} draft:false")
    data, _ = gh.graphql(REQUESTED_QUERY, {"q": q}, gh.token())
    out = {}
    for n in (data.get("search") or {}).get("nodes") or []:
        if n and n.get("url"):
            out[n["url"].rstrip("/")] = {"head": (n.get("headRefOid") or "").lower(),
                                         "base": n.get("baseRefName") or "main",
                                         "title": n.get("title") or "",
                                         "author": (n.get("author") or {}).get("login", "")}
    return out


def _menu():
    import swiftbar_plugin
    return swiftbar_plugin


def cached_triage() -> tuple[dict, float | None]:
    menu = _menu()
    cache = menu.load_cache() or {}
    return cache, menu.cache_age(cache)


def refresh_triage() -> bool:
    """Start the menu's background fetch unless one is running. True when started."""
    menu = _menu()
    if menu.is_fetching():
        return False
    menu.set_marker()
    menu.kick_background_fetch()
    return True


def pr_info(st: dict, live: dict | None) -> dict:
    url = st["pr_url"].rstrip("/")
    repo, num = pr_key(url).split("#")
    return {"url": url, "repo": repo, "number": int(num),
            "owner": (st.get("owner_repo") or f"avetta/{repo}").split("/")[0],
            "title": (live or {}).get("title") or st.get("title") or "",
            "author": st.get("author") or (live or {}).get("author") or "",
            "base": (live or {}).get("base") or st.get("base") or "main",
            "key": priority.ticket(st.get("title") or "", url)}


# --- starting and running ---------------------------------------------------------------


def start(job: dict, kind: str, head: str, cfg: dict, now: float,
          state_dir: Path | None = None, popen=None) -> None:
    """Start `python -m watcher draft-run <repo>#<n>` detached, recorded in the job."""
    popen = popen or subprocess.Popen
    job.update(status="running", kind=kind, run_head=head, started_at=int(now), error="",
               finished_at=None)
    save_job(job, state_dir)
    proc = popen([sys.executable, "-m", "watcher", "draft-run", job["key"]],
                 cwd=settings.REPO_DIR, start_new_session=True, env=_env(),
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    job["pid"] = proc.pid
    save_job(job, state_dir)


def _env() -> dict:
    env = dict(os.environ)
    extra = [str(Path("~/.local/bin").expanduser()), "/opt/homebrew/bin", "/usr/local/bin"]
    env["PATH"] = ":".join(extra + [env.get("PATH", "/usr/bin:/bin")])
    return env


def clone_of(cfg: dict, repo: str) -> Path:
    return settings.path_of(cfg["repos_root"]) / repo


def carry_forward(rec: dict, new_head: str, delta: dict, index_dir: Path | None = None) -> dict:
    """The draft covers `new_head` too: nothing in the PR's own diff changed."""
    out = {**rec, "head": new_head,
           "pr_diff": {"head": new_head, "merge_base": delta["new_merge_base"],
                       "fingerprint": delta["new_fingerprint"]["sha"],
                       "files": delta["new_fingerprint"]["files"]},
           "history": (rec.get("history") or []) + [{
               "kind": "carried", "from": rec.get("head"), "to": new_head,
               "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "cost_usd": 0,
               "summary": "nothing in the PR's own diff changed"}]}
    rec_mod.save(out, index_dir)
    return out


def check(job: dict, rec: dict, pr: dict, head: str, cfg: dict,
          index_dir: Path | None = None) -> tuple[str, dict | None]:
    """Compute the interdiff from the draft's head to `head`. Returns ("carried",
    record) when it's empty, ("changed", None) otherwise."""
    clone = clone_of(cfg, pr["repo"])
    tip = interdiff.fetch(clone, pr["number"], pr["base"], [rec["head"], head])
    delta = interdiff.compute(clone, tip, rec["head"], head)
    if delta["empty"]:
        return "carried", carry_forward(rec, head, delta, index_dir)
    job["checked_delta"] = {"lines": delta["lines"], "files": len(delta["files"]),
                            "pr_lines": delta["pr_lines"], "new_files": delta["new_files"]}
    return "changed", None


def record_pr_diff(rec: dict, pr: dict, cfg: dict, index_dir: Path | None = None) -> dict:
    """Store the PR's own diff fingerprint at the record's head (review/record.py)."""
    clone = clone_of(cfg, pr["repo"])
    tip = interdiff.fetch(clone, pr["number"], pr["base"], [rec["head"]])
    d = interdiff.pr_diff(clone, tip, rec["head"])
    rec = {**rec, "pr_diff": {"head": rec["head"], "merge_base": d["merge_base"],
                              "fingerprint": d["fingerprint"]["sha"],
                              "files": d["fingerprint"]["files"]}}
    rec_mod.save(rec, index_dir)
    return rec


def run_job(key: str, cfg: dict, state_dir: Path | None = None,
            index_dir: Path | None = None) -> int:
    """The detached run behind `python -m watcher draft-run <repo>#<n>`: a full draft,
    or an update (which may turn out empty, or large enough for a full review)."""
    from review import review_pr, update_review
    job = load_job(key, state_dir)
    if not job:
        print(f"no draft job {key}", file=sys.stderr)
        return 2
    pr, head = job["pr"], job["run_head"]
    started = time.time()
    outcome = ""
    try:
        rec = rec_mod.load(pr["repo"], pr["number"], index_dir)
        kind = job.get("kind") if rec else "full"
        if kind == "update":
            clone = clone_of(cfg, pr["repo"])
            tip = interdiff.fetch(clone, pr["number"], pr["base"], [rec["head"], head])
            delta = interdiff.compute(clone, tip, rec["head"], head)
            why_full = interdiff.needs_full_review(delta, float(cfg["review"]["update_max_share"]))
            job["delta"] = {"lines": delta["lines"], "files": sorted(delta["files"]),
                            "pr_lines": delta["pr_lines"], "new_files": delta["new_files"],
                            "commits": delta["commits"], "method": delta["method"]}
            if delta["empty"]:
                carry_forward(rec, head, delta, index_dir)
                outcome = "carried forward: the interdiff is empty"
            elif why_full:
                job["full_because"] = why_full
                kind = "full"
            else:
                import names
                threads = update_review.fetch_threads(pr.get("owner") or cfg["org"], pr["repo"],
                                                      pr["number"], set(cfg["watcher"]["bots"]))
                out = update_review.run(rec, pr, delta, rec["head"], head, clone, cfg, threads,
                                        names.book(fetch=True), index_dir=index_dir)
                record_pr_diff(out, pr, cfg, index_dir)
                outcome = out["history"][-1]["summary"]
        if kind == "full" and not outcome:
            records = review_pr.review(pr["url"], None, None, False, cfg)
            for r in records:
                if r["repo"] == pr["repo"] and r["number"] == pr["number"]:
                    try:
                        record_pr_diff(rec_mod.load(r["repo"], r["number"], index_dir) or r, pr,
                                       cfg, index_dir)
                    except interdiff.InterdiffError:
                        pass
            outcome = "full draft written" + (f" ({job['full_because']})" if job.get("full_because") else "")
    except Exception as exc:  # noqa: BLE001 — every failure is recorded on the job
        job = load_job(key, state_dir) or job
        job.update(status="failed", error=str(exc)[:300], finished_at=int(time.time()),
                   duration_s=int(time.time() - started))
        save_job(job, state_dir)
        print(f"draft {key} failed: {exc}", file=sys.stderr)
        return 1
    job = {**(load_job(key, state_dir) or job), **{k: job[k] for k in ("delta", "full_because")
                                                   if k in job}}
    job.update(status="done", outcome=outcome, finished_at=int(time.time()),
               duration_s=int(time.time() - started), checked_head=head, error="")
    save_job(job, state_dir)
    return 0


# --- the tick ----------------------------------------------------------------------------


def tick(cfg: dict, dry_run: bool = False, *, now: float | None = None, limit: int | None = None,
         only: str | None = None, deliver=None, log=print) -> list[dict]:
    """One scheduling pass. Starts drafts and updates, carries drafts forward, and
    returns the notifications for reviews whose row appears now ({pr, color, reason,
    state, href, title}); `deliver(notes)` sends them (default: notify_reviews).
    `limit` caps how many runs start this pass and `only` restricts it to one PR url;
    both exist for trying it out by hand."""
    from watcher import followup
    import names
    now = time.time() if now is None else now
    rcfg = cfg["review"]
    cache, age = cached_triage()
    live: dict[str, dict] = {}
    try:
        live = requested_heads(cfg)
    except Exception as exc:  # noqa: BLE001 — the cached heads still work
        log(f"drafts: couldn't read requested PRs: {str(exc)[:120]}")
    known = {(st.get("pr_url") or "").rstrip("/") for b in (cache.get("reviews") or {}).values()
             for st in b}
    # A request the cached triage hasn't seen refreshes it, at most every NEW_REQUEST_AGE,
    # since a PR the triage leaves out (closed meanwhile) stays unknown.
    unknown = bool(set(live) - known) and (age is None or age > NEW_REQUEST_AGE)
    if not cache.get("reviews") or age is None or age > CACHE_MAX_AGE or unknown:
        if not dry_run and refresh_triage():
            log("drafts: refreshing the cached triage")
    if not cache.get("reviews"):
        return []

    nick = names.book(fetch=False)
    jobs_f = followup.all_jobs()
    now_dt = datetime.fromtimestamp(now, timezone.utc)
    sts = {st["pr_url"].rstrip("/"): st for st in candidates(cache) if st.get("pr_url")}
    owed = classify_owed(cache, cfg, jobs_f, nick, now_dt, None)
    jobs = all_jobs()
    started = 0
    for e in owed:
        url = e["url"].rstrip("/")
        st = sts.get(url)
        if not st or (only and url != only.rstrip("/")) or not owes_draft(st, cfg):
            continue
        key = pr_key(url)
        info = live.get(url)
        head = (info or {}).get("head") or st.get("head_sha") or ""
        job = jobs.get(key) or {"key": key, "seen_at": int(now), "status": "idle"}
        job["pr"] = pr_info(st, info)
        if (job.get("head") or {}).get("sha") != head:
            job["head"] = {"sha": head, "at": int(now)}
        if job.get("status") == "running" and not _alive(job.get("pid")):
            job.update(status="failed", error="the draft process died")
        jobs[key] = job
        rec = rec_mod.load(job["pr"]["repo"], job["pr"]["number"])
        if manual_running(key):
            step = None
        else:
            step = next_step(job, rec, head, now, rcfg["quiet_minutes"])
        if step == "check":
            if dry_run:
                log(f"drafts: would check the interdiff of {key} {rec['head'][:7]}..{head[:7]}")
            else:
                try:
                    outcome, _ = check(job, rec, job["pr"], head, cfg)
                    job["checked_head"] = head
                    log(f"drafts: {key} {rec['head'][:7]}..{head[:7]}: {outcome}")
                except (interdiff.InterdiffError, subprocess.SubprocessError, OSError) as exc:
                    job.update(checked_head=head, check_error=str(exc)[:200])
                    log(f"drafts: {key} interdiff failed: {str(exc)[:120]}")
                save_job(job)
            step = None
        what = "a full draft" if step == "full" else "an update"
        if step in ("full", "update"):
            if limit is not None and started >= limit:
                step = None
            elif dry_run:
                log(f"drafts: would start {what} of {key} at {head[:7]}")
                started += 1
                step = None
        if step in ("full", "update"):
            start(job, step, head, cfg, now)
            started += 1
            log(f"drafts: started {what} of {key} at {head[:7]}")
        elif not dry_run:
            save_job(job)

    # Which hidden reviews now have a row, and so a notification.
    recs = records_for(list(sts.values()))
    drafts = states(list(sts.values()), jobs, recs, now, rcfg["fallback_minutes"])
    shown = classify_owed(cache, cfg, jobs_f, nick, now_dt, drafts)
    notes = []
    for e in shown:
        url = e["url"].rstrip("/")
        key = pr_key(url)
        job = jobs.get(key)
        if not job or job.get("announced_at") or (only and url != only.rstrip("/")):
            continue
        state = drafts.get(url, {}).get("state")
        rec = recs.get(key)
        draft = (rec or {}).get("story") or (rec or {}).get("draft")
        notes.append({"pr": job["pr"], "color": e["color"], "reason": e["reason"],
                      "state": state, "draft": draft if state in (READY, OUTDATED) else None,
                      "url": url, "key": key})
        if not dry_run:
            job.update(announced_at=int(now), announced_state=state)
            save_job(job)
    if notes:
        (deliver or (lambda ns: notify_reviews(ns, cfg, dry_run, log)))(notes)
    return notes


def notify_reviews(notes: list[dict], cfg: dict, dry_run: bool = False, log=print) -> None:
    """One notification per review: red and yellow as alerts with their sound, green as
    a banner. Clicking opens the draft as a page (review/view_draft.py), or the PR when
    there's no draft."""
    import names
    import notify
    nick = names.book(fetch=True)
    for n in sorted(notes, key=lambda n: -priority.rank(n["color"])):
        pr = n["pr"]
        title = f"{priority.dot(n['color'])} {priority.who(pr['author'], nick)} asked you to review {pr['key']}"
        sub = priority.TICKET_RE.sub("", pr.get("title") or "").strip(" []:·-") or pr["key"]
        href = pr["url"]
        if n.get("draft"):
            try:
                from review import view_draft
                href = view_draft.render(Path(n["draft"])).as_uri()
            except Exception:  # noqa: BLE001 — the notification still goes out
                href = Path(n["draft"]).as_uri()
        if dry_run:
            log(f"[{n['color']}] {title}\n    {sub}\n    {n['reason']}\n    {href}")
            continue
        group = f"pr-{pr['repo']}-{pr['number']}"
        if n["color"] in (priority.RED, priority.YELLOW):
            notify.alert(title, sub, n["reason"], href=href, plugin=cfg["notify"]["swiftbar_plugin"],
                         group=group, **notify.sound_options(cfg, n["color"]))
        else:
            notify.banner(title, sub, n["reason"], href=href, group=group)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log(f"{stamp} sent review: {title} | {sub} | {n['reason']} → {href}")
