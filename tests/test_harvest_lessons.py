from __future__ import annotations

import unittest

from review import harvest_lessons as hl

BOTS = ["copilot-pull-request-reviewer", "avettabot"]


def comment(login, body="x", typename="User", url="u"):
    return {"author": {"login": login, "__typename": typename}, "body": body,
            "createdAt": "2026-10-01T00:00:00Z", "url": url}


def thread(comments, outdated=True, resolved=False):
    return {"isOutdated": outdated, "isResolved": resolved, "path": "a.scala",
            "comments": {"nodes": comments}}


def pr(threads, author="dev"):
    return {"number": 1, "title": "t", "author": {"login": author},
            "repository": {"name": "processservice"}, "reviewThreads": {"nodes": threads}}


class LegitimateThreadTests(unittest.TestCase):
    def test_keeps_outdated_human_thread(self):
        [kept] = hl.legitimate_threads(pr([thread([comment("woods"), comment("dev", "done")])]), BOTS)
        self.assertEqual(kept["reviewer"], "woods")
        self.assertEqual(kept["replies"], [{"author": "dev", "body": "done"}])

    def test_drops_thread_whose_code_did_not_change(self):
        self.assertEqual(hl.legitimate_threads(pr([thread([comment("woods")], outdated=False)]), BOTS), [])

    def test_drops_resolved_without_change(self):
        self.assertEqual(
            hl.legitimate_threads(pr([thread([comment("woods")], outdated=False, resolved=True)]), BOTS), [])

    def test_drops_bots_copilot_and_self_notes(self):
        threads = [thread([comment("github-actions", typename="Bot")]),
                   thread([comment("copilot-pull-request-reviewer")]),
                   thread([comment("avettabot")]),
                   thread([comment("dev")]),
                   thread([{"author": None, "body": "x", "createdAt": "", "url": ""}])]
        self.assertEqual(hl.legitimate_threads(pr(threads), BOTS), [])


class ParseCandidatesTests(unittest.TestCase):
    def test_reads_fenced_array(self):
        text = 'Here you go:\n```json\n[{"rule": "Batch lookups", "urls": ["a"]}]\n```'
        self.assertEqual(hl.parse_candidates(text), [{"rule": "Batch lookups", "urls": ["a"]}])

    def test_bad_json_is_empty(self):
        self.assertEqual(hl.parse_candidates("no array here"), [])
        self.assertEqual(hl.parse_candidates("[not json]"), [])


LESSONS_MD = f"""# Review lessons

## Pinned

- Batch per-row service calls (processservice)

## Learned

{hl.AUTO_START}
{hl.AUTO_END}
"""


def lesson(rule, count=5, reviewers=("a", "b"), home="review-lessons.md"):
    return {"rule": rule, "count": count, "reviewers": list(reviewers), "repos": ["ui"],
            "examples": ["https://g/x"], "home": home}


class ApplyTests(unittest.TestCase):
    def test_qualifying_needs_three_threads_two_reviewers_and_caps(self):
        many = [lesson(f"rule {i}", count=3 + i) for i in range(30)]
        weak = [lesson("too few", count=2), lesson("one reviewer", reviewers=("a", "a"))]
        kept = hl.qualifying(many + weak)
        self.assertEqual(len(kept), hl.MAX_LESSONS)
        self.assertEqual(kept[0]["rule"], "rule 29")
        self.assertNotIn("too few", [k["rule"] for k in kept])
        self.assertNotIn("one reviewer", [k["rule"] for k in kept])

    def test_apply_replaces_only_the_auto_block(self):
        first = hl.apply_lessons(LESSONS_MD, [lesson("Use String.isBlank")], "2026-11-02")
        self.assertIn("- Batch per-row service calls (processservice)", first)
        self.assertIn("- Use String.isBlank. (ui; seen 5×; e.g. https://g/x)", first)
        second = hl.apply_lessons(first, [lesson("Alphabetize exposing lists")], "2026-11-09")
        self.assertNotIn("String.isBlank", second)
        self.assertEqual(second.count(hl.AUTO_START), 1)
        self.assertEqual(hl.changed_rules(first, second),
                         (["- Alphabetize exposing lists."], ["- Use String.isBlank."]))

    def test_pinned_excludes_the_auto_block(self):
        text = hl.apply_lessons(LESSONS_MD, [lesson("Learned one")], "d")
        self.assertEqual(hl.pinned_lessons(text), ["- Batch per-row service calls (processservice)"])

    def test_repo_specific_lesson_marked_as_candidate(self):
        line = hl.lesson_line(lesson("Map 4xx by fault", home="processservice/.ai/coding-standards.md"))
        self.assertTrue(line.endswith("*Candidate for `processservice/.ai/coding-standards.md`.*"))

    def test_parse_proposals_tolerates_fences(self):
        out = hl.parse_proposals('```json\n{"lessons": [{"rule": "r"}, {"x": 1}], "dropped": []}\n```')
        self.assertEqual(out, {"lessons": [{"rule": "r"}], "dropped": []})


if __name__ == "__main__":
    unittest.main()
