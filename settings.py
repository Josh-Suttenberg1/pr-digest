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
OUTPUT_DIR = REPO_DIR / "output"

DEFAULTS: dict = {
    "me": "",
    "org": "avetta",
    # The folder you start Claude in. Headless runs and "Open in Claude" sessions start
    # here, so they load its CLAUDE.md and its .claude/agents/; the lessons harvest reads
    # and writes <workspace>/.claude/review/.
    "workspace": "~",
    # Where repo clones live, as <repos_root>/<repo>. Empty means the workspace.
    "repos_root": "",
    "review": {
        "reviewer_agent": "pr-reviewer",
        "verifier_agent": "pr-review-verifier",
        "drafts_dir": str(OUTPUT_DIR / "reviews"),
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
        # Re-approve a PR you approved once the author has done everything your comments
        # asked and no blocking comment or change request from anyone is unanswered
        # (watcher/followup.py reapproval_blockers). It posts as you, so it's off by default.
        "auto_reapprove": False,
        "bots": ["copilot-pull-request-reviewer", "avettabot"],
        "reports_dir": str(OUTPUT_DIR / "followups"),
    },
    "triage": {
        # Your pod: their review requests are red from the start and stay red until
        # you've reviewed (triage/priority.py owed), and rank higher in the list.
        "pod": [],
        # Check names (case-insensitive substrings) whose failure doesn't park a PR.
        "ci_ignored": [],
    },
    "lessons": {
        # Repos whose review threads are harvested; empty means the harvest does nothing.
        "repos": [],
        "days": 90,
        "out_dir": str(OUTPUT_DIR / "lessons"),
    },
    "notify": {
        "swiftbar_plugin": "prdigest.1h.sh",
        # Off: every notification is silent.
        "sounds": True,
        "alert_sound": "Glass",
        # Played with afplay for a PR's notification, by its priority color.
        "red_sound": "/System/Library/Sounds/Glass.aiff",
        "yellow_sound": "/System/Library/Sounds/Tink.aiff",
        # The quiet "✅ Fixed for you" notification (watcher/autofix.py).
        "done_sound": "/System/Library/Sounds/Purr.aiff",
    },
    # watcher/autofix.py: fix a reviewer's nit or suggestion on your own PR, push it,
    # reply, resolve the thread and re-request the reviewer. It commits and posts as
    # you, so it's off by default; with dry_run on it stops before the commit.
    "autofix": {
        "enabled": False,
        "dry_run": True,
        # comment_kind kinds that qualify.
        "categories": ["nit", "suggestion"],
        # The assessment's confidence a fix needs before anything is edited.
        "min_confidence": 0.9,
        # Lines added plus removed that one fix may change.
        "max_lines": 40,
        # The edit prompt template; "" uses watcher/autofix_prompt.md.
        "prompt": "",
        # "" uses models.followup.
        "model": "",
        "assess_timeout": 600,
        "edit_timeout": 900,
        "validate_timeout": 1800,
        # One clone per PR, as <worktrees_dir>/<repo>-<number>.
        "worktrees_dir": str(OUTPUT_DIR / "worktrees"),
        # The agents an Elm repo's fix runs, in order, from <workspace>/.claude/agents/ or
        # ~/.claude/agents/ (watcher/autofix.py plan_validation).
        "elm_agents": ["elm-validation", "elm-conventions-review"],
        # repo -> a command that validates a fix, run in its worktree. Only an override:
        # each repo's validation is worked out from its files (elm.json, build.sbt).
        "validate": {},
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
    # triage/jira.py: PRs whose tickets are sub-tasks of one Jira ticket show as one
    # group in the menu. `cli` is the Jira command (Atlassian's acli); empty turns the
    # lookup off, and only PRs sharing a ticket key group. A ticket's parent is cached
    # for cache_days.
    "jira": {
        "cli": "acli",
        "cache_days": 7,
        # Prefix that turns a ticket key into a link, e.g.
        # "https://example.atlassian.net/browse/"; "" leaves story rows unlinked.
        "browse_url": "",
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
    """Defaults overlaid with the user's config file, when it exists. repos_root is
    filled in from workspace when the file leaves it empty."""
    user: dict = {}
    if path.exists():
        with path.open("rb") as f:
            user = tomllib.load(f)
    cfg = _merge(DEFAULTS, user)
    cfg["repos_root"] = cfg["repos_root"] or cfg["workspace"]
    return cfg


def path_of(value: str) -> Path:
    return Path(value).expanduser()


def workspace(cfg: dict) -> Path:
    return path_of(cfg["workspace"])


def review_notes_dir(cfg: dict) -> Path:
    """Where the reviewer's hand-kept and harvested notes live."""
    return workspace(cfg) / ".claude" / "review"
