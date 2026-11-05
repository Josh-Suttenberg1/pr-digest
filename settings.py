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

# Code and data live apart. REPO_DIR is where this code runs from: the checkout while
# developing, or an installed release (bin/deploy) under <install>/releases/<sha>.
# DATA_DIR holds everything the code writes or reads as data (state/, output/, .env,
# review-drafter/state/): $PR_DIGEST_DATA, else the path a release records in its
# `.data_dir` file, else the checkout itself.
REPO_DIR = Path(__file__).resolve().parent


def _data_dir() -> Path:
    env = os.environ.get("PR_DIGEST_DATA", "").strip()
    if env:
        return Path(env).expanduser()
    marker = REPO_DIR / ".data_dir"
    if marker.exists():
        return Path(marker.read_text().strip()).expanduser()
    return REPO_DIR


DATA_DIR = _data_dir()
STATE_DIR = DATA_DIR / "state"
OUTPUT_DIR = DATA_DIR / "output"
# The path SwiftBar, launchd and menu actions run the code through: an installed
# release's `current` link, so a deploy switches them all at once; else the checkout.
RUN_DIR = REPO_DIR.parent.parent / "current" if REPO_DIR.parent.name == "releases" else REPO_DIR

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
        # Re-approve a PR Josh already approved when its author did exactly what his
        # comment asked (see watcher/followup.py reapproval_plan).
        "auto_reapprove": True,
        "bots": ["copilot-pull-request-reviewer", "avettabot"],
        "reports_dir": "~/avetta/JoshWorkspace/pr_followups",
    },
    "triage": {
        # Authors whose PRs rank higher in the reviews-you-owe list.
        "pod": [],
        # Check names (case-insensitive substrings) whose failure doesn't park a PR.
        "ci_ignored": [],
    },
    "lessons": {
        "repos": ["processservice", "process-domain", "ui", "workerbridgeservice"],
        "days": 90,
        "out_dir": "~/avetta/JoshWorkspace/review_lessons",
    },
    "notify": {
        "swiftbar_plugin": "prdigest.1h.sh",
        "alert_sound": "Glass",
        # Played with afplay for a PR's notification, by its priority color.
        "red_sound": "/System/Library/Sounds/Glass.aiff",
        "yellow_sound": "/System/Library/Sounds/Tink.aiff",
    },
    # triage/priority.py: a review you owe turns yellow once it has waited more than
    # stale_after_days with fewer than watcher.required_approvals approvals and at most
    # max_engaged_for_stale people (besides the author and bots) engaged.
    "priority": {
        "stale_after_days": 2,
        "max_engaged_for_stale": 1,
    },
    "session": {
        "terminal": "iterm",
    },
    # GitHub login -> the name notifications use for that person.
    "nicknames": {},
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
