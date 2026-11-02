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


if __name__ == "__main__":
    unittest.main()
