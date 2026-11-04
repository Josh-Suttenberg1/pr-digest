"""What kind of review comment this is, from the label its author led with. Pure.

Reviewers here lead with a label ("**Nit:**", "Suggestion:", "[question]"). The kind
decides how a comment is shown and ordered: a blocking comment or a suggestion needs
a decision, a nit is a small mechanical fix. An unlabeled comment whose first line
ends in "?" is a question; a GitHub ```suggestion block is a suggestion; anything
else is a plain comment.
"""

from __future__ import annotations

import re
from collections import Counter

# Display order: what needs the most thought first, nits last.
ORDER = ("blocking", "suggestion", "question", "comment", "nit")

LABELS = {
    "blocking": "🛑 Blocking",
    "suggestion": "💡 Suggestion",
    "question": "❓ Question",
    "comment": "🗨 Comment",
    "nit": "🔹 Nit",
}

_PLURAL = {"blocking": "blocking", "suggestion": "suggestions", "question": "questions",
           "comment": "comments", "nit": "nits"}

_ALIASES = {"nit": "nit", "nitpick": "nit", "minor": "nit",
            "suggestion": "suggestion", "suggest": "suggestion",
            "question": "question", "q": "question",
            "blocking": "blocking", "blocker": "blocking", "must fix": "blocking",
            "must-fix": "blocking"}

# A leading label: optional markdown emphasis or brackets, the word, then ":" or a
# closing bracket. "Nit picking aside" doesn't match; "**Nit:**" and "[nit]" do.
_LABEL = re.compile(
    r"^[\s*_\[(]*(nitpick|nit|minor|suggestion|suggest|question|q|blocking|blocker|must[- ]fix)"
    r"\s*(?:[:\])]|\*\*\s*:|:\s*\*\*)[\s*_\])]*",
    re.IGNORECASE,
)


def _first_text_line(body: str) -> str:
    for line in (body or "").splitlines():
        line = line.strip()
        if line and not line.startswith("```"):
            return line
    return ""


def kind(body: str) -> str:
    """One of ORDER."""
    line = _first_text_line(body)
    m = _LABEL.match(line)
    if m:
        return _ALIASES[m.group(1).lower()]
    if "```suggestion" in (body or ""):
        return "suggestion"
    if line.rstrip("*_ ").endswith("?"):
        return "question"
    return "comment"


def summary(body: str, limit: int = 90) -> str:
    """The comment's first line without its label or markdown emphasis."""
    line = _LABEL.sub("", _first_text_line(body), count=1)
    line = re.sub(r"[*_]{2}", "", line).lstrip(">#-* ").strip()
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def phrase(kinds) -> str:
    """'3 suggestions, 5 nits' from an iterable of kinds, in ORDER; '' when empty."""
    counts = Counter(kinds)
    parts = []
    for k in ORDER:
        n = counts.get(k, 0)
        if n:
            parts.append(f"{n} {k if n == 1 else _PLURAL[k]}")
    return ", ".join(parts)


def sort_key(k: str) -> int:
    return ORDER.index(k) if k in ORDER else len(ORDER)
