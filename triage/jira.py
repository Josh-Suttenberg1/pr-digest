"""Which Jira ticket each PR's ticket groups under, so the menu can show a ticket's PRs
together.

A sub-task groups under its parent; any other ticket (a story, a bug, an epic's child)
groups under itself, so an epic never collects its children. `groups(keys, cfg)`
returns {ticket key: {"key": group key, "summary": that ticket's summary}}. It looks
each key up with the configured Jira CLI (`[jira] cli`, Atlassian's acli:
`acli jira workitem view <key> --fields issuetype,parent,summary --json`) and caches
the answer in state/jira.json for `[jira] cache_days`. A key the CLI can't read is
left out and isn't cached, so the next fetch asks again; callers group a key that's
missing under itself.
"""

from __future__ import annotations

import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CACHE = Path(__file__).resolve().parent.parent / "state" / "jira.json"
WORKERS = 6
TIMEOUT = 20


def group_of(view: dict) -> dict:
    """{key, summary} of the ticket a `workitem view` result groups under. Pure."""
    fields = view.get("fields") or {}
    parent = fields.get("parent") or {}
    if (fields.get("issuetype") or {}).get("subtask") and parent.get("key"):
        return {"key": parent["key"],
                "summary": (parent.get("fields") or {}).get("summary") or ""}
    return {"key": view.get("key") or "", "summary": fields.get("summary") or ""}


def _view(cli: str, key: str) -> dict | None:
    try:
        r = subprocess.run([cli, "jira", "workitem", "view", key, "--fields",
                            "issuetype,parent,summary", "--json"],
                           capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def groups(keys, cfg: dict, *, path: Path = CACHE, now: float | None = None,
           view=_view) -> dict[str, dict]:
    """The group of every key the CLI could read (see the module docstring); {} when
    `[jira] cli` is empty."""
    jira = cfg.get("jira") or {}
    cli = jira.get("cli") or ""
    if not cli:
        return {}
    now = time.time() if now is None else now
    max_age = float(jira.get("cache_days", 7)) * 86400
    cache = _load(path)
    fresh = {k: v for k, v in cache.items()
             if isinstance(v, dict) and now - float(v.get("at") or 0) < max_age}
    todo = sorted({k for k in keys if k} - set(fresh))
    if todo:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            views = list(pool.map(lambda k: view(cli, k), todo))
        for key, v in zip(todo, views):
            if v:
                fresh[key] = {**group_of(v), "at": now}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(fresh, indent=2, sort_keys=True) + "\n")
        except OSError:
            pass
    return {k: {"key": v["key"], "summary": v.get("summary") or ""}
            for k, v in fresh.items() if k in set(keys) and v.get("key")}
