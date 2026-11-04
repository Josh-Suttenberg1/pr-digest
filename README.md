# pr-digest

A tiny tool that fetches your open GitHub pull requests and surfaces the ones
that need you — the PRs where you're a **requested reviewer** or the
**assignee**. Run it from the command line for a printed digest, or drop the
included **SwiftBar plugin** into your macOS menu bar for a glanceable count of
what is your move.

## Preview

The menu bar shows a count and turns red when something is new or updated since
you last looked. Open it for the full list, grouped by repo, as clickable links:

![PR Digest menu bar dropdown](docs/dropdown.png)

The icon at a glance:

![Menu bar icon states](docs/states.png)

> Screenshots use demo data (`PR_DIGEST_DEMO=1`); your real PRs never leave your
> machine.

## What it does

Using the GitHub REST search API (`GET /search/issues`), it runs three queries:

- `is:open is:pr review-requested:@me` — PRs waiting on your review
- `is:open is:pr assignee:@me` — PRs assigned to you
- `is:open is:pr author:@me` — PRs you opened

It de-duplicates across the lists, prints a clean digest grouped by repo
(title, number, author, repo, age in days, URL) with a total count at the top,
and also writes the same digest to a timestamped file in `./digests/`. If
nothing's waiting, it prints `No PRs need your attention.`

To scope the digest to one or more orgs (e.g. a work machine), set
`PR_DIGEST_ONLY_ORGS` in `.env` to a comma-separated owner allowlist — PRs from
other owners are dropped. Unset shows everything.

## Setup

Requires Python 3.9+.

```bash
git clone https://github.com/m0n01d/pr-digest.git
cd pr-digest

# 1. Install dependencies (a virtualenv is recommended)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure your token
cp .env.example .env
# then edit .env and paste your token
```

### Token

The tool reads a GitHub personal access token from the `GITHUB_TOKEN`
environment variable — never hardcoded. It's loaded from `.env` via
[python-dotenv](https://pypi.org/project/python-dotenv/) if present, and `.env`
is git-ignored.

The token only needs **read-only** access to pull requests:

- **Fine-grained PAT**: Repository permissions → **Pull requests: Read**
- **Classic PAT**: the **`repo`** scope

Create one at <https://github.com/settings/tokens>.

## Run

```bash
# with the venv active and .env in place
python3 pr_digest.py

# or via the wrapper (also used by cron/launchd below)
./run
```

You can also pass the token inline instead of using `.env`:

```bash
GITHUB_TOKEN=ghp_xxx python3 pr_digest.py
```

## Menu bar app (SwiftBar)

For a glanceable view instead of a file, there's a [SwiftBar](https://swiftbar.app)
plugin: one menu-bar icon that counts only what is **your move**, with a dropdown in
three sections:

1. **Waiting on you** — drafted replies to your review comments (from the watcher,
   with Post / Edit first / Open in Claude / Dismiss), and your own PRs that have a
   change request, an open thread, or a comment you haven't answered.
2. **Reviews you owe** — PRs requesting your review that are really on you, ranked.
   PRs with enough approvals, PRs waiting on their author, and PRs your review can't
   unblock yet (CI failing, waiting on someone else's change request) are left out.
   Each row shows its review status and a **Review with Claude** action.
3. **Everything else** — a collapsed list of every other open PR you're involved in,
   each with the reason it isn't counted, so you can check the sorting.

Review drafts and follow-up reports open as rendered pages in your browser
(**📖 Read draft**, via `review/view_draft.py`; nothing leaves your machine). The page's
**Discuss with Claude** button opens a Claude Code session in iTerm primed on that
draft, once you register the `prdigest://` link handler: run `url-handler/install.sh`
(`--remove` to undo).

Each refresh also writes `output/status/`: an `index.md` and one file per PR stating
its section, the reason, and the facts behind it, so a Claude session can start from
them. The menu's Open in Claude items point the session at that file.

The number counts sections 1 and 2. Other people's comments on PRs you haven't
engaged with never appear. Bots (GitHub Apps, `*[bot]` logins, and the `bots` list
in `~/.config/pr-digest/config.toml`) never count as someone waiting on you.

The menu **opens instantly** — it renders from a local cache, never blocking on
the network. Opening it (and an **hourly** timer) kicks off a detached
background fetch (at most every 5 minutes; a full triage takes about a minute);
the icon shows a **refresh spinner** while that runs, then the count and list
update in place. So you get a snappy menu *and* fresh data.

```bash
brew install --cask swiftbar

# Point SwiftBar at this repo's plugin folder, then launch it
# (use the absolute path to your clone — run `pwd` inside the repo)
defaults write com.ameba.SwiftBar PluginDirectory "$(pwd)/swiftbar-plugins"
open -a SwiftBar
```

On first launch SwiftBar may ask you to **choose the plugin folder** — pick
`pr-digest/swiftbar-plugins`. (macOS sandboxing means the folder sometimes has
to be granted through the picker, not just the `defaults` command above.)

It reuses the same `.env` token as the CLI, so no extra setup. The plugin
(`swiftbar_plugin.py`, launched by `swiftbar-plugins/prdigest.1h.sh`) shares the
fetch code in `gh_prs.py`.

The triage lives in `triage/` (`reviews.py` for reviews you owe, `mine.py` for your
own PRs); its knobs are under `[triage]` in the config.

The other menu bar tool in the plugin folder,
[`claude-heartbeat/`](claude-heartbeat/), shows whether each Claude Code session is
working, stalled, or idle. Run `claude-heartbeat/install.sh` once.

**Reading the icon:**

| Menu bar | Meaning |
|---|---|
| ✅ (green check) | Nothing is your move. |
| `3` 📥 | 3 reviews you owe. |
| `3` 📥 (red) | 3 items, and at least one is a drafted reply or your own PR waiting on you. |
| `3` 🔄 (spinner) | Refreshing in the background — count and list stay visible. |

**Refresh now** re-fetches on demand.

State lives in `state/` (git-ignored); `cache.json` holds the last fetched triage
so the menu can draw instantly.

> The SwiftBar plugin handles the live view; it does **not** write to
> `digests/`. The dated digest files stay the job of the CLI / launchd schedule
> below, so you keep both a glanceable menu and a daily archived record.

## Scheduling

The CLI deliberately has **no built-in scheduler** — run it once and let your
OS schedule it. (SwiftBar self-refreshes, so this section is only needed for the
dated `digests/` file archive.)

### cron (Linux / macOS)

Run once each weekday morning at 9:00. Edit your crontab with `crontab -e`:

```cron
0 9 * * 1-5  cd /path/to/pr-digest && GITHUB_TOKEN=ghp_xxx ./run >> cron.log 2>&1
```

The `./run` wrapper `cd`s to its own directory and activates `.venv`, so it
works regardless of cron's working directory. If you keep your token in `.env`
you can drop the inline `GITHUB_TOKEN=` (the script loads `.env` itself).

### launchd (macOS, recommended alternative)

macOS prefers `launchd`. Save this as
`~/Library/LaunchAgents/com.example.pr-digest.plist` (replace the
`/path/to/pr-digest` paths with your clone), then load it with
`launchctl load ~/Library/LaunchAgents/com.example.pr-digest.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.example.pr-digest</string>

  <key>ProgramArguments</key>
  <array>
    <string>/path/to/pr-digest/run</string>
  </array>

  <key>EnvironmentVariables</key>
  <dict>
    <key>GITHUB_TOKEN</key>
    <string>ghp_xxx</string>
  </dict>

  <!-- Weekdays (1=Mon … 5=Fri) at 09:00. launchd uses one dict per day. -->
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
  </array>

  <key>StandardOutPath</key>
  <string>/path/to/pr-digest/cron.log</string>
  <key>StandardErrorPath</key>
  <string>/path/to/pr-digest/cron.log</string>
</dict>
</plist>
```

## Error handling

The tool exits non-zero with a clear message on:

- **Missing token** — tells you to set `GITHUB_TOKEN` and which scope it needs.
- **Auth failure (401)** — token invalid or expired.
- **Forbidden (403)** — token missing the required scope.
- **Rate limit** — reports when the limit resets.
- **Network errors** — surfaces the underlying problem.

## Files

| File | Purpose |
|---|---|
| `gh_prs.py` | Shared fetch/auth logic (used by both front-ends). |
| `pr_digest.py` | The CLI — prints the digest and writes the dated file. |
| `swiftbar_plugin.py` | The SwiftBar menu: fetch, sort into sections, render. |
| `review/view_draft.py`, `url-handler/` | Draft pages in the browser + the `prdigest://` handler for Discuss with Claude. |
| `status_report.py` | Writes `output/status/` on each menu refresh: one plain-text file per PR saying where the menu put it and why, plus `index.md`, for Claude sessions to read. |
| `triage/` | Whose move is it: reviews you owe (`reviews.py`), your PRs waiting on you (`mine.py`). |
| `swiftbar-plugins/prdigest.1h.sh` | SwiftBar launcher (hourly + refresh-on-open). |
| `review-drafter/` | Review actions behind the menu's Review with Claude rows (see its README). |
| `claude-heartbeat/` | Claude session status menu + hook (see its README). |
| `swiftbar-plugins/claude-heartbeat.5s.sh` | Launcher for the heartbeat menu. |
| `run` | Wrapper for cron/launchd (activates venv, fixes cwd). |
| `requirements.txt` | `requests`, `python-dotenv`. |
| `.env.example` | Template — copy to `.env`. |
| `docs/` | README screenshots. |
| `digests/`, `state/`, `output/` | Generated output, menu and watcher state, draft pages (git-ignored). |
