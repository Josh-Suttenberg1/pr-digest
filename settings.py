"""User configuration shared by review-pr, the watcher, and the lessons harvest.

Settings live outside the repo in ~/.config/pr-digest/config.toml (override the
path with PR_DIGEST_CONFIG). Every key has a default here, so the file only
needs the values that differ. See config.example.toml for the full set.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

CONFIG_PATH = Path(
    os.environ.get("PR_DIGEST_CONFIG", "~/.config/pr-digest/config.toml")
).expanduser()

REPO_DIR = Path(__file__).resolve().parent
STATE_DIR = REPO_DIR / "state"

DEFAULTS: dict = {
    "me": "",
    "org": "avetta",
    "repos_root": "~/avetta",
    "claude_cwd": "~/avetta",
    "review": {
        "reviewer_agent": "pr-reviewer",
        "verifier_agent": "pr-review-verifier",
        "drafts_dir": "~/avetta/JoshWorkspace/code_reviews",
        "timeout": 3600,
        "max_concurrent": 2,
    },
    "models": {
        "followup": "sonnet",
        "lessons": "sonnet",
    },
    "watcher": {
        "poll_interval": 120,
        "required_approvals": 2,
        "max_concurrent": 2,
        "followup_timeout": 600,
        "max_notifs_per_tick": 6,
        "bots": ["copilot-pull-request-reviewer", "avettabot"],
        "reports_dir": "~/avetta/JoshWorkspace/pr_followups",
    },
    "lessons": {
        "repos": ["processservice", "process-domain", "ui", "workerbridgeservice"],
        "days": 90,
        "out_dir": "~/avetta/JoshWorkspace/review_lessons",
    },
    "notify": {
        "swiftbar_plugin": "prdigest.1h.sh",
        "alert_sound": "Glass",
    },
    "session": {
        "terminal": "iterm",
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load(path: Path = CONFIG_PATH) -> dict:
    """Defaults overlaid with the user's config file, when it exists."""
    if path.exists():
        with path.open("rb") as f:
            return _merge(DEFAULTS, tomllib.load(f))
    return _merge(DEFAULTS, {})


def path_of(value: str) -> Path:
    return Path(value).expanduser()
