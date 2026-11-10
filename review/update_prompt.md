You are updating a code review draft that @{me} has not posted yet. The draft reviewed
avetta/{repo}#{number} at {old_short}. Since then the author pushed {commits_text}. Review
ONLY what the author changed since {old_short}, shown below as an interdiff: the PR's own
diff at {old_short} turned into its own diff at {new_short}, with anything the base branch
brought in already taken out. Don't review anything outside it.

PR: avetta/{repo}#{number} — {title}
PR author: @{author}
Base branch: {base}

## The interdiff ({method_text})
```diff
{diff}
```
{truncated_note}
## Earlier findings to judge

These findings from the draft sit in files the interdiff touches, or name no file. For
each, decide whether the changes since {old_short} address it:
- "addressed": the change does what the finding asked, or fixes the defect another way.
- "partly": some of it is done; say what is left.
- "not": the change leaves the defect in place.
- "no_longer_relevant": the code the finding was about is gone or rewritten so the finding
  no longer applies.

{findings}

## Review comments on the changed lines to judge

Each is an open review thread, by any reviewer, on lines the interdiff changed. Judge only
against what the comment asked for, the way a follow-up on your own comment would:
"verdict" is "addressed" | "partly" | "not_addressed", and "mechanical" is true only when
the commits do exactly what the comment asked, as it was asked: no pushback, no different
approach, nothing asked left open.

{comments}

## A defect review of the new and changed lines

Review the added and changed lines in the interdiff for defects, against the same rules
pr-reviewer uses, in this order of authority:
1. avetta/{repo}'s own `.ai/` files that apply to these lines, and its `CLAUDE.md` /
   `.claude/CLAUDE.md` (in {clone}).
2. {elm_rules}
3. ~/avetta/.claude/review/review-lessons.md, and the checks in
   ~/avetta/.claude/review/recurring-checks.md that apply to these lines.

Priorities: correctness first, then cross-file invariants and boundaries (the v1/v2
boundary, error handling at service edges, producer/consumer contracts), then data-access
efficiency, then style as nits. Always blocking: v2 code in a v1 path or the reverse,
Mongo writes in local, test or script code, `--force` pushes or `--no-verify` in scripts.

Every new finding proposes its fix and carries evidence a verifier can re-check: the
`file:line` at {new_short} and what it shows. Settle a question from the code or drop it;
never ask the author to confirm something the code can answer. Don't repeat an earlier
finding, and don't raise what a reviewer's open comment already raised.

## Reading the code

The clone is at {clone}. Read the code at the new head with
`git -C {clone} show {new_head}:<path>`, `git -C {clone} grep -n <sym> {new_head} -- <glob>`,
`git -C {clone} log` and `git -C {clone} diff {old_head} {new_head} -- <path>`. Never check
out a branch. `gh pr view <n> --repo avetta/<repo>` reads a PR's body and state; nothing
may be posted.

## Output

Output ONLY a JSON object, no prose, no code fences:
{{"findings": [{{"id": "<the finding's id>", "status": "addressed" | "partly" | "not" | "no_longer_relevant",
                "sha": "<short sha of the commit that settled it, or empty>",
                "note": "<one sentence: what changed, or what is still missing>"}}],
  "comments": [{{"id": <the comment's id>, "verdict": "addressed" | "partly" | "not_addressed",
                "mechanical": true | false,
                "evidence": [{{"file": "...", "line": 0, "sha": "<short sha>", "note": "..."}}],
                "note": "<one sentence>"}}],
  "new_findings": [{{"severity": "blocking" | "advisory" | "nit" | "decision",
                    "path": "<path>", "line": 0,
                    "text": "<claim, at most 70 characters>. <What goes wrong because of it.> *Fix:* <change>.",
                    "evidence": "<file:line at {new_short} and what it shows>"}}]}}
Every finding and comment listed above gets exactly one entry. "new_findings" is empty
when the changed lines are clean.
