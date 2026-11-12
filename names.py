"""People's GitHub profile names, for the menu, notifications and status files.

`book(fetch=...)` returns a mapping from login to display name with the dict-style
reads the callers use (`get`, `in`, `[]`). A login whose profile has no name, or
that couldn't be looked up, maps to itself through `get`.

A login listed in the config's `[names]` table shows that name instead, with no lookup.
Other names come from `gh api users/<login>` and are cached in state/names.json for
REFRESH_DAYS. Only a book made with fetch=True looks up logins it hasn't seen; the
menu's instant render passes fetch=False and reads the cache, which the menu's
background fetch and the watcher's tick keep filled.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import settings

CACHE = settings.STATE_DIR / "names.json"
REFRESH_DAYS = 30


def _gh_name(login: str) -> str | None:
    """The profile name ('' when the profile has none); None when the lookup failed."""
    try:
        proc = subprocess.run(["gh", "api", f"users/{login}", "--jq", ".name // \"\""],
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _save(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except OSError:
        pass


class Book:
    def __init__(self, fetch: bool, path: Path = CACHE, lookup=_gh_name, now=time.time,
                 overrides: dict[str, str] | None = None):
        self._fetch = fetch
        self._overrides = overrides or {}
        self._path = path
        self._lookup = lookup
        self._now = now
        self._cache = _load(path)

    def _resolve(self, login: str) -> str:
        if not login or login.startswith("team:") or login == "?":
            return ""
        if self._overrides.get(login):
            return self._overrides[login]
        hit = self._cache.get(login)
        fresh = hit and self._now() - hit.get("at", 0) < REFRESH_DAYS * 86400
        if fresh or not self._fetch:
            return (hit or {}).get("name", "")
        name = self._lookup(login)
        if name is None:
            return (hit or {}).get("name", "")
        self._cache[login] = {"name": name, "at": int(self._now())}
        _save(self._path, self._cache)
        return name

    def get(self, login: str, default: str | None = None) -> str | None:
        return self._resolve(login) or default

    def __contains__(self, login: str) -> bool:
        return bool(self._resolve(login))

    def __getitem__(self, login: str) -> str:
        name = self._resolve(login)
        if not name:
            raise KeyError(login)
        return name


def _overrides() -> dict[str, str]:
    try:
        table = settings.load().get("names") or {}
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in table.items() if isinstance(v, str) and v.strip()}


def book(fetch: bool = False) -> Book:
    return Book(fetch, overrides=_overrides())
