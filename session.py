"""Open an interactive Claude session in a new iTerm tab.

Used by the menu bar's "Open in Claude" items and by notification clicks:

    .venv/bin/python session.py --prompt "Let's go over the draft review for ..."

The tab starts in the configured claude_cwd (~/avetta), so the session loads
the same CLAUDE.md files as one Josh starts by hand.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys

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
