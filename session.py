"""Open an interactive Claude session in a new iTerm tab.

Used by the menu bar's "Open in Claude" items and by notification clicks (through
the prdigest://respond link, see respond_url):

    .venv/bin/python session.py --prompt "Let's go over the draft review for ..."

The tab starts in the configured claude_cwd (~/avetta), so the session loads
the same CLAUDE.md files as one Josh starts by hand.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from urllib.parse import quote

import settings


def _applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def iterm_script(command: str) -> str:
    return "\n".join(
        [
            'tell application "iTerm"',
            "  activate",
            "  if (count of windows) = 0 then",
            "    create window with default profile",
            "  else",
            "    tell current window to create tab with default profile",
            "  end if",
            f"  tell current session of current window to write text {_applescript_string(command)}",
            "end tell",
        ]
    )


def claude_command(prompt: str, cwd: str) -> str:
    return f"cd {shlex.quote(cwd)} && claude {shlex.quote(prompt)}"


def respond_prompt(pr_url: str) -> str:
    """The opening prompt for working through the review comments on one of your PRs.
    The status file holds each comment's full text, so the session starts from it."""
    import status_report
    return (f"Let's go through the review comments waiting on me on my PR {pr_url}. "
            f"Every comment's full text, and who approved or requested changes, is in "
            f"{status_report.path_for(pr_url)}; if its 'Status as of' time predates the "
            f"newest comment, refresh with my-pr-followups.py --comments. Walk me through "
            f"each comment with your take on it. Don't change code or post anything until "
            f"I say.")


def respond_url(pr_url: str) -> str:
    """A prdigest:// link that opens a respond_prompt session; notifications use it as
    their click target. review/view_draft.py --handle-url validates and launches it."""
    return f"prdigest://respond?pr={quote(pr_url, safe='')}"


def launch(prompt: str, cfg: dict | None = None, run=subprocess.run) -> int:
    cfg = cfg or settings.load()
    cwd = str(settings.path_of(cfg["claude_cwd"]))
    script = iterm_script(claude_command(prompt, cwd))
    return run(["osascript", "-e", script], capture_output=True).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prompt", required=True)
    args = parser.parse_args(argv)
    return launch(args.prompt)


if __name__ == "__main__":
    sys.exit(main())
