#!/usr/bin/env bash
# menubar.sh — the review actions behind the digest menu's "Reviews you owe" rows.
#
# The digest (swiftbar_plugin.py) calls the --review, --cancel and --open-claude modes
# and reads the inflight-/phase-/fail-<key> files they leave in state/. Nothing loads
# this script as a SwiftBar plugin any more; its render section below (run with no
# mode) still prints the standalone review list it was built as.
#
# Standalone list: every open PR that requests your review (your org). Each PR gets:
#   -- View on GitHub          just look, costs nothing
#   -- 🤖 Auto code review     runs drafter.sh --pr for THAT PR only
# Claude never runs unless you click Auto code review, so no tokens burn
# in the background. The scheduled refresh (30 min) and menu-open refresh
# only re-run the cheap GitHub GraphQL scan.
#
# Row states:
#   🤖 Auto code review        not reviewed yet — click to draft
#   🔄 <step> · elapsed         engine is running: live step from drafter.sh,
#                              time since the click, last run's size/duration,
#                              and a Cancel row
#   ⏳ Draft ready             pending review waiting for YOUR submit (click to open)
#   ⚠️ new commits since draft  PR moved after the draft was made
#   ⚠️ last attempt failed      the engine could not draft; reason shown, button stays
#   ✔ reviewed                 you already reviewed this head commit
#
# A banner warns when the claude CLI is logged out (state/claude-auth, written
# by --fetch). Its login is separate from the Claude desktop app.
#
# Modes (internal): --fetch (scan GitHub, rewrite cache), --review <key>
# (run the engine for one PR, then rescan), --cancel <key> (kill that run),
# --mark-seen.
# State: state/menu.json (scan cache), state/fetchstamp, state/inflight-<key>,
# state/newdone (red badge), state/engine.log, state/fail-<key> (last failure
# reason, written by drafter.sh), state/claude-auth (loggedIn true/false).

set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
BASE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$BASE/.." && pwd)"
REVIEWS_DIR="$REPO/state/reviews"   # one <repo>#<n>.json per PR, written by bin/review-pr
ST="$BASE/state"
MENU="$ST/menu.json"; FETCHSTAMP="$ST/fetchstamp"; NEWDONE="$ST/newdone"
STATE_FILE="$ST/state.tsv"; DRAFT_LOG="$ST/drafts.tsv"; LOG="$ST/engine.log"
CONF_DIR="${PR_REVIEW_HOME:-$HOME/.config/pr-review-drafter}"
[ -f "$CONF_DIR/env" ] && . "$CONF_DIR/env"
ORG="${PR_REVIEW_ORG:-}"
PLUGIN_NAME="prdigest.1h.sh"   # the menu that shows review status; redrawn after each action
TAB=$'\t'
mkdir -p "$ST"

github_token() {
  export GH_TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-$(gh auth token 2>/dev/null)}}"
}

refresh_plugin() { open -g "swiftbar://refreshplugin?name=$PLUGIN_NAME&plugin=$PLUGIN_NAME" >/dev/null 2>&1 || true; }
fmt_dur() { # seconds -> 45s / 3m05s / 1h02m
  if [ "$1" -ge 3600 ]; then printf '%dh%02dm' $(($1 / 3600)) $(($1 % 3600 / 60))
  elif [ "$1" -ge 60 ]; then printf '%dm%02ds' $(($1 / 60)) $(($1 % 60))
  else printf '%ds' "$1"; fi
}
kill_tree() { for c in $(pgrep -P "$1" 2>/dev/null); do kill_tree "$c"; done; kill "$1" 2>/dev/null; }

# --- fetch: cheap GraphQL scan, no claude ----------------------------------
do_fetch() {
  github_token
  QUERY='query { search(query: "is:pr is:open org:'"$ORG"' review-requested:@me draft:false", type: ISSUE, first: 30) { nodes { ... on PullRequest {
    number title url headRefOid author { login } repository { name }
    viewerLatestReview { commit { oid } }
    reviews(states: [PENDING], first: 5) { nodes { author { login } } } } } } viewer { login } }'
  RAW="$(gh api graphql -f query="$QUERY" 2>/dev/null)"
  [ -z "$RAW" ] && return 1
  ME="$(printf '%s' "$RAW" | jq -r '.data.viewer.login')"

  TSVF="$(mktemp)"; ROWS="$(mktemp)"
  printf '%s' "$RAW" | jq -r --arg me "$ME" '.data.search.nodes[] | select(. != null) | [
    .repository.name, (.number|tostring), .headRefOid, .author.login,
    (if ([.reviews.nodes[]? | select(.author.login == $me)] | length) > 0 then "1" else "0" end),
    (if (.viewerLatestReview != null and .viewerLatestReview.commit.oid == .headRefOid) then "1" else "0" end),
    .url, (.title | gsub("[\\t|]"; " ")) ] | @tsv' > "$TSVF"

  while IFS="$TAB" read -r repo num head author has_pending reviewed_head url title; do
    [ -z "$repo" ] && continue
    key="${repo}#${num}"
    [ "$has_pending" = "1" ] && rm -f "$ST/fail-$key"   # a draft exists, so the last failure is history
    newc="0"
    if [ "$has_pending" = "1" ] && ! grep -qF "$key${TAB}$head" "$STATE_FILE" 2>/dev/null \
       && grep -q "^${key}${TAB}" "$STATE_FILE" 2>/dev/null; then newc="1"; fi
    dline="$(grep -F "${TAB}$key${TAB}" "$DRAFT_LOG" 2>/dev/null | tail -1)"
    verdict="?"; ncom="-"; took=""
    if [ -n "$dline" ]; then
      verdict="$(printf '%s' "$dline" | cut -f4)"; ncom="$(printf '%s' "$dline" | cut -f5)"
      took="$(printf '%s' "$dline" | cut -f6)"
    fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$key" "$url" "$title" "$author" "$has_pending" "$reviewed_head" "$newc" "$verdict" "$ncom" "$took" "$head" >> "$ROWS"
  done < "$TSVF"

  jq -Rn '[inputs | select(length > 0) | split("\t") |
    {key: .[0], url: .[1], title: .[2], author: .[3],
     has_pending: (.[4] == "1"), reviewed_head: (.[5] == "1"),
     new_commits: (.[6] == "1"), verdict: .[7], ncomments: .[8], took: (.[9] // ""),
     head: (.[10] // "")}]' < "$ROWS" > "$MENU.tmp" \
    && mv "$MENU.tmp" "$MENU"
  claude auth status --json 2>/dev/null | jq -r 'if type == "object" and (.loggedIn | type) == "boolean" then (.loggedIn | tostring) else "unknown" end' > "$ST/claude-auth.tmp" 2>/dev/null \
    && mv "$ST/claude-auth.tmp" "$ST/claude-auth"
  date +%s > "$FETCHSTAMP"
  rm -f "$TSVF" "$ROWS"
  return 0
}

if [ -z "$ORG" ]; then
  echo "🤖 setup"
  echo "---"
  echo "Set PR_REVIEW_ORG in $CONF_DIR/env | color=#d29922"
  echo "See review-drafter/README.md | size=11 color=#8e8e93"
  exit 0
fi

case "${1:-}" in
  --fetch) do_fetch; refresh_plugin; exit 0 ;;
  --review)
    key="${2:-}"; [ -z "$key" ] && exit 0
    touch "$ST/inflight-$key"
    nohup "$0" --run-review "$key" >/dev/null 2>&1 &
    echo $! > "$ST/inflight-$key"   # pid lets --cancel kill the run and the render detect a dead one
    exit 0 ;;
  --run-review)
    # Foreground body of --review: bin/review-pr writes the draft, the state/reviews/
    # index entry, and the notification; a failure leaves its reason in fail-<key>.
    key="${2:-}"; [ -z "$key" ] && exit 0
    err="$ST/err-$key"
    echo "$(date '+%Y-%m-%d %H:%M:%S')  review $key" >> "$LOG"
    if "$REPO/bin/review-pr" "$key" --notify --phase-file "$ST/phase-$key" \
         --refresh-plugin "$PLUGIN_NAME" >> "$LOG" 2> "$err"; then
      rm -f "$ST/fail-$key"
    else
      printf '%s\t%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$(tail -1 "$err")" > "$ST/fail-$key"
    fi
    cat "$err" >> "$LOG"; rm -f "$err"
    rm -f "$ST/inflight-$key"
    touch "$NEWDONE"
    refresh_plugin
    exit 0 ;;
  --open-claude)
    key="${2:-}"; idx="$REVIEWS_DIR/$key.json"; [ -f "$idx" ] || exit 0
    draft="$(jq -r '.story // .draft' "$idx")"; tkey="$(jq -r '.key' "$idx")"
    "$REPO/.venv/bin/python" "$REPO/session.py" \
      --prompt "Let's go over the draft review for $tkey ($key): $draft"
    exit 0 ;;
  --cancel)
    key="${2:-}"; [ -z "$key" ] && exit 0
    pid="$(cat "$ST/inflight-$key" 2>/dev/null)"
    [ -n "$pid" ] && kill_tree "$pid"
    rm -f "$ST/inflight-$key" "$ST/phase-$key"; rmdir "$ST/lock" 2>/dev/null
    printf '%s\t%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "cancelled by you" > "$ST/fail-$key"
    echo "$(date '+%Y-%m-%d %H:%M:%S')  cancelled $key from the menu" >> "$LOG"
    refresh_plugin; exit 0 ;;
  --mark-seen) rm -f "$NEWDONE"; exit 0 ;;
esac

# --- render ------------------------------------------------------------------
reason="${SWIFTBAR_PLUGIN_REFRESH_REASON:-Schedule}"
now="$(date +%s)"

# keep the scan fresh: detached fetch on open, or when older than 20 min.
# Never fetch right after a fetch already refreshed us (fetchstamp guard).
last_fetch="$(cat "$FETCHSTAMP" 2>/dev/null || echo 0)"
age=$((now - last_fetch))
if { [ "$reason" = "MenuOpen" ] && [ "$age" -gt 60 ]; } || [ "$age" -gt 1200 ]; then
  nohup "$0" --fetch >/dev/null 2>&1 &
fi
[ "$reason" = "MenuOpen" ] && rm -f "$NEWDONE"

N=0
if [ -s "$MENU" ] && jq -e . "$MENU" >/dev/null 2>&1; then N="$(jq 'length' "$MENU")"; fi

# drop inflight markers whose run died without cleaning up (crash, reboot)
for f in "$ST"/inflight-*; do
  [ -f "$f" ] || continue
  pid="$(cat "$f" 2>/dev/null)"
  if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then rm -f "$f" "$ST/phase-${f##*/inflight-}"; fi
done
INFLIGHT="$(ls "$ST" 2>/dev/null | grep -c '^inflight-')"
INFLIGHT_EL=0
for f in "$ST"/inflight-*; do
  [ -f "$f" ] || continue
  el=$((now - $(stat -f %m "$f"))); [ "$el" -gt "$INFLIGHT_EL" ] && INFLIGHT_EL=$el
done
# duration of the most recent review, for the estimate under a running row
LAST_HINT=""
last_idx="$(ls -t "$REVIEWS_DIR"/*.json 2>/dev/null | head -1)"
if [ -n "$last_idx" ]; then
  last_s="$(jq -r '.duration_s // empty' "$last_idx" 2>/dev/null)"
  [ -n "$last_s" ] && LAST_HINT=" · last review took $(fmt_dur "$last_s")"
fi
NFAIL="$(ls "$ST" 2>/dev/null | grep -c '^fail-')"
CLAUDE_AUTH="$(cat "$ST/claude-auth" 2>/dev/null || echo unknown)"
CLAUDE_BIN="$(command -v claude || echo claude)"

# menu bar icon
icon="✍️"
[ "$N" -gt 0 ] && icon="✍️ $N"
[ "$INFLIGHT" -gt 0 ] && icon="$icon 🔄 $(fmt_dur "$INFLIGHT_EL")"
[ "$NFAIL" -gt 0 ] || [ "$CLAUDE_AUTH" = "false" ] && icon="$icon ⚠️"
if [ -f "$NEWDONE" ]; then
  echo "$icon | color=#ff3b30"
elif [ "$N" -eq 0 ]; then
  echo "$icon | color=#8e8e93"
else
  echo "$icon"
fi

echo "---"
if [ "$CLAUDE_AUTH" = "false" ]; then
  echo "⚠️ claude CLI is logged out, auto review will fail | color=#d29922"
  echo "-- Log in (opens Terminal, then your browser) | bash=$CLAUDE_BIN param1=auth param2=login terminal=true"
  echo "-- The CLI login is separate from the Claude desktop app | size=11 color=#8e8e93"
  echo "---"
fi
if [ "$N" -gt 0 ]; then
  echo "PRs waiting on your review — pick one | size=12"
  jq -r '.[] | [.key, .url, .title, .author,
                (if .has_pending then "1" else "0" end),
                (if .reviewed_head then "1" else "0" end),
                (if .new_commits then "1" else "0" end),
                .verdict, .ncomments, (.took // ""), (.head // "")] | @tsv' "$MENU" \
  | while IFS="$TAB" read -r key url title author has_pending reviewed_head newc verdict ncom took head; do
      short="$title"; [ ${#short} -gt 58 ] && short="${short:0:57}…"
      [ "$has_pending" = "1" ] && rm -f "$ST/fail-$key"
      fail=""
      [ -f "$ST/fail-$key" ] && fail="$(cut -f2 "$ST/fail-$key" | tr '|' '/' | head -c 120)"
      # local review draft from bin/review-pr: current when it was written against this head
      rv_sev=""; rv_sum=""; rv_current=0
      if [ -f "$REVIEWS_DIR/$key.json" ]; then
        rv_head="$(jq -r '.head' "$REVIEWS_DIR/$key.json")"
        rv_sev="$(jq -r '.severity' "$REVIEWS_DIR/$key.json")"
        rv_sum="$(jq -r '[.counts | to_entries[] | select(.value > 0) | "\(.value) \(.key)"] | join(", ")' "$REVIEWS_DIR/$key.json")"
        case "$head" in "$rv_head"*) rv_current=1 ;; esac
      fi
      state=""
      [ -n "$fail" ] && state=" · ⚠️ last attempt failed"
      [ "$rv_current" = "1" ] && state=" · 📝 review ready ($rv_sev)"
      [ -n "$rv_sev" ] && [ "$rv_current" = "0" ] && state=" · 📝 review is for an older commit"
      [ "$has_pending" = "1" ] && state=" · ⏳ draft ready"
      if [ -f "$ST/inflight-$key" ]; then
        run_started="$(stat -f %m "$ST/inflight-$key" 2>/dev/null || echo "$now")"
        run_el=$((now - run_started))
        state=" · 🔄 reviewing $(fmt_dur "$run_el")"
      fi
      [ "$reviewed_head" = "1" ] && state=" · ✔ reviewed"
      echo "$key$state"
      echo "$short — $author | size=11 color=#8e8e93"
      echo "-- View on GitHub | href=$url"
      if [ -f "$ST/inflight-$key" ]; then
        step="working"; step_ts="$run_started"
        if [ -f "$ST/phase-$key" ]; then
          step_ts="$(cut -f1 "$ST/phase-$key")"; step="$(cut -f2 "$ST/phase-$key" | tr '|' '/')"
        fi
        echo "-- 🔄 $step · $(fmt_dur $((now - step_ts))) | color=#d29922"
        echo "-- started $(fmt_dur "$run_el") ago$LAST_HINT | size=11 color=#8e8e93"
        echo "-- ✖ Cancel this review | bash=$BASE/menubar.sh param1=--cancel param2=$key terminal=false refresh=true"
      elif [ "$has_pending" = "1" ]; then
        took_txt=""; [ -n "$took" ] && took_txt=", took $(fmt_dur "$took")"
        echo "-- ⏳ Draft ready (suggests $verdict, $ncom comments$took_txt) — open & submit | href=$url"
        [ "$newc" = "1" ] && echo "-- ⚠️ new commits since the draft — submit or discard it first | color=#d29922"
      elif [ "$reviewed_head" = "1" ]; then
        echo "-- ✔ you already reviewed this commit | color=#8e8e93"
      else
        [ -n "$fail" ] && echo "-- ⚠️ last attempt failed: $fail | color=#d29922"
        if [ -n "$rv_sev" ]; then
          [ "$rv_current" = "0" ] && echo "-- ⚠️ new commits since this review | color=#d29922"
          echo "-- 📝 $rv_sev${rv_sum:+: $rv_sum} | size=12"
          echo "-- 💬 Open in Claude | bash=$BASE/menubar.sh param1=--open-claude param2=$key terminal=false"
          echo "-- 📄 Open draft | bash=/usr/bin/open param1=-t param2=$(jq -r '.story // .draft' "$REVIEWS_DIR/$key.json") terminal=false"
        fi
        label="🤖 Review with pr-reviewer (uses claude)"
        [ -n "$rv_sev" ] && label="🔁 Re-run the review"
        echo "-- $label | bash=$BASE/menubar.sh param1=--review param2=$key terminal=false refresh=true"
      fi
    done
else
  echo "No PRs waiting on your review. | color=#8e8e93"
fi

echo "---"
echo "Refresh list | bash=$BASE/menubar.sh param1=--fetch terminal=false"
if [ "$last_fetch" -gt 0 ]; then
  echo "Scanned $((age / 60)) min ago · reviews only run when you click | size=11 color=#8e8e93"
fi
echo "Open engine log | bash=/usr/bin/open param1=$LOG terminal=false"
exit 0
