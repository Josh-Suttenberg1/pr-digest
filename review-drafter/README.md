# pr-review-drafter

A body double for code review. It reviews your team's pull requests for you,
but you keep the final say.

For each open PR that requests your review, it runs an automatic style-guide
review with `claude` and saves the result on GitHub as a **pending review**
under your account. A pending review is a GitHub draft: only you can see it.
You open the PR, edit or delete any comment, pick **Approve**, **Comment**, or
**Request changes**, and press submit. Nothing reaches the author until you do.

Lives next to the pr-digest menu bar PR inbox. pr-digest shows what needs your
eyes; this tool does the first pass and queues drafts for your sign-off.

## Setup

Needs macOS, [SwiftBar](https://swiftbar.app), `jq`, the `gh` CLI (logged in) and
a logged-in `claude` CLI. Config lives outside the repo, in
`~/.config/pr-review-drafter/` (override with `PR_REVIEW_HOME`):

```
env               export PR_REVIEW_ORG=your-org        (required)
                  export PR_REVIEW_IGNORE='(^|/)generated/|\.lock$'   (optional)
                  export PR_REVIEW_KEEP='...'          (optional exceptions)
guides/default.md your team's review style guide       (used for every repo)
guides/<repo>.md  a guide for one repo, wins over default.md
extra-rules.md    optional personal rules appended to the prompt
```

Without a guide the review still runs, with no repo-specific rules.

## How it decides and reviews

Each run:

1. Finds open PRs in your org (`PR_REVIEW_ORG`) with `review-requested:@me` (not drafts).
2. Skips a PR when:
   - you already have a pending draft on it (GitHub allows one per user),
   - you already reviewed its current head commit,
   - this tool already drafted a review for its current head commit.
   New commits on a PR make it eligible again.
3. Fetches the diff and strips noise: paths matching `PR_REVIEW_IGNORE`
   (default `(^|/)generated/`), unless they match `PR_REVIEW_KEEP`.
4. Runs `claude -p` with your style guide: `guides/<repo>.md` if it exists,
   else `guides/default.md` (see [Setup](#setup)).
   Comments are simple english, max 4 sentences, ```suggestion blocks for
   small exact fixes. Every style-guide item carries a `nit:` prefix; a
   comment with no prefix is a defect the author must fix before merge, and
   only defects make the suggested verdict "request changes". A second pass
   re-checks the most-missed rules: touch each value once, sibling branches
   that differ (Ok/Err, per picker), fetch responses that can land on the
   wrong target, and stacked `||`/`&&` conditions.
5. Checks every comment against the real diff hunks. A comment that does not
   land on a diff line moves into the review body instead of being dropped.
6. Creates the pending review. The first line of the body is an HTML comment
   with the suggested verdict. It never renders, but you see it in the submit
   dialog.
7. Sends a macOS notification.

Diffs over 6000 lines are skipped with a notification: review those yourself.

## Menu bar (SwiftBar) — on demand, no background token burn

The PR digest menu (`swiftbar_plugin.py`) shows the reviews you owe and runs
these actions through [menubar.sh](menubar.sh) (`--review`, `--cancel`,
`--open-claude`). menubar.sh is no longer a SwiftBar plugin of its own; run with no
arguments it still prints the standalone list described here. Each PR row has:

- **View on GitHub** — just look, costs nothing.
- **🤖 Auto code review** — runs the engine for that one PR only.

Claude never runs unless you click. The 30 minute refresh and the
refresh-on-open only re-run the cheap GitHub scan. Row states after you click:
🔄 reviewing → notification with a sound → ⏳ draft ready (suggests verdict, n
comments, how long it took) → you open, edit, submit. PRs you already reviewed
show ✔; a draft that got new commits shows ⚠️.

While a review runs the row shows live progress. A big diff on `opus` takes
about 10 minutes (5,270 lines took 9m55s), so the row tells you:

- the current step and how long it has been on it: fetching the diff → diff
  is N lines → claude is reviewing → checking comment anchors → posting,
- how long ago you clicked, and the size and duration of the last draft,
- **✖ Cancel this review**, which kills the run and marks the row.

The menu bar icon shows `🔄 3m20s` while a run is in progress. The engine
re-renders the menu every 30 seconds, so the time moves without opening it.

## When a review fails

The engine needs a logged-in `claude` CLI. That login is separate from the
Claude desktop app and it expires on its own. Check it with:

```bash
claude auth status
```

If `loggedIn` is `false`, run `claude auth login`. The menu shows a banner in
this state, with a row that opens Terminal and starts the login.

Every failed attempt (logged-out CLI, empty diff, output that is not JSON,
GitHub rejecting the payload) does three things:

- sends a macOS notification with the reason,
- shows `⚠️ last attempt failed: <reason>` under the PR row; the
  **Auto code review** button stays so you can retry,
- for claude failures, saves stderr and the raw envelope to
  `state/last-failure.txt`.

The marker clears when a draft is created for that PR.

The review rows live in the digest menu; there is no separate menu item to remove.

## CLI

```bash
./drafter.sh                 # one normal run
./drafter.sh --dry-run       # print the reviews, post nothing
./drafter.sh --pr ui#8749    # one specific PR, even without a review request
./drafter.sh --force         # redo a PR this tool already drafted
./drafter.sh --limit 5       # max claude reviews per run (default 3)
```

Env: `PR_REVIEW_MODEL` (default `opus`), `PR_REVIEW_MAX_DIFF` (default 6000),
`PR_REVIEW_ORG`, `PR_REVIEW_IGNORE`, `PR_REVIEW_KEEP`, `PR_REVIEW_HOME`.
The GitHub token comes from `GH_TOKEN`/`GITHUB_TOKEN`, else `gh auth token`.

## Files

| File | Purpose |
|---|---|
| `drafter.sh` | The engine: find, review, post pending reviews, notify. |
| `menubar.sh` | SwiftBar renderer + background engine trigger. |
| `state/state.tsv` | Heads already handled (`key`, `head`, `status`). |
| `state/drafts.tsv` | Log of created drafts: ts, key, url, verdict, comments, seconds, diff lines. |
| `state/inflight-<key>` | PID of a running review (written by the menu). |
| `state/phase-<key>` | Live step of a running review (written by the engine). |
| `state/pending.json` | Drafts awaiting your submit (feeds the menu bar). |
| `state/lastrun.json` | Last run summary. |
| `state/engine.log` | Output of SwiftBar-triggered runs. |
| `state/lock/` | Held while a run is in progress. |
| `state/fail-<key>` | Reason of the last failed attempt on a PR (menu row). |
| `state/last-failure.txt` | stderr + raw claude envelope of the last failure. |
| `state/claude-auth` | `claude auth status` result cached by the menu scan. |

## Trust notes

- The reviewer sees only the diff, the PR description, and the style guide.
  It cannot read the rest of the repo. Treat cross-file claims as questions.
- A pending review holds the inline comment slot: while a draft sits
  unsubmitted, the tool cannot draft a fresh one for new commits. The menu
  flags this with "new commits since draft". Submit or discard, and the next
  run drafts against the new head.
- Everything it posts stays private until you press submit.
