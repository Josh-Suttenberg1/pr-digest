"""Keep every test away from the live machine's notification state: popups record to a
temporary file, and the alert app counts as not installed whatever is in ~/Applications."""

import tempfile
from pathlib import Path

import notify
from watcher import popups

_TMP = Path(tempfile.mkdtemp(prefix="pr-digest-tests-"))
popups.PATH = _TMP / "popups.json"
notify.ALERT_APP = _TMP / "no-alert-app.app"

from watcher import fyi  # noqa: E402

fyi.PATH = _TMP / "fyi.json"
