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

## Setting up your own copy

This gets you the menu bar, notifications about your PRs and your reviews, and,
if you want them, Claude reviews and automatic re-approval. macOS only.

1. **Install** the repo as in [Setup](#setup) above, with the `.env` token.
2. **Log in to GitHub's CLI:** `gh auth login`. The triage, the watcher and every
   post (a drafted reply, a re-approval) go through `gh`, as you.
3. **Install the Claude Code CLI and log in** (`claude` on your PATH), if you want
   reviews or follow-ups. Without it you still get the menu and notifications.
   "Open in Claude" opens a new tab in iTerm2, so it needs iTerm2 installed.
4. **Write your config:** copy `config.example.toml` to
   `~/.config/pr-digest/config.toml` and fill it in (see below).
5. **Menu bar:** install SwiftBar and point it at `swiftbar-plugins/`, as described
   under [Menu bar app](#menu-bar-app-swiftbar). Then set **System Settings →
   Notifications → SwiftBar → Alerts**, so notifications stay on screen until you
   close them.
6. **Watcher:** `bin/watcher install` starts it under launchd (every 2 minutes);
   `bin/watcher status` checks it, and `bin/watcher notify-test` plays each sound.
7. **Links:** `url-handler/install.sh` registers the `prdigest://` links that
   notifications and draft pages use to open a Claude session.

### Your config

| Setting | What it does |
|---|---|
| `me` | Your GitHub login. |
| `workspace` | The folder you start Claude in. Reviews, follow-ups and "Open in Claude" sessions start there, so they load its `CLAUDE.md` and `.claude/agents/`. Your clones are expected at `<workspace>/<repo>`. |
| `repos_root` | Only if your clones live somewhere other than the workspace. |
| `[review] reviewer_agent` | Your review agent's name. `""` turns reviews off. |
| `[review] verifier_agent` | An agent that checks the reviewer's findings before you see them. `""` skips it. |
| `[watcher] auto_reapprove` | Off by default. When on, it re-approves a PR you approved once the author has done everything your comments asked and no blocking comment or change request from anyone is left unanswered. The follow-up's menu entry says what's holding it otherwise. |
| `[notify] sounds` | `false` makes every notification silent. |

Everything else has a default in `settings.py`.

**How your review agent is found.** The tool runs `claude --agent <name>` from your
workspace, so Claude looks for the agent in `<workspace>/.claude/agents/` and then
`~/.claude/agents/`, the same places it looks when you start a session there. Its
model and effort come from the agent file's frontmatter. The draft it writes must
start each PR's section with `## <TICKET> … avetta/<repo>#<n> … head <sha>` and end
with a `SEVERITY: substantive | nits-only | clean` line; see `review/review_pr.py`.

**Names.** People show by their GitHub profile name, or their login when the profile
has none. Nicknames belong in your own `CLAUDE.md`, which your Claude sessions read.

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
plugin. Every PR gets **one row** with a priority dot, and the menu-bar title counts
PRs per color, e.g. `🔴2 🟡1 🟢2` (`✓` when there's nothing to do). A grey legend at
the top explains the dots: 🔴 act now · 🟡 act soon · 🟢 new · ⚪ no action
needed. Each row
reads `<dot> <ticket> · <what happened> · <PR title>`, so the ticket always says which
PR it is and truncation cuts the title, not the action. The dropdown has four
sections, and each PR appears in exactly one:

1. **My PRs** — every open PR you author, under three sub-headers:
   - **Needs you:** 🔴 for a change request you haven't re-requested, or any comment
     to answer that isn't a nit or a suggestion (`Answer Mykal's change request: 1
     question · 1d`). 🟡 when only nits and suggestions wait (`Reply to Woods: 3
     suggestions, 5 nits · today`), or when the PR needs re-approval: it has the
     required approvals, but fewer than that on the latest push.
   - **Waiting on others:** who you're waiting on and since when, for a review or a
     re-review (`Waiting on re-review: Mykal · 5d`). 🟡 once the wait passes
     `[priority] stale_after_days`, as a cue to nudge them.
   - **Ready:** the required approvals on the latest push, nothing to answer and no
     change request standing.

   The submenu says why the row has its dot, lists approvals with dates (marking any
   given before the latest push), change requests and whose move each is, who hasn't
   reviewed, every comment to answer by kind (Blocking / Suggestion / Question / Nit,
   linked to its thread), and **Go through these with Claude**. GitHub drafts with
   nothing to answer collapse into one submenu.
2. **Follow-ups and mentions** — others' PRs you reviewed where someone responded,
   and any PR where someone @mentioned you. 🔴 for a mention, a re-request, or an
   answer to (or code change under) one of your blocking comments; 🟡 when only
   non-blocking ones got responses. The watcher drafts a reply to each response and
   each mention. The submenu lists them with their drafted replies (Post / Edit first
   / Read Claude's follow-up / Dismiss), then **Post the drafted replies** and the
   review draft's actions.
3. **Reviews you owe** — PRs you haven't reviewed yet (or whose review of yours was
   dismissed). 🟡 when stalled: waiting more than `[priority] stale_after_days`, under
   the required approvals, and at most `max_engaged_for_stale` others engaged. 🟢
   otherwise. Each row has its review draft's status and actions.
4. **Non action requiring updates** — ⚪ low priority: every other open PR you're
   involved in, with what happened in words (the author is still working, waiting on
   the author, parked, already approved, GitHub drafts). Never counted, never notified.

Holding ⌥ turns a colored row into **Dismiss**: it loses its dot until something newer
happens on the PR. The rules live in `triage/priority.py`, which the menu, the
watcher's notifications, the status files and the personal readouts all share.

Review drafts and follow-up reports open as rendered pages in your browser
(**📖 Read draft**, via `review/view_draft.py`; nothing leaves your machine). The page's
**Discuss with Claude** button opens a Claude Code session in iTerm primed on that
draft, once you register the `prdigest://` link handler: run `url-handler/install.sh`
(`--remove` to undo).

Each refresh also writes `output/status/`: an `index.md` and one file per PR stating
its section, its priority, the reason, and the facts behind it, so a Claude session can start from
them. The menu's Open in Claude items point the session at that file.

**Notifications** come only for PRs that turn red or yellow, one per PR, titled with
the dot, the ticket and the reason (`🔴 ABC-123 · Mykal answered 2 of your
comments, 1 blocking`), with a distinct chime for red and a softer one for yellow
(`[notify] red_sound` / `yellow_sound`, played with `afplay`). All of a tick's events
on a PR make one notification; later activity at the same or a lower color only
updates the menu, and a PR pops up again only when it rises (yellow → red) or after
its row clears. Approvals, green review requests and activity on PRs you haven't
engaged with never pop up. A notification only links to something that exists:
clicking one of your PRs' opens a Claude session on its comments (a
`prdigest://respond` link, so it needs the link handler above). A follow-up or
mention waits until Claude's drafted replies are written, then opens them as a page;
if the drafts don't come within 30 minutes it goes out linking to the comment on
GitHub. A re-request on its own opens the PR. Every notification the
watcher sends is logged to `~/.cache/pr-digest/watcher.log`. Set SwiftBar to
**Alerts** in System Settings → Notifications so they stay on screen.

Other people's comments on PRs you haven't engaged with never appear. Bots (GitHub
Apps, `*[bot]` logins, and the `bots` list in `~/.config/pr-digest/config.toml`) never
count as someone waiting on you.

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
| `✓` | Nothing is your move. |
| `🔴2 🟡1 🟢2` | PRs per color: 2 need you now, 1 has waited a while, 2 are new. |
| `🔴2 🟡1` 🔄 (spinner) | Refreshing in the background — the counts and list stay visible. |

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
