#!/bin/bash
# <xbar.title>PR Digest</xbar.title>
# <xbar.version>v1.0</xbar.version>
# <xbar.desc>The PRs that are your move: replies waiting on you, your PRs, reviews you owe.</xbar.desc>
# <swiftbar.refreshOnOpen>true</swiftbar.refreshOnOpen>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
#
# Thin launcher: runs the real plugin with the project venv, forwarding any
# args (e.g. --fetch from the "Refresh now" menu item). All logic
# lives in swiftbar_plugin.py so this file rarely changes.
#
# Self-locating: REPO is the parent of this script's directory, so the repo
# can live anywhere — no hardcoded paths.
REPO="$(cd "$(dirname "$0")/.." && pwd)"
exec "$REPO/.venv/bin/python" "$REPO/swiftbar_plugin.py" "$@"
