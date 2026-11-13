"""Move a merged PR's Jira ticket to the status that follows a merge.

On this team a PR merges into main after QA has passed its ticket, and the ticket then
moves to its workflow's integration status, where QA verifies the release build. Each
watcher tick runs `run`, which finds your PRs merged into BASE since the last look (one
GraphQL search), wherever they were merged from, and, for each one not handled yet:

  1. Takes the ticket key from the branch name (`CONN-1234`), else from the title. A PR
     with no key is skipped. Only projects in `[triage] team_tickets` count.
  2. Reads the ticket with acli. An old WS key that Jira moved to CONN resolves to its
     CONN key, so the CONN rule applies to it.
  3. Applies the RULES row for the ticket's project and issue type:
       - already in `target` or a later status (`later`): nothing to do;
       - any status but the row's `source`, or no row for its type: moves nothing and
         raises an alert, since the ticket is somewhere a merge doesn't expect (QA
         hasn't passed it, say) and only a person can say where it belongs;
       - in `source`, while another open PR in the org (anyone's) still carries the
         key in its title or as its branch: holds it there, and the last of those PRs
         to merge moves it;
       - in `source` otherwise: transitions it to `target` (acli, by status name) and
         reads it back to confirm.
     A ticket never moves through intermediate statuses, and never from any status but
     its row's `source`.

Outcomes: a move or a hold is a ⚪ FYI row (watcher/fyi.py) with one quiet banner that
links to the ticket; anything to fix by hand is a red alert plus an FYI row. Every
outcome is logged to the watcher log.

State is state/watcher/jira_merge.json: `since` (the GitHub server time of the last
search; each search looks back OVERLAP before it), `handled` (PR url -> outcome; pruned
after KEEP_DAYS), and `retries` (PR url -> failed Jira reads; a PR is retried on later
ticks and alerted after MAX_RETRIES). The first run records the time and moves nothing,
so the watcher never acts on merges from before it first ran this.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import settings  # noqa: E402
from watcher import fyi, gh  # noqa: E402

PATH = settings.STATE_DIR / "watcher" / "jira_merge.json"
# Only merges into this branch move a ticket; a stacked PR merging into its parent
# branch doesn't.
BASE = "main"
OVERLAP = timedelta(hours=1)
KEEP_DAYS = 30
MAX_RETRIES = 5
TIMEOUT = 30

MOVED, ALREADY, HELD, UNEXPECTED, NO_RULE, FAILED, NO_KEY = (
    "moved", "already", "held", "unexpected", "no_rule", "failed", "no_key")
# Outcomes that need a person to move the ticket.
ALERTS = {UNEXPECTED, NO_RULE, FAILED}


@dataclass(frozen=True)
class Rule:
    project: str
    types: tuple[str, ...]
    source: str
    target: str
    later: tuple[str, ...]


_INTEGRATED_CONN = ("Integration", "Ready for Release", "Released")
_INTEGRATED_WS = ("Ready for Integration", "Ready for Release", "Released")

# One row per workflow. `source` is the status QA leaves a ticket in once it may merge;
# `target` is where the merge takes it (the transition Jira offers between them is named
# in the comment); `later` are the statuses that mean the move already happened.
RULES: tuple[Rule, ...] = (
    # "Start Integration"
    Rule("CONN", ("Story", "Bug"), "Team Tests Passed", "Integration", _INTEGRATED_CONN),
    # "Done": these workflows have no QA statuses, so review is the last step before it.
    Rule("CONN", ("Sub-task", "Spike", "Non-Code Task"), "In Review", "Done", ("Done",)),
    # "Merge"
    Rule("WS", ("Bug", "Task", "Code Sub-task"), "Ready to merge", "Ready for Integration",
         _INTEGRATED_WS),
    # "Stage for Integration": this workflow has no Ready to merge status.
    Rule("WS", ("User Story", "QA Sub-task"), "Team Testing In Progress",
         "Ready for Integration", _INTEGRATED_WS),
    Rule("WS", ("Spike", "Non-Code Task"), "In Review", "Done", ("Done",)),
)


def rule_for(project: str, issue_type: str) -> Rule | None:
    for r in RULES:
        if r.project == project and issue_type in r.types:
            return r
    return None


# --- the ticket key --------------------------------------------------------------------


def _key_re(projects) -> re.Pattern:
    alts = "|".join(re.escape(p) for p in projects)
    return re.compile(rf"\b(?:{alts})-\d+\b", re.IGNORECASE)


def key_of(branch: str, title: str, projects) -> str | None:
    """The PR's ticket key, upper-cased: the branch when it's exactly a key, else the
    first key in the title (titles carry keys as "CONN-1: …", "[CONN-1] …", "[ CONN-1 ]")."""
    rx = _key_re(projects)
    m = rx.fullmatch((branch or "").strip())
    if m:
        return m.group(0).upper()
    m = rx.search(title or "")
    return m.group(0).upper() if m else None


def carries(pr: dict, keys: set[str], projects) -> bool:
    """Whether a PR's branch or title names one of `keys`."""
    rx = _key_re(projects)
    found = {k.upper() for k in rx.findall(pr.get("title") or "")}
    branch = (pr.get("headRefName") or "").strip().upper()
    return bool(keys & (found | {branch}))


# --- deciding --------------------------------------------------------------------------


def decide(ticket: dict, siblings: list[str]) -> tuple[str, Rule | None]:
    """The outcome for a merged PR's ticket ({key, project, type, status}), given the
    open PRs still carrying its key (`siblings`, as "repo#n"). Pure."""
    rule = rule_for(ticket["project"], ticket["type"])
    if rule is None:
        return NO_RULE, None
    status = ticket["status"]
    if status == rule.target or status in rule.later:
        return ALREADY, rule
    if status != rule.source:
        return UNEXPECTED, rule
    return (HELD if siblings else MOVED), rule


# --- Jira (acli) -----------------------------------------------------------------------


class JiraError(Exception):
    pass


def _acli(cli: str, args: list[str]) -> str:
    try:
        r = subprocess.run([cli, "jira", "workitem", *args], capture_output=True, text=True,
                           timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise JiraError(f"{cli} didn't run: {exc}") from exc
    if r.returncode != 0:
        raise JiraError((r.stderr or r.stdout or f"{cli} exited {r.returncode}").strip()[:300])
    return r.stdout


def view(key: str, cli: str = "acli") -> dict:
    """{key, project, type, status, summary} of a ticket. A moved ticket comes back under
    its current key."""
    out = _acli(cli, ["view", key, "--fields", "status,issuetype,project,summary", "--json"])
    try:
        data = json.loads(out)
    except ValueError as exc:
        raise JiraError(f"unreadable acli output for {key}") from exc
    f = data.get("fields") or {}
    return {"key": data.get("key") or key,
            "project": (f.get("project") or {}).get("key") or "",
            "type": (f.get("issuetype") or {}).get("name") or "",
            "status": (f.get("status") or {}).get("name") or "",
            "summary": f.get("summary") or ""}


def transition(key: str, status: str, cli: str = "acli") -> None:
    """Move a ticket to `status` and confirm it got there."""
    _acli(cli, ["transition", "--key", key, "--status", status, "--yes"])
    now = view(key, cli)["status"]
    if now != status:
        raise JiraError(f"{key} is in {now} after the transition to {status}")


# --- GitHub ----------------------------------------------------------------------------

_PR_FIELDS = "number title url mergedAt headRefName baseRefName repository { name }"
MERGED_QUERY = """
query($q: String!) { search(type: ISSUE, query: $q, first: 50) {
  nodes { ... on PullRequest { %s } } } }
""" % _PR_FIELDS
OPEN_QUERY = """
query($q: String!) { search(type: ISSUE, query: $q, first: 30) {
  nodes { ... on PullRequest { number title headRefName repository { name } } } } }
"""


def merged_since(me: str, org: str, base: str, since: str, tok: str) -> tuple[list[dict], str | None]:
    """Your PRs merged into `base` at or after `since` (ISO), oldest first, and the
    server's Date header."""
    data, date = gh.graphql(MERGED_QUERY, {
        "q": f"is:pr is:merged org:{org} author:{me} base:{base} merged:>={since}"}, tok)
    prs = [n for n in (data.get("search") or {}).get("nodes") or [] if n and n.get("url")]
    return sorted(prs, key=lambda p: p.get("mergedAt") or ""), date


def open_siblings(keys: set[str], org: str, projects, tok: str) -> list[str]:
    """Open PRs in the org, by anyone, whose title or branch names one of `keys`, as
    "repo#n"."""
    out: list[str] = []
    for key in sorted(keys):
        data, _ = gh.graphql(OPEN_QUERY, {"q": f'is:pr is:open org:{org} "{key}"'}, tok)
        for n in (data.get("search") or {}).get("nodes") or []:
            if n and carries(n, keys, projects):
                ref = f"{n['repository']['name']}#{n['number']}"
                if ref not in out:
                    out.append(ref)
    return out


# --- state -----------------------------------------------------------------------------


def load(path: Path | None = None) -> dict:
    try:
        data = json.loads((path or PATH).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data: dict, path: Path | None = None) -> None:
    path = path or PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    tmp.replace(path)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _server_time(date_header: str | None) -> datetime:
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(date_header) if date_header else datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


# --- one merged PR ---------------------------------------------------------------------


def handle(pr: dict, cfg: dict, tok: str, dry_run: bool = False,
           jira_view: Callable[[str], dict] | None = None,
           jira_move: Callable[[str, str], None] | None = None,
           siblings_of: Callable[[set[str]], list[str]] | None = None) -> dict:
    """The outcome for one merged PR: {outcome, ref, url, key?, ticket?, rule?, siblings?,
    error?}. Moves the ticket unless `dry_run`. Raises JiraError when the ticket can't be
    read, so the caller can retry it later."""
    cli = cfg["jira"].get("cli") or "acli"
    projects = cfg["triage"].get("team_tickets") or ["WS", "CONN"]
    jira_view = jira_view or (lambda k: view(k, cli))
    jira_move = jira_move or (lambda k, s: transition(k, s, cli))
    siblings_of = siblings_of or (lambda keys: open_siblings(keys, cfg["org"], projects, tok))
    ref = f"{pr['repository']['name']}#{pr['number']}"
    out = {"ref": ref, "url": pr["url"], "title": pr.get("title") or "",
           "merged_at": pr.get("mergedAt")}
    key = key_of(pr.get("headRefName") or "", pr.get("title") or "", projects)
    if not key:
        return {**out, "outcome": NO_KEY}
    ticket = jira_view(key)
    out.update(key=key, ticket=ticket)
    # Search can list a just-merged PR as open for a minute or two; it never holds itself.
    sibs = [s for s in siblings_of({key, ticket["key"]}) if s != ref]
    outcome, rule = decide(ticket, sibs)
    out.update(outcome=outcome, siblings=sibs,
               rule={"source": rule.source, "target": rule.target} if rule else None)
    if outcome == MOVED and not dry_run:
        try:
            jira_move(ticket["key"], rule.target)
        except JiraError as exc:
            out.update(outcome=FAILED, error=str(exc))
    return out


# --- telling you -----------------------------------------------------------------------


def _browse(cfg: dict, key: str) -> str | None:
    base = cfg["jira"].get("browse_url") or ""
    return f"{base}{key}" if base else None


def message(o: dict) -> tuple[str, str] | None:
    """(headline, row) for an outcome worth telling you about; None for the rest."""
    t = o.get("ticket") or {}
    key, status = t.get("key") or o.get("key"), t.get("status")
    rule = o.get("rule") or {}
    ref = o["ref"]
    kind = o["outcome"]
    if kind == MOVED:
        return (f"{ref} merged · moved {key} to {rule['target']}",
                f"Merged · {key} {rule['source']} → {rule['target']}")
    if kind == HELD:
        others = ", ".join(o["siblings"])
        return (f"{ref} merged · {key} stays in {status} until {others} merges",
                f"Merged · {key} stays in {status}: {others} still open")
    if kind == UNEXPECTED:
        return (f"⚠️ {ref} merged, but {key} is in {status}",
                f"Merged · move {key} by hand: it's in {status}, the merge moves it only "
                f"from {rule['source']} (to {rule['target']})")
    if kind == NO_RULE:
        return (f"⚠️ {ref} merged · no merge rule for {key} ({t.get('project')} {t.get('type')})",
                f"Merged · move {key} by hand: no merge rule for a {t.get('project')} "
                f"{t.get('type')} (it's in {status})")
    if kind == FAILED:
        what = f"to {rule['target']}" if rule else "on"
        return (f"⚠️ {ref} merged · couldn't move {key} {what}",
                f"Merged · move {key} by hand: {o.get('error') or 'Jira error'}")
    return None


def tell(outcomes: list[dict], cfg: dict, alert=None) -> None:
    """A ⚪ FYI row per move or hold (one quiet banner each), and a red alert plus an FYI
    row per outcome you have to fix by hand."""
    if alert is None:
        import notify

        def alert(title, body, href):
            notify.alert(title, "", body, href=href, plugin=cfg["notify"]["swiftbar_plugin"],
                         **notify.sound_options(cfg))
    quiet, loud = [], []
    for o in outcomes:
        msg = message(o)
        if not msg:
            continue
        headline, row = msg
        key = (o.get("ticket") or {}).get("key") or o.get("key") or ""
        parts = o["url"].rstrip("/").split("/")
        item = {"id": f"jira-{o['outcome']}-{parts[-3]}-{parts[-1]}", "kind": f"jira_{o['outcome']}",
                "pr": {"url": o["url"].rstrip("/"), "repo": parts[-3], "number": int(parts[-1]),
                       "title": o.get("title") or ""},
                "headline": headline, "row": row, "subtitle": (o.get("ticket") or {}).get("summary") or "",
                "detail": row, "href": _browse(cfg, key) or o["url"]}
        (loud if o["outcome"] in ALERTS else quiet).append(item)
    fyi.add(quiet, cfg)
    fyi.add(loud, cfg, send=False)
    for it in loud:
        alert(it["headline"], it["row"], it["href"])


# --- the tick's pass -------------------------------------------------------------------


def run(cfg: dict, tok: str, dry_run: bool = False, since: str | None = None,
        path: Path | None = None, now: float | None = None, log=print, **io) -> list[dict]:
    """Handle every PR of yours merged since the last look. `since` (an ISO time) replays
    a window instead, and then nothing is saved. Returns the outcomes handled."""
    st = load(path)
    replay = since is not None
    if not replay and not st.get("since"):
        first = _iso(datetime.now(timezone.utc))
        if not dry_run:
            save({"since": first, "handled": {}, "retries": {}}, path)
        log(f"jira-merge: first run, recording {first}; past merges are left alone")
        return []
    start = since or _iso(datetime.fromisoformat(st["since"].replace("Z", "+00:00")) - OVERLAP)
    prs, date = merged_since(cfg["me"], cfg["org"], BASE, start, tok)
    handled = {} if replay else dict(st.get("handled") or {})
    retries = {} if replay else dict(st.get("retries") or {})
    now = time.time() if now is None else now
    done: list[dict] = []
    for pr in prs:
        if pr["url"] in handled:
            continue
        try:
            o = handle(pr, cfg, tok, dry_run=dry_run, **io)
        except (JiraError, gh.GhError) as exc:
            n = retries.get(pr["url"], 0) + 1
            if n < MAX_RETRIES:
                retries[pr["url"]] = n
                log(f"jira-merge: {pr['url']}: {exc} (try {n} of {MAX_RETRIES}, again next tick)")
                continue
            o = {"ref": f"{pr['repository']['name']}#{pr['number']}", "url": pr["url"],
                 "title": pr.get("title") or "", "outcome": FAILED, "error": str(exc)[:300],
                 "key": key_of(pr.get("headRefName") or "", pr.get("title") or "",
                               cfg["triage"].get("team_tickets") or ["WS", "CONN"])}
        retries.pop(pr["url"], None)
        handled[pr["url"]] = {"at": int(now), "outcome": o["outcome"], "key": o.get("key"),
                              "ticket": (o.get("ticket") or {}).get("key")}
        done.append(o)
        msg = message(o)
        t = o.get("ticket") or {}
        seen = (f"{o['key']}{' → ' + t['key'] if t.get('key') not in (None, o['key']) else ''}, "
                f"{t.get('project')} {t.get('type')} in {t.get('status')}") if t else (o.get("key") or "no key")
        log(f"jira-merge: {o['ref']} ({seen}): {o['outcome']}"
            + (f" · {msg[1]}" if msg else "")
            + (" [dry run]" if dry_run else ""))
    if replay or dry_run:
        return done
    cutoff = now - KEEP_DAYS * 86400
    save({"since": _iso(_server_time(date)),
          "handled": {u: h for u, h in handled.items() if (h.get("at") or 0) >= cutoff},
          "retries": retries}, path)
    tell(done, cfg)
    return done
