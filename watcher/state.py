"""The watcher's saved state and its lock.

Everything lives under state/watcher/ in the repo (gitignored); the menu bar plugin
reads the same path. state.json is written to a temp file and renamed, so a crash
mid-write never leaves it half-written. tick holds an flock on lock for its whole
run, so overlapping launchd ticks skip instead of racing.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import settings  # noqa: E402

WATCHER_DIR = settings.STATE_DIR / "watcher"
STATE_PATH = WATCHER_DIR / "state.json"
LOCK_PATH = WATCHER_DIR / "lock"
JOBS_DIR = WATCHER_DIR / "jobs"
# PR url -> when Josh dismissed its menu row (ISO). Written by the menu; the tick reads
# it so the next response after a dismissal notifies afresh.
CLEARED_PATH = WATCHER_DIR / "cleared.json"

EMPTY = {"version": 1, "seeded_at": None, "last_ok_at": None, "last_sweep_at": None,
         "backoff": {"fails": 0, "next_at": 0, "alerted": False},
         "claude_auth_alerted": False, "rate": {}, "prs": {}}


def load(path: Path = STATE_PATH) -> dict:
    if not path.exists():
        return json.loads(json.dumps(EMPTY))
    with path.open() as f:
        data = json.load(f)
    return {**json.loads(json.dumps(EMPTY)), **data}


def save(data: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


def lock():
    """The held lock file, or None when another tick holds it."""
    WATCHER_DIR.mkdir(parents=True, exist_ok=True)
    f = LOCK_PATH.open("w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def load_cleared(path: Path = CLEARED_PATH) -> dict[str, str]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def mark_cleared(url: str, when: str, path: Path = CLEARED_PATH) -> None:
    data = load_cleared(path)
    data[url] = when
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


# Back-off after GitHub failures: 2, 4, 8, then every 30 minutes.
BACKOFF_STEPS = (120, 240, 480, 1800)


def backoff_delay(fails: int) -> int:
    return BACKOFF_STEPS[min(max(fails, 1), len(BACKOFF_STEPS)) - 1]
