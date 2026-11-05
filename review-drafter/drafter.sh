#!/usr/bin/env bash
# drafter.sh — the pr-review-drafter engine.
#
# Body-double code reviewer. For every open PR that requests your review, it
# runs an automatic style-guide review and saves it on GitHub as a PENDING
# review under YOUR account. Pending reviews are visible only to you: open the
# PR, edit or delete any comment, pick Approve / Comment / Request changes,
# and submit. Nothing reaches the author until you do.
#
# Flow per PR (your org, review-requested:@me, not draft):
#   - skip if you already have a pending draft on it (GitHub allows only one
#     pending review per user), or you already reviewed this head commit,
#     or this tool already drafted for this head commit (state/state.tsv)
#   - fetch the diff, strip noise (PR_REVIEW_IGNORE, default generated/)
#   - run `claude -p` with the matching style guide from the config dir:
#       guides/<repo>.md, else guides/default.md
#   - validate every comment anchor against the real diff hunks; comments that
#     don't land on a diff line go into the review body instead
#   - POST the review with NO event field => PENDING draft
#   - macOS notification on each new draft
#
# The suggested verdict is embedded as an HTML comment on the first line of
# the review body: invisible when rendered, visible in the submit dialog.
#
# State (all under state/):
#   state.tsv     key<TAB>head<TAB>status — heads this tool already handled
#   drafts.tsv    log of created drafts: ts key url verdict ncomments took_s difflines
#   phase-<key>   epoch<TAB>text of the step in progress; menubar.sh shows it
#                 live under the row and removes it when the review ends
#   pending.json  drafts currently awaiting your submit (menubar.sh renders it)
#   lastrun.json  summary of the last full run
#   lock/         held while a run is in progress
#   engine.log    output of SwiftBar-triggered runs
#   fail-<key>    ts<TAB>reason of the last failed attempt; menubar.sh renders
#                 it under the PR row; cleared when a draft is created
#   last-failure.txt  stderr + raw claude envelope of the last failure
#
# Prerequisite: the claude CLI must be logged in (`claude auth status`). Its
# login is separate from the Claude desktop app; when it lapses every review
# fails with "OAuth session expired". Fix: `claude auth login`.
#
# Usage:
#   drafter.sh                   # normal tick (SwiftBar or /loop triggers this)
#   drafter.sh --dry-run         # generate reviews, print them, post nothing
#   drafter.sh --pr ui#8749      # one specific PR (ignores the request filter)
#   drafter.sh --force           # ignore the already-drafted state stamp
#   drafter.sh --limit N         # max claude reviews this run (default 3)
#
# Env overrides: PR_REVIEW_MODEL (default opus), PR_REVIEW_MAX_DIFF (default 6000 lines)
# Written for macOS stock bash 3.2. Exit code 0 always.

set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"   # SwiftBar spawns with a bare env

DRY_RUN=0; FORCE=0; ONLY_PR=""; LIMIT=3
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --force)   FORCE=1 ;;
    --pr)      shift; ONLY_PR="${1:-}" ;;
    --limit)   shift; LIMIT="${1:-3}" ;;
  esac
  shift
done

# --- config ---------------------------------------------------------------
# Config lives outside the repo: $CONF_DIR/env (exports), guides/<repo>.md or
# guides/default.md, and an optional extra-rules.md appended to the prompt.
CONF_DIR="${PR_REVIEW_HOME:-$HOME/.config/pr-review-drafter}"
[ -f "$CONF_DIR/env" ] && . "$CONF_DIR/env"
ORG="${PR_REVIEW_ORG:-}"
GUIDES_DIR="$CONF_DIR/guides"
EXTRA_RULES="$CONF_DIR/extra-rules.md"
IGNORE_RE="${PR_REVIEW_IGNORE:-(^|/)generated/}"   # diff paths to drop
KEEP_RE="${PR_REVIEW_KEEP:-}"                      # exceptions to the ignore rule
if [ -z "$ORG" ]; then
  echo "Set PR_REVIEW_ORG (GitHub org to scan), e.g. in $CONF_DIR/env. See README." >&2
  exit 1
fi
BASE="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="$BASE/state"
STATE_FILE="$STATE_DIR/state.tsv"
DRAFT_LOG="$STATE_DIR/drafts.tsv"
PENDING_JSON="$STATE_DIR/pending.json"
LASTRUN_JSON="$STATE_DIR/lastrun.json"
LOCK="$STATE_DIR/lock"
PLUGIN_NAME="prreviews.30m.sh"
MODEL="${PR_REVIEW_MODEL:-opus}"
MAX_DIFF_LINES="${PR_REVIEW_MAX_DIFF:-6000}"
MAX_COMMENTS=20
TAB=$'\t'

mkdir -p "$STATE_DIR"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "another run holds the lock ($LOCK) — exiting."
  exit 0
fi
SCRATCH="$(mktemp -d)"
trap 'rmdir "$LOCK" 2>/dev/null; rm -rf "$SCRATCH"' EXIT

# --- auth: GH_TOKEN / GITHUB_TOKEN, else the gh CLI login -----------------
export GH_TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-$(gh auth token 2>/dev/null)}}"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
notify() { # title message url  (with a sound: a 10 minute review needs a nudge)
  afplay /System/Library/Sounds/Glass.aiff >/dev/null 2>&1 &
  if command -v terminal-notifier >/dev/null 2>&1; then
    terminal-notifier -title "$1" -message "$2" -open "${3:-}" >/dev/null 2>&1 || true
  else
    osascript -e "display notification \"$2\" with title \"$1\"" >/dev/null 2>&1 || true
  fi
}
already_done() { grep -qF "$1${TAB}$2" "$STATE_FILE" 2>/dev/null; }
stamp() { printf '%s\t%s\t%s\n' "$1" "$2" "$3" >> "$STATE_FILE"; }

refresh_plugin() { open -g "swiftbar://refreshplugin?name=$PLUGIN_NAME" >/dev/null 2>&1 || true; }
phase() { # key text -> live progress line under the PR row
  printf '%s\t%s\n' "$(date +%s)" "$2" > "$STATE_DIR/phase-$1"
  refresh_plugin
}
# per-PR outcome marker the menu bar renders under the row: state/fail-<key>
mark_row()  { printf '%s\t%s\n' "$(ts)" "$2" > "$STATE_DIR/fail-$1"; }
clear_row() { rm -f "$STATE_DIR/fail-$1"; }
record_failure() { # key reason url  -> log line, row marker, notification
  echo "     ⚠️  $2"
  mark_row "$1" "$2"
  notify "Auto review failed: $1" "$2" "${3:-}"
  errors=$((errors+1))
}
save_debug() { # key stderr-file envelope
  { echo "== $(ts)  $1"; echo "-- stderr"; cat "$2" 2>/dev/null; echo "-- envelope"; printf '%s\n' "$3"; } \
    > "$STATE_DIR/last-failure.txt"
}
CLAUDE_READY=""
claude_ready() { # checked once per run; a logged-out CLI fails every review the same way
  if [ -z "$CLAUDE_READY" ]; then
    CLAUDE_READY="$(claude auth status --json 2>/dev/null | jq -r 'if type == "object" and (.loggedIn | type) == "boolean" then (.loggedIn | tostring) else "unknown" end' 2>/dev/null)"
    [ -z "$CLAUDE_READY" ] && CLAUDE_READY="unknown"
  fi
  [ "$CLAUDE_READY" != "false" ]
}
guide_for() {
  for g in "$GUIDES_DIR/$1.md" "$GUIDES_DIR/default.md"; do
    [ -f "$g" ] && { echo "$g"; return; }
  done
  echo ""
}

touch "$STATE_FILE"
echo "=== PR review drafter  $(ts)$([ $DRY_RUN = 1 ] && echo ' (dry-run)') ==="

# --- fetch candidate PRs ---------------------------------------------------
PR_FIELDS='number title url headRefOid author { login } repository { name }
           viewerLatestReview { commit { oid } }
           reviews(states: [PENDING], first: 5) { nodes { author { login } } }'
if [ -n "$ONLY_PR" ]; then
  repo="${ONLY_PR%%#*}"; num="${ONLY_PR##*#}"
  QUERY='query { repository(owner: "'"$ORG"'", name: "'"$repo"'") { pullRequest(number: '"$num"') { '"$PR_FIELDS"' } } viewer { login } }'
  JQ_NODES='.data.repository.pullRequest'
else
  QUERY='query { search(query: "is:pr is:open org:'"$ORG"' review-requested:@me draft:false", type: ISSUE, first: 30) { nodes { ... on PullRequest { '"$PR_FIELDS"' } } } viewer { login } }'
  JQ_NODES='.data.search.nodes[]'
fi

RAW="$(gh api graphql -f query="$QUERY" 2>/dev/null)"
if [ -z "$RAW" ]; then echo "  ERROR: could not reach GitHub. Skipping this run."; exit 0; fi
ME="$(printf '%s' "$RAW" | jq -r '.data.viewer.login')"

PR_TSV="$SCRATCH/prs.tsv"
printf '%s' "$RAW" | jq -r --arg me "$ME" "$JQ_NODES"' | select(. != null) | [
  .repository.name, (.number|tostring), .headRefOid, .author.login,
  (if ([.reviews.nodes[]? | select(.author.login == $me)] | length) > 0 then "1" else "0" end),
  (if (.viewerLatestReview != null and .viewerLatestReview.commit.oid == .headRefOid) then "1" else "0" end),
  .url, (.title | gsub("[\\t|]"; " ")) ] | @tsv' > "$PR_TSV"

total=0; drafted=0; awaiting=0; skipped=0; errors=0
AWAIT_TSV="$SCRATCH/await.tsv"; : > "$AWAIT_TSV"   # key url title author new_commits verdict ncomments

# lookup the last draft we logged for a key -> "verdict<TAB>ncomments"
draft_info() {
  line="$(grep -F "${TAB}$1${TAB}" "$DRAFT_LOG" 2>/dev/null | tail -1)"
  if [ -n "$line" ]; then
    printf '%s\t%s' "$(printf '%s' "$line" | cut -f4)" "$(printf '%s' "$line" | cut -f5)"
  else
    printf '?\t-'
  fi
}

# --- review one PR ----------------------------------------------------------
review_pr() { # repo num head author url title -- clears the live phase however the steps end
  review_pr_steps "$@"
  rm -f "$STATE_DIR/phase-$1#$2"
  refresh_plugin
}

review_pr_steps() { # repo num head author url title
  repo="$1"; num="$2"; head="$3"; author="$4"; url="$5"; title="$6"
  key="${repo}#${num}"

  if ! claude_ready; then
    record_failure "$key" "claude CLI is logged out. Run: claude auth login" "$url"; return
  fi

  phase "$key" "fetching the diff"
  DIFF="$SCRATCH/$repo-$num.diff"
  gh pr diff "$num" --repo "$ORG/$repo" > "$DIFF.raw" 2>/dev/null
  if [ ! -s "$DIFF.raw" ]; then record_failure "$key" "empty diff or GitHub fetch failed" "$url"; return; fi

  # strip noise files: paths matching IGNORE_RE unless they match KEEP_RE
  awk -v ign="$IGNORE_RE" -v keep="$KEEP_RE" '
    /^diff --git /{
      p=$0; sub(/^diff --git a\//,"",p); sub(/ b\/.*/,"",p)
      skip = (p ~ ign) && !(keep != "" && p ~ keep)
    }
    !skip { print }' "$DIFF.raw" > "$DIFF"

  lines=$(wc -l < "$DIFF" | tr -d ' ')
  phase "$key" "diff is $lines lines, building the prompt"
  if [ "$lines" -gt "$MAX_DIFF_LINES" ]; then
    echo "     ⚠️  diff too big ($lines lines > $MAX_DIFF_LINES) — review this one yourself."
    [ $DRY_RUN = 0 ] && stamp "$key" "$head" "skipped-too-big"
    [ $DRY_RUN = 0 ] && mark_row "$key" "diff too big ($lines lines > $MAX_DIFF_LINES), review it yourself"
    notify "PR too big for auto-review" "$key — review it yourself" "$url"
    skipped=$((skipped+1)); return
  fi

  # commentable anchors: any line that appears in a hunk
  VALID="$SCRATCH/$repo-$num.valid"
  awk '
    /^diff --git /{ file=""; inhunk=0 }
    /^\+\+\+ /{ if ($0 ~ /dev\/null/) { file="" } else { file=$0; sub(/^\+\+\+ [ab]\//,"",file) }; next }
    /^@@/{
      if (file=="") next
      s=$0; match(s,/-[0-9]+/); o=substr(s,RSTART+1,RLENGTH-1)+0
      match(s,/\+[0-9]+/);      n=substr(s,RSTART+1,RLENGTH-1)+0
      inhunk=1; next
    }
    inhunk && file!="" {
      c=substr($0,1,1)
      if      (c=="+")  { print file "|RIGHT|" n; n++ }
      else if (c=="-")  { print file "|LEFT|"  o; o++ }
      else if (c==" ")  { print file "|RIGHT|" n; print file "|LEFT|" o; n++; o++ }
      else if (c!="\\") { inhunk=0 }
    }' "$DIFF" | jq -Rn '[inputs] | map({(.): true}) | add // {}' > "$VALID"

  guide="$(guide_for "$repo")"
  body_text="$(gh pr view "$num" --repo "$ORG/$repo" --json body -q .body 2>/dev/null | head -c 4000)"

  PROMPT="$SCRATCH/$repo-$num.prompt"
  {
    cat <<'EOP'
You are drafting a GitHub pull request review on behalf of the reviewer. The review is saved as PENDING: the reviewer edits and submits it themselves. Write comments they could send with one click.

Comment rules:
- Simple english. Short sentences. Plain words. No filler, no praise padding, no hedging.
- Shape: one sentence for the claim, one for the consequence, then the fix. Use a ```suggestion fenced block when the fix is a small exact in-place replacement of the commented line(s); otherwise describe the fix in one sentence.
- The first sentence names the concrete wrong outcome ("two radios end up checked", "the dropdown spins forever"). No metaphors standing in for the bad state.
- Before emitting a suggestion block, read the line it produces as if it were already in the file and check every claim in it against the diff. A suggestion that rewrites a comment must be true of the code after this PR, not before it.
- Max 4 sentences per comment. One comment per issue. If the same issue repeats, comment once and name the other spots.
- Severity: prefix every style-guide item with "nit: ". Leave the prefix off only for a defect the author must fix before merge (wrong behavior, a broken build). The guide's own etiquette is default approve.
How to review. The style guide is law, not advice:
- Walk the guide rule by rule and scan the whole diff for violations of each rule. Do not skip a violation because it looks small or the fix feels pedantic.
- Flag every violation. When one rule is broken in several places, write one comment on the first site and name the other sites in it.
- Rules the guide marks strongest ([consensus], [durable], or "the five things" / "the spine") are never skipped. They are still "nit: " unless the code is wrong. Severity comes from the consequence, not from where the rule sits in the guide.
- Also flag real defects (bugs, broken logic, missed edge cases) even when no rule names them.
- No opinions beyond the guide. Return zero comments only when the rule-by-rule pass truly finds nothing.

Before you write the output, do a second pass over the diff against the most-missed rules and add anything you skipped:
- Sibling branches: for each case handled in several variants (success and error, one branch per tab or per picker), compare the branches. One that differs from its siblings with no comment saying why is a finding. Say what the odd branch leaves behind (a loading flag never cleared, a field the others reset).
- Stale responses: for each fetch result handler, ask what identifies the request it answers. If the handler writes to "whatever is selected now", a late response from the previous selection lands on the wrong target. Flag the race and name the writer.
- Repeated work: the same value unwrapped or the same list traversed more than once where one pass would do.

Anchoring rules:
- Comment ONLY on lines that appear in the diff below.
- For added or context lines use the NEW file line number with side "RIGHT".
- For deleted lines use the OLD file line number with side "LEFT".
- ```suggestion blocks only on RIGHT-side comments.

The diff is the complete source of truth. You have no repository access. Do not guess about code you cannot see; if something depends on unseen code, phrase it as a question.

Output ONLY a JSON object. No prose. No code fences around the JSON.
{"verdict":"approve"|"comment"|"request_changes","summary":"1 to 3 short sentences for the review body","comments":[{"path":"src/File.ts","line":123,"side":"RIGHT","body":"..."}]}
verdict is a suggestion for the reviewer: "request_changes" only when a real defect is present (wrong behavior, a broken build), "approve" when there is nothing or only nits, "comment" when a non-nit question needs an answer before approving. When approving with nothing to flag, summary should be a short LGTM note.
EOP
    if [ -n "$guide" ]; then
      echo ""
      echo "=== STYLE GUIDE (enforce this) ==="
      cat "$guide"
    fi
    if [ -f "$EXTRA_RULES" ]; then
      echo ""
      echo "=== EXTRA RULES FROM THE REVIEWER ==="
      cat "$EXTRA_RULES"
    fi
    echo ""
    echo "=== PULL REQUEST ==="
    echo "Repo: $ORG/$repo   PR #$num"
    echo "Title: $title"
    echo "Author: $author"
    echo "Description:"
    printf '%s\n' "$body_text"
    echo ""
    echo "=== DIFF ==="
    cat "$DIFF"
  } > "$PROMPT"

  echo "     🤖 reviewing ($lines diff lines, model $MODEL)..."
  CLAUDE_ERR="$SCRATCH/$repo-$num.claude.err"
  started="$(date +%s)"
  phase "$key" "claude ($MODEL) is reviewing $lines lines"
  # re-render the menu every 30s so the elapsed time keeps moving while claude works
  ( while :; do sleep 30; refresh_plugin; done ) >/dev/null 2>&1 &
  ticker=$!
  ENVELOPE="$(cd "$SCRATCH" && claude -p --model "$MODEL" --output-format json < "$PROMPT" 2>"$CLAUDE_ERR")"
  claude_rc=$?
  kill "$ticker" 2>/dev/null; wait "$ticker" 2>/dev/null
  took=$(( $(date +%s) - started ))
  phase "$key" "claude finished after $((took / 60))m$((took % 60))s, checking comment anchors"
  # the envelope reports failures (auth, rate limit, ...) as is_error=true with the
  # message in .result; treating that string as a review produced a misleading
  # "could not parse review JSON" for what was really a logged-out CLI
  is_error="$(printf '%s' "$ENVELOPE" | jq -r 'if type == "object" then (.is_error // false) else false end' 2>/dev/null)"
  result="$(printf '%s' "$ENVELOPE" | jq -r 'if type == "object" then (.result // empty) else empty end' 2>/dev/null)"
  if [ "$is_error" = "true" ] || [ -z "$result" ]; then
    reason="$result"
    [ -z "$reason" ] && reason="$(head -c 300 "$CLAUDE_ERR" 2>/dev/null | tr '\n' ' ')"
    [ -z "$reason" ] && reason="claude exited $claude_rc with no output"
    save_debug "$key" "$CLAUDE_ERR" "$ENVELOPE"
    record_failure "$key" "claude failed: $(printf '%s' "$reason" | head -c 200)" "$url"
    return
  fi

  # strip accidental code fences, then parse
  REVIEW="$SCRATCH/$repo-$num.review.json"
  printf '%s' "$result" | sed -e 's/^```json//' -e 's/^```//' -e 's/```$//' \
    | jq 'if type == "object" then . else empty end' > "$REVIEW" 2>/dev/null
  if [ ! -s "$REVIEW" ]; then
    save_debug "$key" "$CLAUDE_ERR" "$ENVELOPE"
    record_failure "$key" "review output was not JSON (see state/last-failure.txt)" "$url"
    return
  fi

  verdict="$(jq -r '.verdict // "comment"' "$REVIEW")"
  summary="$(jq -r '.summary // ""' "$REVIEW")"

  # keep only comments that anchor to a real diff line; the rest go in the body
  jq --slurpfile v "$VALID" --argjson max "$MAX_COMMENTS" '
    (.comments // []) as $all
    | [$all[] | select(.path and .line and .side and .body)] as $shaped
    | [ $shaped[] | select($v[0]["\(.path)|\(.side)|\(.line)"] == true) | {path, line, side, body} ] as $ok
    | { anchored: $ok[0:$max],
        dropped:  ([ $shaped[] | select($v[0]["\(.path)|\(.side)|\(.line)"] != true) ] + $ok[$max:]) }' \
    "$REVIEW" > "$REVIEW.split"

  n_anchored="$(jq '.anchored | length' "$REVIEW.split")"
  n_dropped="$(jq '.dropped | length' "$REVIEW.split")"

  BODY="$SCRATCH/$repo-$num.body"
  {
    verdict_uc="$(printf '%s' "$verdict" | tr '[:lower:]_' '[:upper:] ')"
    echo "<!-- pr-review-drafter suggests: $verdict_uc ($n_anchored inline comments). Draft review: edit or delete anything, then submit. This line never renders. -->"
    printf '%s\n' "$summary"
    if [ "$n_dropped" -gt 0 ]; then
      echo ""
      echo "Notes that did not anchor to a diff line:"
      jq -r '.dropped[] | "- `\(.path):\(.line)` \(.body)"' "$REVIEW.split"
    fi
  } > "$BODY"

  if [ $DRY_RUN = 1 ]; then
    echo "     --- dry-run: would create pending review (suggests $verdict, $n_anchored comments, $n_dropped unanchored)"
    sed 's/^/     | /' "$BODY"
    jq -r '.anchored[] | "     >> \(.path):\(.line) [\(.side)]\n\(.body | split("\n") | map("     |  " + .) | join("\n"))"' "$REVIEW.split"
    return
  fi

  phase "$key" "posting the pending review to GitHub"
  PAYLOAD="$SCRATCH/$repo-$num.payload.json"
  jq -n --arg commit "$head" --rawfile body "$BODY" --slurpfile s "$REVIEW.split" \
    '{commit_id: $commit, body: $body, comments: $s[0].anchored}' > "$PAYLOAD"

  APIOUT="$SCRATCH/$repo-$num.apiout"
  if gh api "repos/$ORG/$repo/pulls/$num/reviews" --method POST --input "$PAYLOAD" > "$APIOUT" 2>&1; then
    echo "     🟢 pending review created — suggests $verdict, $n_anchored inline comment(s), took $((took / 60))m$((took % 60))s."
    stamp "$key" "$head" "drafted"
    clear_row "$key"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$(ts)" "$key" "$url" "$verdict" "$n_anchored" "$took" "$lines" >> "$DRAFT_LOG"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$key" "$url" "$title" "$author" "0" "$verdict" "$n_anchored" >> "$AWAIT_TSV"
    notify "Draft review ready: $key" "suggests $verdict, $n_anchored comment(s) — open and submit" "$url"
    drafted=$((drafted+1)); awaiting=$((awaiting+1))
  else
    echo "     ⚠️  GitHub rejected the review payload:"
    sed 's/^/         /' "$APIOUT" | head -5
    record_failure "$key" "GitHub rejected the review: $(head -1 "$APIOUT" | head -c 160)" "$url"
  fi
}

# --- main loop ---------------------------------------------------------------
while IFS=$'\t' read -r repo num head author has_pending reviewed_head url title; do
  [ -z "$repo" ] && continue
  total=$((total+1)); key="${repo}#${num}"
  echo ""
  echo "👀 $key  by $author — $title"

  if [ "$has_pending" = "1" ]; then
    newc="0"
    if ! already_done "$key" "$head" && grep -q "^${key}${TAB}" "$STATE_FILE" 2>/dev/null; then
      newc="1"
    fi
    info="$(draft_info "$key")"
    v="$(printf '%s' "$info" | cut -f1)"; n="$(printf '%s' "$info" | cut -f2)"
    echo "     ⏳ draft awaiting YOUR submit (suggests $v)$([ "$newc" = "1" ] && echo '  ⚠ new commits since the draft')"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$key" "$url" "$title" "$author" "$newc" "$v" "$n" >> "$AWAIT_TSV"
    awaiting=$((awaiting+1)); continue
  fi
  if [ "$reviewed_head" = "1" ]; then
    echo "     ✔ you already reviewed this head commit."; skipped=$((skipped+1)); continue
  fi
  if [ $FORCE = 0 ] && already_done "$key" "$head"; then
    echo "     ✔ already drafted for this head (submitted or dismissed since)."; skipped=$((skipped+1)); continue
  fi
  if [ "$drafted" -ge "$LIMIT" ]; then
    echo "     ⏭ per-run limit ($LIMIT) reached — next run."; skipped=$((skipped+1)); continue
  fi

  review_pr "$repo" "$num" "$head" "$author" "$url" "$title"
done < "$PR_TSV"

# --- publish state for the menu bar (full scans only, never --pr slices) ------
if [ -z "$ONLY_PR" ]; then
  NOW="$(date +%s)"
  PREV='[]'
  if [ -s "$PENDING_JSON" ] && jq -e . "$PENDING_JSON" >/dev/null 2>&1; then PREV="$(cat "$PENDING_JSON")"; fi
  jq -Rn --argjson prev "$PREV" --argjson now "$NOW" '
    [inputs | select(length > 0) | split("\t")
     | {key: .[0], url: .[1], title: .[2], author: .[3],
        new_commits: (.[4] == "1"), verdict: .[5], ncomments: .[6]}]
    | map(. as $e | $e + {first_seen: (([ $prev[] | select(.key == $e.key) | .first_seen ] | first) // $now)})
  ' < "$AWAIT_TSV" > "$PENDING_JSON.tmp" && mv "$PENDING_JSON.tmp" "$PENDING_JSON"

  jq -n --arg ts "$(ts)" --argjson total "$total" --argjson drafted "$drafted" \
        --argjson awaiting "$awaiting" --argjson skipped "$skipped" --argjson errors "$errors" \
        '{ts: $ts, total: $total, drafted: $drafted, awaiting: $awaiting, skipped: $skipped, errors: $errors}' \
        > "$LASTRUN_JSON"

  refresh_plugin
fi

# --- summary -----------------------------------------------------------------
echo ""
echo "--- summary: $total PR(s) requesting review | $drafted drafted | $awaiting awaiting your submit | $skipped skipped | $errors error(s)"
echo "==========================================================="
exit 0
