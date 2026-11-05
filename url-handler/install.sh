#!/bin/bash
# Build and register the prdigest:// URL handler behind the draft page's
# "Discuss with Claude" button.
#
# Builds output/PRDigestLinks.app, a background AppleScript applet that receives
# prdigest:// links and passes them to `review/view_draft.py --handle-url`, which
# validates the link and opens the Claude session. Re-run after moving the repo.
#
#   url-handler/install.sh            build + register
#   url-handler/install.sh --remove   unregister + delete the app
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
# The app lives with the data; the code it runs is this copy's (an installed release's
# `current` link when run from there).
DATA="${PR_DIGEST_DATA:-$(cat "$REPO/.data_dir" 2>/dev/null || echo "$REPO")}"
APP="$DATA/output/PRDigestLinks.app"
PLIST="$APP/Contents/Info.plist"
LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister

if [ "${1:-}" = "--remove" ]; then
  [ -d "$APP" ] && "$LSREGISTER" -u "$APP" && rm -rf "$APP"
  echo "removed $APP"
  exit 0
fi

mkdir -p "$DATA/output"
SRC="$(mktemp -t prdigest-handler).applescript"
cat > "$SRC" <<EOF
on open location theURL
  do shell script quoted form of "$REPO/.venv/bin/python" & " " & quoted form of "$REPO/review/view_draft.py" & " --handle-url " & quoted form of theURL & " > /dev/null 2>&1 &"
end open location
EOF
rm -rf "$APP"
osacompile -o "$APP" "$SRC"
rm -f "$SRC"

/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier local.prdigest.links" "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string local.prdigest.links" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes array" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0 dict" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0:CFBundleURLName string prdigest" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0:CFBundleURLSchemes array" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0:CFBundleURLSchemes:0 string prdigest" "$PLIST"

"$LSREGISTER" -f "$APP"
echo "registered prdigest:// → $APP"
