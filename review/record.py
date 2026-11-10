"""The one record per PR of what was reviewed: state/reviews/<repo>#<n>.json.

bin/review-pr writes it with every draft (review_pr.write_drafts), next to the draft's
`.json` sidecar, which holds the same record. An update review (review/update_review.py)
and a carried-forward draft (review/auto_draft.py, when new commits change nothing in
the PR's own diff) rewrite it. The menu reads it for the draft's state and cost.

Fields beyond the draft's metadata (key, repo, number, draft, story, severity, counts,
cost_usd, generated_at, run_dir, ...):

  head      the head commit the draft is current for: the reviewed head, or a later
            head whose changes an update review covered or that changed nothing in the
            PR's own diff.
  findings  every finding the draft has raised, in the draft's numbering:
            {id ("f<n>", or "u<sha7>-<n>" for one an update review raised), n,
             severity (blocking | advisory | nit | decision), path, line (int or
             None), summary, status, status_note, status_head}
            status is "open" until an update review judges it: "addressed",
            "partly", "not" or "no_longer_relevant". status_head is the head it was
            judged at.
  pr_diff   the PR's own diff at `head`: {head, merge_base, fingerprint (sha), files
            ({path: sha} over each file's added and removed lines)}, from
            review/interdiff.py. Absent until the scheduler has computed it.
  history   [{kind: full | update | carried, from, to, at, cost_usd, duration_s,
              summary}], oldest first.
  comment_verdicts
            the latest verdict on each review comment an update review judged:
            {id (the thread's first comment), author, path, line, url, head, verdict
             (addressed | partly | not_addressed), mechanical, evidence, note}, in
            watcher/followup.py's verdict shape.

The follow-up jobs and the automatic re-approval (watcher/followup.py) keep their own
per-comment verdicts today; they can read `findings` and `head` here to agree with the
draft about what is still open.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import settings  # noqa: E402

INDEX_DIR = settings.STATE_DIR / "reviews"

SEVERITIES = {"🔴": "blocking", "🟡": "advisory", "🔵": "nit", "⚠️": "decision"}
MARKS = {v: k for k, v in SEVERITIES.items()}
OPEN = ("open", "partly", "not")
STATUSES = ("addressed", "partly", "not", "no_longer_relevant")

FINDING_LINE_RE = re.compile(
    r"^\s*(?P<n>\d+)\.\s*(?P<mark>🔴|🟡|🔵|⚠️)\s*(?:`(?P<where>[^`]+)`)?\s*[—:–-]?\s*(?P<text>.*)$",
    re.M)
UPDATE_START, UPDATE_END = "<!-- update:start -->", "<!-- update:end -->"
UPDATE_BLOCK_RE = re.compile(re.escape(UPDATE_START) + r".*?" + re.escape(UPDATE_END) + r"\n*",
                             re.S)


def key_of(repo: str, number: int) -> str:
    return f"{repo}#{number}"


def path_of(repo: str, number: int, index_dir: Path | None = None) -> Path:
    return (index_dir or INDEX_DIR) / f"{key_of(repo, number)}.json"


def load(repo: str, number: int, index_dir: Path | None = None) -> dict | None:
    try:
        return json.loads(path_of(repo, number, index_dir).read_text())
    except (OSError, ValueError):
        return None


def save(rec: dict, index_dir: Path | None = None) -> None:
    """Write the record to the index and to the draft's sidecar."""
    text = json.dumps(rec, indent=2) + "\n"
    target = path_of(rec["repo"], rec["number"], index_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(tmp, target)
    if rec.get("draft"):
        Path(rec["draft"]).with_suffix(".json").write_text(text)


def without_updates(text: str) -> str:
    """A draft's text without the update sections at its top."""
    return UPDATE_BLOCK_RE.sub("", text)


def _where(where: str | None) -> tuple[str | None, int | None]:
    if not where:
        return None, None
    m = re.match(r"^(?P<path>[^\s:]+?)(?::(?P<line>\d+)(?:[-–]\d+)?)?$", where.strip())
    if not m:
        return None, None
    path = m["path"]
    if "/" not in path and "." not in path:
        return None, None
    return path, int(m["line"]) if m["line"] else None


def parse_findings(text: str, prefix: str = "f") -> list[dict]:
    """The numbered findings in a pr-reviewer report: `1. 🔴 \\`path:line\\` — claim`."""
    out = []
    for m in FINDING_LINE_RE.finditer(without_updates(text)):
        path, line = _where(m["where"])
        summary = re.sub(r"\s+", " ", m["text"]).strip()
        out.append({"id": f"{prefix}{m['n']}", "n": int(m["n"]),
                    "severity": SEVERITIES[m["mark"]], "path": path, "line": line,
                    "summary": summary[:240], "status": "open", "status_note": "",
                    "status_head": None})
    return out


def counts(findings: list[dict]) -> dict[str, int]:
    """Open findings by severity, in the draft sidecar's `counts` shape."""
    out = {name: 0 for name in SEVERITIES.values()}
    for f in findings:
        if f.get("status", "open") in OPEN:
            out[f["severity"]] += 1
    return out


def severity(c: dict[str, int]) -> str:
    if c["blocking"] or c["advisory"] or c["decision"]:
        return "substantive"
    return "nits-only" if c["nit"] else "clean"


def covers(rec: dict | None, head: str) -> bool:
    """True when the record's draft is current for `head` (a prefix of either counts)."""
    reviewed, live = ((rec or {}).get("head") or "").lower(), (head or "").lower()
    return bool(reviewed and live) and (live.startswith(reviewed) or reviewed.startswith(live))
