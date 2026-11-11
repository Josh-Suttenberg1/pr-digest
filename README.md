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
7. **Red alerts:** `bin/watcher alert-app` builds `~/Applications/PR Digest
   Alerts.app`, a copy of terminal-notifier that red notifications go through. In
   **System Settings → Notifications → PR Digest Alerts**, allow notifications and
   set the style to **Alerts**. `bin/watcher notify-test --withdraw-after 10` shows a
   red alert and a yellow banner, then takes both back. Without the app, red alerts
   go through SwiftBar and can't be taken back.
8. **Links:** `url-handler/install.sh` registers the `prdigest://` links that
   notifications and draft pages use to open a Claude session.

### Your config

| Setting | What it does |
|---|---|
| `me` | Your GitHub login. |
| `workspace` | The folder you start Claude in. Reviews, follow-ups and "Open in Claude" sessions start there, so they load its `CLAUDE.md` and `.claude/agents/`. Your clones are expected at `<workspace>/<repo>`. |
| `repos_root` | Only if your clones live somewhere other than the workspace. |
| `[review] reviewer_agent` | Your review agent's name. `""` turns reviews off. |
| `[review] verifier_agent` | An agent that checks the reviewer's findings before you see them. `""` skips it. |
| `[review] auto_draft` | Off by default. When on, every review you owe gets a draft without being asked, and the draft updates itself when the author pushes (see "Automatic review drafts"). |
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
PRs per color, e.g. `🔴2 🟡1` (`✓` when there's nothing to do). A grey legend at
the top explains the dots: 🔴 act now · 🟡 act soon · 🟢 new · ⚪ no action
needed. Each row
reads `<dot> <ticket> · <what happened> · <PR title>`, so the ticket always says which
PR it is and truncation cuts the title, not the action. The dropdown has two
sections, and each PR appears in exactly one:

1. **My PRs** — every open PR you author, under three sub-headers:
   - **Needs you:** 🔴 for a change request you haven't re-requested, or any comment
     to answer that isn't a nit or a suggestion (`Answer Mykal's change request: 1
     question · 1 of 2 approvals · 1d`). 🟡 when only nits and suggestions wait
     (`Reply to Woods: 3 suggestions, 5 nits · 2 of 2 approvals · today`).
   - **Waiting on others:** who you're waiting on and since when, for a review or a
     re-review (`Waiting on re-review: Mykal · 5d`). 🟡 once the wait passes
     `[priority] stale_after_days`, as a cue to nudge them.
   - **Ready:** the required approvals, nothing to answer and no change request
     standing. In a repo whose rules require an approval of the latest push (ui), a
     PR with none on its head commit says `Ready · re-approve the latest push before
     merge`, still with no dot.

   The submenu says why the row has its dot, lists approvals with dates (marking any
   given before the latest push), change requests and whose move each is, who hasn't
   reviewed, every comment to answer by kind (Blocking / Suggestion / Question / Nit,
   linked to its thread), and **Go through these with Claude**. GitHub drafts with
   nothing to answer collapse into one submenu.
2. **Other PRs** — everyone else's open PRs you're involved in:
   - **Needs you:** follow-ups, mentions and reviews you owe, most severe first.
     - A follow-up is one of your reviews someone responded to, or an @mention. 🔴 for
       a mention, a re-request, or an answer to (or code change under) one of your
       blocking comments; 🟡 when only non-blocking ones got responses. The watcher
       drafts a reply to each response and each mention, and the submenu lists them
       with their drafted replies (Post / Edit first / Read Claude's follow-up /
       Dismiss), then **Post the drafted replies** and the review draft's actions.
     - A review you owe is a PR you haven't reviewed yet (or whose review of yours was
       dismissed). 🟡 when stalled: waiting more than `[priority] stale_after_days`,
       under the required approvals, and at most `max_engaged_for_stale` others
       engaged. 🟢 otherwise. Each row has its review draft's status and actions.
       With `[review] auto_draft` on, the row names who approved and the draft's
       state (`Review it · Mykal approved (1 of 2) · draft ready · 5d`), its tooltip
       says what the draft cost, and it appears only once the draft is ready.
   - **Waiting on others:** PRs you left comments or a change request on whose author
     hasn't responded to them yet, with how long since your review. No dot.
   - **Nothing needed (N):** a collapsed submenu with the rest (parked, already
     approved, GitHub drafts), each with the reason in words. ⚪, never counted, never
     notified.

The menu-bar title counts 🔴 and 🟡 only; 🟢 new review requests show in the menu but
don't count. Every row about a PR's reviews ends with its approval tally.

PRs for the same Jira ticket show as one row in either section: sub-tasks under
their parent (an epic never groups its children), and PRs in several repos that share
a ticket key. The row takes its most urgent PR's dot and sub-header and counts its PRs
by state (`WS-5769 · 2 PRs: 2 needs you · Record rejections on the parent form`); its
submenu lists each PR, with its repo, and that PR's own submenu. Parents are looked
up with `acli` and cached for a week (`[jira]` in the config; `cli = ""` turns the
lookup off).

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

**Notifications** come only for PRs that turn red or yellow, one per PR. The title
says what set it off, naming who and the ticket (`🟡 Mykal approved ABC-123`,
`🔴 Woods requested changes on ABC-123 with 2 comments`); the body says where the PR
stands now, approval tally included, then what was said. A distinct chime plays for
red and a softer one for yellow
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
watcher sends is logged to `~/.cache/pr-digest/watcher.log`.

**Comments on your PRs come with drafted replies.** Each comment newly waiting on you
(an open thread whose last word isn't yours, a top-level comment, or a change request's
body) gets a reply drafted by Claude from the PR's code (`[models] followup`), unless an
auto-fix is working on it. The draft takes a stance: it answers a question, agrees to a
change (said as a plan, never as done), pushes back with the reason, or says it needs
your call and what to decide. The PR's row shows "Claude is drafting replies" with no
dot until they're written; then the dot and the popup arrive together, and the popup
opens the drafts as a page. In the menu each comment shows its draft under it, with
Post this reply, Edit first and Dismiss, and the PR has Post the drafted replies.
Nothing posts until you click.

**Red stays, the rest slide away.** A red notification is an alert that stays on
screen, sent through the PR Digest Alerts app; yellow and green are banners that slide
away into Notification Center.

**A notification goes away once it's done.** The watcher re-checks every PR with a
notification out on every tick, and takes the notification out of Notification Center
once the thing it asked for is done, wherever you did it (the menu, a Claude session,
github.com): your PR has nothing left to answer, you answered a follow-up or mention,
you reviewed a PR you owed a review, an auto-fix that escalated was answered, or the
PR merged or closed. Clicking a notification or a menu link doesn't count as done by
itself. A stale wait (🟡, time to nudge someone) stays until the review arrives. The
menu refetches at the same moment, so its row goes too. Each withdrawal is logged to
`~/.cache/pr-digest/watcher.log` (`withdrew popup: …`), and what's out is recorded in
`state/watcher/popups.json` (`watcher/popups.py`).

**FYI (⚪): news that asks nothing of you.** Someone approved your PR with no comment,
or with praise only ("LGTM 🚀"; anything that might ask for something keeps its usual
handling, and Claude judges the unclear ones with `[models] fyi`); one of your PRs
became Ready; or a review you owed is no longer needed because the PR merged or closed
or you were removed as a reviewer. Each gets one slide-away banner with no sound and a
⚪ row in the menu's FYI section. Opening the menu marks the rows seen and takes their
banners out of Notification Center; they're gone the next time you open it (at least
10 seconds later). They're stored in `state/watcher/fyi.json` (`watcher/fyi.py`) and
never count in the title.

### Automatic review drafts

With `[review] auto_draft = true`, the watcher's tick also runs `review/auto_draft.py`:

- **Every review you owe gets a draft,** in the order the review list ranks them, with
  no limit on how many run at once. A PR that already has the required approvals gets
  none, unless its author is in `[triage] pod`.
- **The row and the notification wait for the draft.** Nothing shows while it's being
  written. Then the row appears with `draft ready` and one notification opens the draft
  as a page. If the draft fails, or isn't ready after `fallback_minutes` (45), the row
  and notification come anyway with `draft failed` or `no draft`.
- **New commits update the draft.** A merge from the base branch that changes nothing in
  the PR's own diff leaves the draft current. Other new commits mark it `draft outdated`
  and, once the author has stopped pushing for `quiet_minutes` (10), an update review
  covers only what changed (`review/interdiff.py`, `review/update_review.py`): each
  earlier finding addressed or not, each open review comment on the changed lines, and
  any new issue, at the top of the draft. When the change passes `update_max_share` of
  the PR's changed lines, or touches a file the draft never saw, a full review runs
  instead. The row goes back to `draft ready` with no second notification.
- `bin/watcher drafts --dry-run` says what a pass would start; `bin/watcher status`
  counts the drafts by state; `state/drafts/<repo>#<n>.json` holds each PR's
  scheduling state and `state/reviews/<repo>#<n>.json` what its draft reviewed.

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
| `review/review_pr.py`, `bin/review-pr` | Runs the reviewer and verifier agents and saves the draft. |
| `review/auto_draft.py`, `review/interdiff.py`, `review/update_review.py`, `review/record.py` | Automatic drafts and their updates on new commits; the per-PR record of what was reviewed. |
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
