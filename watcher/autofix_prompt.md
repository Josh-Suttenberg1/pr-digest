A reviewer left a comment on @{me}'s pull request {pr_url} in the {org} organization.
It was checked already: it's a small mechanical change, and it's right. Make exactly
that change in this working tree, which is the PR's head commit.

File: {path}, line {line}
The comment:
---
{comment}
---

The planned fix: {plan}

Rules:
- Change only what the comment asks for, as the plan says, in {path} (and only the
  other files the plan names). No other cleanup, no reformatting, no renames it
  didn't ask for.
- Follow this repo's own conventions: read its CLAUDE.md, .claude/CLAUDE.md and .ai/
  docs when they exist, and match the style of the code around the change.
- You can read, search and edit files. You can't run commands, build, test or use
  git; the change is validated and committed after you finish.
- If the change turns out to need more than the plan says, or you can't make it
  safely, change nothing and say why.

When you're done, output ONLY a JSON object, no prose, no code fences:
{"summary": "<one line, starting with a lowercase verb, saying what changed, e.g. renamed `t` to `total`>",
  "changed": true | false,
  "reason": "<when changed is false: why not>"}
