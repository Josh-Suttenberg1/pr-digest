"""What a PR's author changed between two of its heads, ignoring what the base brought in.

A PR's own diff at a head is `merge-base(base tip, head)..head`. The interdiff between
the reviewed head H0 and a new head H1 is what turns the PR's own diff at H0 into its
own diff at H1, the way GitHub's "changes since your last review" reads:

  T = the PR's changes at H0 replayed onto H1's merge base:
      `git merge-tree --write-tree --merge-base=<mb0> <mb1> <H0>`, or H0's own tree
      when both heads share a merge base
  interdiff = `git diff T H1`

A merge from the base branch that touches none of the PR's changes leaves T equal to
H1's tree, so the interdiff is empty. When replaying H0 onto the new base conflicts,
the interdiff falls back to `git diff H0 H1` limited to the files whose own patch
changed (`method` "files"), which can then include base changes to those files.

Every function takes the clone path and runs read-only git in it: `fetch` writes
objects only (no ref, branch, index, worktree or FETCH_HEAD change), and
`merge-tree --write-tree` writes tree objects only.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

DIFF_LIMIT = 60_000
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)


class InterdiffError(Exception):
    pass


def _git(clone: Path, *args: str, check: bool = True,
         timeout: int = 300) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(clone), *args], capture_output=True, text=True,
                          timeout=timeout)
    if check and proc.returncode != 0:
        raise InterdiffError(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()[:200]}")
    return proc


def _has(clone: Path, sha: str) -> bool:
    return _git(clone, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0


def fetch(clone: Path, number: int, base_ref: str, shas: list[str]) -> str:
    """Fetch the PR head, the base branch tip and any of `shas` the clone lacks, into
    the object store only. Returns the base branch tip's sha."""
    _git(clone, "fetch", "-q", "--no-write-fetch-head", "origin", f"pull/{number}/head")
    out = _git(clone, "ls-remote", "origin", f"refs/heads/{base_ref}").stdout.split()
    if not out:
        raise InterdiffError(f"no base branch {base_ref} on origin")
    base_tip = out[0]
    for sha in [base_tip, *shas]:
        if sha and not _has(clone, sha):
            _git(clone, "fetch", "-q", "--no-write-fetch-head", "origin", sha, check=False)
            if not _has(clone, sha):
                raise InterdiffError(f"commit {sha[:12]} is not on origin any more")
    return base_tip


def merge_base(clone: Path, base_tip: str, head: str) -> str:
    return _git(clone, "merge-base", base_tip, head).stdout.strip()


def file_patches(diff: str) -> dict[str, str]:
    """`git diff` output split per file: path (the new path) -> its patch text."""
    out: dict[str, str] = {}
    for chunk in re.split(r"(?m)^(?=diff --git )", diff):
        if not chunk.startswith("diff --git "):
            continue
        m = re.search(r"(?m)^\+\+\+ b/(.+)$", chunk) or re.search(r"(?m)^--- a/(.+)$", chunk)
        path = m.group(1) if m else chunk.split(" b/", 1)[-1].splitlines()[0]
        out[path] = chunk
    return out


def _changed_lines(patch: str) -> list[str]:
    """A patch's added and removed lines, without file headers, hunk positions or
    context, so the same change at shifted line numbers compares equal."""
    return [ln for ln in patch.splitlines()
            if ln[:1] in "+-" and not ln.startswith(("+++ ", "--- "))]


def fingerprint(patches: dict[str, str]) -> dict:
    """{"sha": one hash of the whole diff, "files": {path: hash}} over each file's
    added and removed lines."""
    files = {p: hashlib.sha256("\n".join(_changed_lines(t)).encode()).hexdigest()[:16]
             for p, t in sorted(patches.items())}
    whole = hashlib.sha256("\n".join(f"{p}:{h}" for p, h in files.items()).encode())
    return {"sha": whole.hexdigest()[:16], "files": files}


def pr_diff(clone: Path, base_tip: str, head: str) -> dict:
    """The PR's own diff at `head`: {merge_base, patches, lines, fingerprint}."""
    mb = merge_base(clone, base_tip, head)
    text = _git(clone, "diff", "--no-color", "--no-ext-diff", mb, head).stdout
    patches = file_patches(text)
    return {"merge_base": mb, "patches": patches,
            "lines": sum(len(_changed_lines(t)) for t in patches.values()),
            "fingerprint": fingerprint(patches)}


def new_side_ranges(patch: str) -> list[tuple[int, int]]:
    """(first, last) line numbers each hunk covers on the new side."""
    out = []
    for m in HUNK_RE.finditer(patch):
        start, count = int(m.group(1)), int(m.group(2) or 1)
        out.append((start, start + max(count, 1) - 1))
    return out


def compute(clone: Path, base_tip: str, old_head: str, new_head: str) -> dict:
    """The interdiff between two heads of one PR. Returns {empty, method, diff (text,
    cut at DIFF_LIMIT), files: {path: patch}, ranges: {path: [(first, last)]},
    lines (changed lines in the interdiff), pr_lines (in the PR's own diff at
    new_head), reviewed_files, new_files (interdiff files outside reviewed_files),
    commits (new_head's commits since old_head, or None after a force push),
    old_merge_base, new_merge_base, old_fingerprint, new_fingerprint}."""
    old, new = pr_diff(clone, base_tip, old_head), pr_diff(clone, base_tip, new_head)
    method = "replay"
    if old["merge_base"] == new["merge_base"]:
        tree = f"{old_head}^{{tree}}"
    else:
        proc = _git(clone, "merge-tree", "--write-tree", f"--merge-base={old['merge_base']}",
                    new["merge_base"], old_head, check=False)
        tree = proc.stdout.split("\n", 1)[0].strip() if proc.returncode == 0 else ""
    if tree:
        text = _git(clone, "diff", "--no-color", "--no-ext-diff", tree, new_head).stdout
    else:
        method = "files"
        changed = sorted(p for p in set(old["patches"]) | set(new["patches"])
                         if old["fingerprint"]["files"].get(p) != new["fingerprint"]["files"].get(p))
        text = (_git(clone, "diff", "--no-color", "--no-ext-diff", old_head, new_head, "--",
                     *changed).stdout if changed else "")
    files = file_patches(text)
    ancestor = _git(clone, "merge-base", "--is-ancestor", old_head, new_head,
                    check=False).returncode == 0
    commits = (int(_git(clone, "rev-list", "--count", "--first-parent",
                        f"{old_head}..{new_head}").stdout.strip() or 0) if ancestor else None)
    return {
        "empty": not files, "method": method, "diff": text[:DIFF_LIMIT],
        "truncated": len(text) > DIFF_LIMIT, "files": files,
        "ranges": {p: new_side_ranges(t) for p, t in files.items()},
        "lines": sum(len(_changed_lines(t)) for t in files.values()),
        "pr_lines": new["lines"], "reviewed_files": sorted(old["patches"]),
        "new_files": sorted(set(files) - set(old["patches"])), "commits": commits,
        "old_merge_base": old["merge_base"], "new_merge_base": new["merge_base"],
        "old_fingerprint": old["fingerprint"], "new_fingerprint": new["fingerprint"],
    }


def needs_full_review(delta: dict, max_share: float) -> str:
    """Why the interdiff calls for a full review instead of an update; '' when an
    update covers it: it changes more than `max_share` of the PR's changed lines, or
    touches files the reviewed diff didn't have."""
    if delta["new_files"]:
        n = len(delta["new_files"])
        return f"{n} file{'s' if n != 1 else ''} outside what was reviewed"
    if delta["pr_lines"] and delta["lines"] > max_share * delta["pr_lines"]:
        return (f"{delta['lines']} of the PR's {delta['pr_lines']} changed lines moved "
                f"(more than {max_share:.0%})")
    return ""
