"""FYI: things to know about a PR that ask nothing of you. ⚪ rows that clear once seen.

Four kinds, each found by the watcher (watcher/__main__.py `find_fyis`):

  approved   someone approved your PR with no comment: no inline comments and no
             body, or a body that only praises (`praise_only`).
  ready      one of your PRs became Ready (triage/priority.py group READY: the
             required approvals and no change request standing). It replaces the
             approval FYI that made it Ready, so one push of news makes one row.
  not_owed   a review you owed is no longer owed without your doing it: the PR merged
             or closed, or you were removed as a reviewer (popups.review_outcome).

Each item is stored in state/watcher/fyi.json under its id, sends one slide-away banner
with no sound (watcher/popups.py, kind fyi), and shows as a ⚪ row in the menu's FYI
section until it's been seen:

  - The first render that comes from opening the menu
    (SWIFTBAR_PLUGIN_REFRESH_REASON=MenuOpen) marks every item it shows as seen
    (`seen_at`) and withdraws its banner from Notification Center.
  - Any render SEEN_FOR seconds or more after that drops the item, so it's normally gone
    the next time the menu opens.

FYIs never count in the menu-bar title and never pop up as alerts. The menu and the
watcher both write the file, so every read-modify-write holds an flock on fyi.lock.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import settings  # noqa: E402

PATH = settings.STATE_DIR / "watcher" / "fyi.json"
SEEN_FOR = 10
APPROVED, READY, NOT_OWED = "approved", "ready", "not_owed"
# What a menu render reports when it comes from opening the menu (SwiftBar 2.1.1+).
MENU_OPEN = "MenuOpen"


def load(path: Path | None = None) -> dict[str, dict]:
    try:
        data = json.loads((path or PATH).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def item_id(kind: str, pr: dict, who: str = "") -> str:
    """One id per kind and PR (and reviewer, for an approval), so the same news found
    twice makes one row."""
    base = f"{kind}-{pr['repo']}-{pr['number']}"
    return f"{base}-{who}" if who else base


def add(items: list[dict], cfg: dict, path: Path | None = None, run=subprocess.run,
        send: bool = True) -> list[dict]:
    """Store each new item ({id, kind, pr: {url, repo, number, title}, headline, row,
    detail}, and optionally href, where the banner and the row link instead of the PR)
    and send its banner. An id already stored is skipped. Returns the items added."""
    path = path or PATH
    added = []
    with _locked(path):
        data = load(path)
        for it in items:
            if it["id"] in data:
                continue
            data[it["id"]] = {**it, "at": int(time.time()), "seen_at": None}
            added.append(it)
        _save(data, path)
    if send:
        from watcher import popups
        for it in added:
            popups.send(popups.FYI, it["pr"], "low", f"⚪ {it['headline']}",
                        it.get("subtitle") or "", it.get("detail") or " ", cfg,
                        href=it.get("href") or it["pr"].get("url"), job=it["id"], run=run)
    return added


def on_render(reason: str, now: float | None = None, path: Path | None = None,
              withdraw=None) -> list[dict]:
    """The items a menu render shows, oldest first. A render from opening the menu
    (`reason` MENU_OPEN) marks each unseen item seen and withdraws its banner
    (`withdraw(groups)`, watcher/popups.py by default); an item seen SEEN_FOR seconds
    ago or more is dropped, on any render."""
    path = path or PATH
    now = time.time() if now is None else now
    if not path.exists():
        return []
    with _locked(path):
        data = load(path)
        gone = [i for i, it in data.items()
                if it.get("seen_at") and now - it["seen_at"] >= SEEN_FOR]
        for i in gone:
            data.pop(i)
        seen_now = []
        if reason == MENU_OPEN:
            for i, it in data.items():
                if not it.get("seen_at"):
                    it["seen_at"] = now
                    seen_now.append(i)
        if gone or seen_now:
            _save(data, path)
    if seen_now:
        if withdraw is None:
            from watcher import popups

            def withdraw(groups):
                popups.withdraw({g: "seen in the menu" for g in groups})
        withdraw([f"fyi-{i}" for i in seen_now])
    return sorted(data.values(), key=lambda it: it.get("at") or 0)


# --- does an approval only praise? -------------------------------------------------------

FOOTER = re.compile(r"\n*(?:drafted|generated) with claude[^\n]*\s*$", re.IGNORECASE)
_PRAISE = re.compile(
    r"^(?:lgtm|looks good(?: to me)?|looks great|nice(?: work| job| one)?|great(?: work| job)?|"
    r"good (?:work|job|stuff)|ship it|approved?|thanks?(?: you)?|thx|well done|awesome|"
    r"love it|perfect|beautiful|solid(?: work)?|clean|:\+1:|:shipit:|:rocket:|👍|🚀|🎉|✅|💯|🙌)"
    r"(?:[\s!.,]+(?:lgtm|nice|thanks?|great|👍|🚀|🎉|✅|💯|🙌|:\+1:|:shipit:|:rocket:))*[\s!.]*$",
    re.IGNORECASE)
# Words that ask for something: a body with any of them is never praise only.
_ASKS = re.compile(r"\?|\b(?:please|could you|can you|would you|should|must|nit|consider|"
                   r"but|before (?:merg|we)|todo|follow[- ]?up|fix|rename|change|instead|"
                   r"suggest|question|wonder|maybe)\b", re.IGNORECASE)

PRAISE_PROMPT = """A reviewer approved a pull request with this comment. Does the comment ask
the author to do, change, answer or consider anything, now or later? Praise, thanks and
"looks good" ask nothing.

Comment:
<<<
{body}
>>>

Output ONLY a JSON object, no prose, no code fences: {{"asks": true | false}}"""


def strip_footer(body: str) -> str:
    return FOOTER.sub("", (body or "").strip()).strip()


def praise_only(body: str, cfg: dict, ask_claude=None) -> bool:
    """True when an approval's body asks nothing of you: empty, an obvious "LGTM", or
    judged so by Claude (`[models] fyi`). A body that mentions a question, a nit, a
    fix, "please"… is never praise only, and any doubt (Claude unreachable, an odd
    answer) counts as asking, so the approval keeps its usual handling."""
    text = strip_footer(body)
    if not text:
        return True
    if len(text) <= 80 and _PRAISE.match(text):
        return True
    if _ASKS.search(text):
        return False
    try:
        return (ask_claude or _ask_claude)(text, cfg) is False
    except Exception:  # noqa: BLE001 — doubt means it asks
        return False


def _ask_claude(text: str, cfg: dict) -> bool | None:
    """Claude's answer to "does it ask anything?"; None when there isn't a clear one."""
    from watcher import followup
    proc = subprocess.run(
        ["claude", "-p", "--model", cfg["models"].get("fyi") or "haiku", "--output-format", "json",
         "--permission-mode", "dontAsk", "--disallowedTools", "Bash", "Write", "Edit", "Read",
         "WebFetch", "WebSearch", "Agent"],
        input=PRAISE_PROMPT.format(body=text[:2000]), capture_output=True, text=True,
        timeout=90, env=followup._env())
    envelope = json.loads(proc.stdout or "{}")
    result = str(envelope.get("result") or "").strip().strip("`")
    result = result[result.find("{"):result.rfind("}") + 1] if "{" in result else result
    asks = json.loads(result).get("asks")
    return asks if isinstance(asks, bool) else None
