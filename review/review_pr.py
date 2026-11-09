"""Run the PR reviewer and its verifier headless, and save the draft(s).

    bin/review-pr <ticket key | PR URL | repo#N> [--notify] [--phase-file P]
                  [--reviewer-output REPORT.md]

This is the one entry point for every review: the menu bar button, /reviews,
and interactive "review <ticket key>" sessions all call it. Steps:

1. Check that the claude CLI is logged in.
2. claude -p --agent <review.reviewer_agent>, from the workspace so its CLAUDE.md
   and .claude/agents/ load.
   With --reviewer-output, an interactive session already wrote that report by
   following the agent file, and this step is skipped.
3. claude -p --agent pr-review-verifier on the reviewer's output, only when it has
   findings beyond nits (anything that could be posted to a coworker).
4. Write <KEY>-<repo>.md (+ .json sidecar) per PR to the drafts dir, plus
   <KEY>-story.md when one ticket covered several PRs, and an index entry per PR
   in state/reviews/ for the menu bar.

The model and effort come from each agent's frontmatter, in
<workspace>/.claude/agents/ or ~/.claude/agents/. stdout is one JSON summary line; failures exit non-zero
with the reason as the last stderr line.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import notify  # noqa: E402
import settings  # noqa: E402

REVIEW_INDEX_DIR = settings.STATE_DIR / "reviews"
RUNS_DIR = Path("~/.cache/pr-digest/reviews").expanduser()

# "## <KEY> (title) · avetta/<repo>#<n> · @author · head <sha>"
PR_HEADER_RE = re.compile(
    r"^## (?P<key>[A-Z][A-Z0-9]+-\d+|PR\d+)\b.*?avetta/(?P<repo>[\w.-]+)#(?P<number>\d+)"
    r".*?\bhead `?(?P<head>[0-9a-f]{7,40})`?",
    re.M,
)
SEVERITY_RE = re.compile(r"^SEVERITY: (substantive|nits-only|clean)\s*$", re.M)
FINDING_RE = re.compile(r"^\s*\d+\.\s*(🔴|🟡|🔵|⚠️)", re.M)
SEVERITY_NAMES = {"🔴": "blocking", "🟡": "advisory", "🔵": "nit", "⚠️": "decision"}

EXIT_AUTH, EXIT_CLAUDE, EXIT_OUTPUT = 2, 3, 4


class ReviewError(Exception):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


# --- pure helpers (unit-tested) -------------------------------------------


def count_findings(text: str) -> dict[str, int]:
    counts = {name: 0 for name in SEVERITY_NAMES.values()}
    for mark in FINDING_RE.findall(text):
        counts[SEVERITY_NAMES[mark]] += 1
    return counts


def severity_of(text: str) -> str:
    """The SEVERITY line the agents end with, else derived from the findings."""
    found = SEVERITY_RE.findall(text)
    if found:
        return found[-1]
    counts = count_findings(text)
    if counts["blocking"] or counts["advisory"] or counts["decision"]:
        return "substantive"
    return "nits-only" if counts["nit"] else "clean"


def split_pr_blocks(text: str) -> list[dict]:
    """One entry per `## <KEY> … avetta/<repo>#<n> … head <sha>` block."""
    matches = list(PR_HEADER_RE.finditer(text))
    blocks = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.start():end]
        body = SEVERITY_RE.sub("", body)
        body = re.split(r"^<details><summary>Verification", body, flags=re.M)[0].rstrip()
        blocks.append(
            {
                "key": m["key"],
                "repo": m["repo"],
                "number": int(m["number"]),
                "head": m["head"],
                "body": body,
            }
        )
    return blocks


VERDICT_ROW_RE = re.compile(r"^\|\s*\d+\s*\|.*\|\s*\**(CONFIRMED|REFUTED|UNVERIFIABLE)\b", re.M)


def verifier_stats(reviewer_text: str, final_text: str, envelope: dict | None) -> dict:
    """What the verifier did on one review, for review/verifier_stats.py.

    Counts the verdict column of its Verification table, and compares the findings
    the reviewer reported with the findings that survived.
    """
    if envelope is None:
        return {"ran": False}
    verdicts = {"confirmed": 0, "refuted": 0, "unverifiable": 0}
    for verdict in VERDICT_ROW_RE.findall(final_text):
        verdicts[verdict.lower()] += 1
    before, after = count_findings(reviewer_text), count_findings(final_text)
    return {
        "ran": True,
        **verdicts,
        "findings_before": before,
        "findings_after": after,
        "changed_review": verdicts["refuted"] + verdicts["unverifiable"] > 0 or before != after,
        "cost_usd": round(envelope.get("total_cost_usd") or 0, 2),
        "duration_s": int((envelope.get("duration_ms") or 0) / 1000),
    }


def strip_preamble(text: str) -> str:
    """Drop any prose an agent wrote before the review's `Reviewer:` line."""
    m = re.search(r"^Reviewer:", text, re.M)
    return text[m.start():] if m else text


def summary_line(counts: dict[str, int]) -> str:
    parts = [f"{n} {name}" for name, n in counts.items() if n]
    return " · ".join(parts) if parts else "no findings"


# --- claude runs ------------------------------------------------------------


def _env() -> dict:
    env = dict(os.environ)
    extra = [str(Path("~/.local/bin").expanduser()), "/opt/homebrew/bin", "/usr/local/bin"]
    env["PATH"] = ":".join(extra + [env.get("PATH", "/usr/bin:/bin")])
    return env


def claude_logged_in() -> bool | None:
    try:
        out = subprocess.run(
            ["claude", "auth", "status", "--json"],
            capture_output=True, text=True, timeout=30, env=_env(),
        ).stdout
        value = json.loads(out).get("loggedIn")
        return value if isinstance(value, bool) else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def run_agent(agent: str, prompt: str, cwd: Path, timeout: int, log_path: Path) -> dict:
    """claude -p --agent <agent>; returns the parsed JSON envelope."""
    argv = ["claude", "-p", "--agent", agent, "--output-format", "json", prompt]
    with log_path.open("w") as err:
        try:
            proc = subprocess.run(
                argv, cwd=cwd, stdout=subprocess.PIPE, stderr=err,
                text=True, timeout=timeout, env=_env(),
            )
        except subprocess.TimeoutExpired:
            raise ReviewError(f"{agent} timed out after {timeout // 60} min", EXIT_CLAUDE)
    try:
        envelope = json.loads(proc.stdout)
    except ValueError:
        raise ReviewError(f"{agent} returned no JSON (exit {proc.returncode}); see {log_path}", EXIT_CLAUDE)
    # Keep the run's metadata (models used, cost, turns) next to the stderr log.
    meta = {k: v for k, v in envelope.items() if k != "result"}
    log_path.with_suffix(".envelope.json").write_text(json.dumps(meta, indent=2) + "\n")
    if envelope.get("is_error") or not envelope.get("result"):
        reason = str(envelope.get("result") or f"exit {proc.returncode}")[:200]
        raise ReviewError(f"{agent} failed: {reason}", EXIT_CLAUDE)
    return envelope


# --- main -----------------------------------------------------------------------


def _phase(phase_file: Path | None, text: str, refresh_plugin: str | None) -> None:
    if not phase_file:
        return
    phase_file.write_text(f"{int(time.time())}\t{text}\n")
    if refresh_plugin:
        subprocess.run(
            ["open", "-g", f"swiftbar://refreshplugin?name={refresh_plugin}"],
            capture_output=True,
        )


def _slug(target: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", target.split("github.com/")[-1]).strip("-")[:60]


def write_drafts(final: str, meta: dict, drafts_dir: Path) -> list[dict]:
    """Write per-PR drafts, sidecars, and menu index entries. Returns one record per PR."""
    blocks = split_pr_blocks(final)
    if not blocks:
        raise ReviewError("review output has no '## <KEY> … avetta/<repo>#<n> … head <sha>' header", EXIT_OUTPUT)
    drafts_dir.mkdir(parents=True, exist_ok=True)
    REVIEW_INDEX_DIR.mkdir(parents=True, exist_ok=True)

    story_path = None
    if len(blocks) > 1:
        story_path = drafts_dir / f"{meta['target_key'] or blocks[0]['key']}-story.md"
        story_path.write_text(final)

    records = []
    for block in blocks:
        draft = drafts_dir / f"{block['key']}-{block['repo']}.md"
        if story_path:
            text = f"{block['body']}\n\nPart of the story review: {story_path}\n"
        else:
            text = final
        draft.write_text(text)
        counts = count_findings(block["body"])
        record = {
            "key": block["key"],
            "repo": block["repo"],
            "number": block["number"],
            "head": block["head"],
            "severity": severity_of(text) if not story_path else severity_of(block["body"]),
            "counts": counts,
            "draft": str(draft),
            "story": str(story_path) if story_path else None,
            **meta,
        }
        draft.with_suffix(".json").write_text(json.dumps(record, indent=2) + "\n")
        (REVIEW_INDEX_DIR / f"{block['repo']}#{block['number']}.json").write_text(
            json.dumps(record, indent=2) + "\n"
        )
        records.append(record)
    return records


def review(target: str, phase_file: Path | None, refresh_plugin: str | None,
           skip_verifier: bool, cfg: dict, reviewer_output: Path | None = None) -> list[dict]:
    """Run the reviewer (or take its report from reviewer_output), then the verifier.

    reviewer_output is a pr-reviewer report an interactive session wrote by following
    the agent file itself; the reviewer run is skipped and only the verifier runs.
    An empty review.reviewer_agent turns reviews off; an empty verifier_agent skips
    the verifier.
    """
    rcfg = cfg["review"]
    if reviewer_output is None and not rcfg.get("reviewer_agent"):
        raise ReviewError("reviews are off: review.reviewer_agent is empty in the config", EXIT_OUTPUT)
    skip_verifier = skip_verifier or not rcfg.get("verifier_agent")
    cwd = settings.workspace(cfg)
    started = time.time()
    run_dir = RUNS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{_slug(target)}"
    run_dir.mkdir(parents=True, exist_ok=True)

    reviewer_text = None
    if reviewer_output is not None:
        try:
            reviewer_text = strip_preamble(reviewer_output.read_text())
        except OSError as e:
            raise ReviewError(f"cannot read the reviewer output: {e}", EXIT_OUTPUT)
        if not split_pr_blocks(reviewer_text):
            raise ReviewError(
                "reviewer output has no '## <KEY> … avetta/<repo>#<n> … head <sha>' header", EXIT_OUTPUT,
            )

    needs_claude = reviewer_text is None or (not skip_verifier and severity_of(reviewer_text) == "substantive")
    if needs_claude:
        _phase(phase_file, "checking the claude login", refresh_plugin)
        if claude_logged_in() is False:
            raise ReviewError("claude CLI is logged out. Run: claude auth login", EXIT_AUTH)

    started = time.time()

    env1 = None
    if reviewer_text is None:
        _phase(phase_file, f"{rcfg['reviewer_agent']} is reviewing", refresh_plugin)
        env1 = run_agent(
            rcfg["reviewer_agent"], f"Review {target}.", cwd, rcfg["timeout"], run_dir / "reviewer.stderr",
        )
        reviewer_text = env1["result"]
    reviewer_out = run_dir / "reviewer.md"
    reviewer_out.write_text(reviewer_text)

    # The verifier exists to stop wrong findings reaching a coworker. A clean or
    # nits-only review has nothing worth refuting, so it skips the second run.
    final, env2 = reviewer_text, None
    if not skip_verifier and severity_of(reviewer_text) == "substantive":
        _phase(phase_file, f"{rcfg['verifier_agent']} is checking the findings", refresh_plugin)
        env2 = run_agent(
            rcfg["verifier_agent"], f"Verify the review at {reviewer_out}.", cwd,
            rcfg["timeout"], run_dir / "verifier.stderr",
        )
        final = env2["result"]
    final = strip_preamble(final)
    (run_dir / "final.md").write_text(final)

    _phase(phase_file, "saving the draft", refresh_plugin)
    key_match = re.search(r"\b([A-Z][A-Z0-9]+-\d+)\b", target)
    meta = {
        "target": target,
        "target_key": key_match.group(1) if key_match else None,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_s": int(time.time() - started),
        "cost_usd": round(sum(e.get("total_cost_usd", 0) or 0 for e in (env1, env2) if e), 2),
        "reviewer_cost_usd": round(env1.get("total_cost_usd") or 0, 2) if env1 else None,
        "reviewer": "headless" if env1 else "in-session",
        "verified": env2 is not None,
        "verification": verifier_stats(reviewer_text, final, env2),
        "run_dir": str(run_dir),
    }
    return write_drafts(final, meta, settings.path_of(rcfg["drafts_dir"]))


def _page_uri(md: Path) -> str:
    """file:// link to the draft rendered as a web page (review/view_draft.py), so a
    notification click opens something readable; the raw markdown when rendering fails."""
    try:
        from review import view_draft
        return view_draft.render(md).as_uri()
    except Exception:  # noqa: BLE001 — a notification must still go out
        return md.as_uri()


def announce(records: list[dict], cfg: dict) -> None:
    for r in records:
        title_key = f"{r['key']} · {r['repo']}"
        href = _page_uri(Path(r["story"] or r["draft"]))
        body = summary_line(r["counts"])
        if r["severity"] == "substantive":
            notify.alert(
                f"🔎 Review needs a look: {r['key']}", title_key, body, href=href,
                plugin=cfg["notify"]["swiftbar_plugin"], **notify.sound_options(cfg),
            )
        else:
            word = "only nits" if r["severity"] == "nits-only" else "clean"
            notify.banner(f"🔎 Review done: {r['key']} ({word})", title_key, body, href=href)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review a PR with pr-reviewer + pr-review-verifier.")
    parser.add_argument("target", help="CONN-1234 / WS-1234, a PR URL, or repo#N")
    parser.add_argument("--notify", action="store_true", help="send a notification when done")
    parser.add_argument("--phase-file", type=Path, help="write live progress here (menu bar)")
    parser.add_argument("--refresh-plugin", help="SwiftBar plugin to refresh on each phase")
    parser.add_argument("--skip-verifier", action="store_true", help="debugging only")
    parser.add_argument(
        "--reviewer-output", type=Path,
        help="a pr-reviewer report written in an interactive session; skips the reviewer run",
    )
    args = parser.parse_args(argv)

    cfg = settings.load()
    try:
        records = review(args.target, args.phase_file, args.refresh_plugin, args.skip_verifier, cfg,
                         reviewer_output=args.reviewer_output)
    except ReviewError as e:
        print(str(e), file=sys.stderr)
        if args.notify:
            notify.banner("⚠️ Review failed", args.target, str(e))
        return e.code
    finally:
        if args.phase_file:
            args.phase_file.unlink(missing_ok=True)

    if args.notify:
        announce(records, cfg)
    print(json.dumps({"status": "ok", "reviews": [
        {k: r[k] for k in ("key", "repo", "number", "severity", "draft", "story")} for r in records
    ]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
