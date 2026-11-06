"""triage/jira.py lookups and the menu's grouping of PRs by their Jira ticket."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import swiftbar_plugin as menu
from triage import jira

NOW = datetime(2026, 11, 6, 18, 0, tzinfo=timezone.utc)
CFG = {"jira": {"cli": "acli", "cache_days": 7}}


def view(key, *, subtask=False, parent=None, summary="s"):
    fields = {"issuetype": {"subtask": subtask}, "summary": summary}
    if parent:
        fields["parent"] = {"key": parent, "fields": {"summary": f"parent {parent}"}}
    return {"key": key, "fields": fields}


class GroupOfTest(unittest.TestCase):
    def test_a_sub_task_groups_under_its_parent(self):
        self.assertEqual(jira.group_of(view("ABC-2", subtask=True, parent="ABC-1")),
                         {"key": "ABC-1", "summary": "parent ABC-1"})

    def test_anything_else_groups_under_itself_even_with_an_epic_parent(self):
        self.assertEqual(jira.group_of(view("ABC-5", parent="ABC-EPIC", summary="story")),
                         {"key": "ABC-5", "summary": "story"})


class GroupsCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "jira.json"
        self.asked = []

    def fake(self, cli, key):
        self.asked.append(key)
        return None if key == "ABC-9" else view(key, subtask=True, parent="ABC-1")

    def test_lookups_are_cached_and_unreadable_keys_retried(self):
        out = jira.groups({"ABC-2", "ABC-9"}, CFG, path=self.path, now=1000, view=self.fake)
        self.assertEqual(out, {"ABC-2": {"key": "ABC-1", "summary": "parent ABC-1"}})
        self.assertEqual(sorted(json.loads(self.path.read_text())), ["ABC-2"])
        self.asked.clear()
        jira.groups({"ABC-2", "ABC-9"}, CFG, path=self.path, now=2000, view=self.fake)
        self.assertEqual(self.asked, ["ABC-9"])            # ABC-2 came from the cache

    def test_a_stale_entry_is_looked_up_again(self):
        jira.groups({"ABC-2"}, CFG, path=self.path, now=0, view=self.fake)
        self.asked.clear()
        jira.groups({"ABC-2"}, CFG, path=self.path, now=8 * 86400, view=self.fake)
        self.assertEqual(self.asked, ["ABC-2"])

    def test_no_cli_means_no_lookups(self):
        self.assertEqual(jira.groups({"ABC-2"}, {"jira": {"cli": ""}}, path=self.path,
                                     view=self.fake), {})
        self.assertEqual(self.asked, [])


def mine_row(num, title, **kw):
    base = {"url": f"https://github.com/acme/{kw.pop('repo', 'web')}/pull/{num}",
            "title": title, "repo": "web", "number": num, "isDraft": False,
            "waiting": False, "updated_at": "", "unanswered_comments": [], "requested": [],
            "requested_since": {}, "blockers": [], "answered_change_requests": [],
            "threads_waiting": [], "approvals": [], "head_at": ""}
    return {**base, **kw}


def data(mine, groups):
    return {"me": "me", "reviews": {}, "mine": mine, "legacy": [], "groups": groups}


class GroupedTest(unittest.TestCase):
    def test_sub_tasks_fold_into_their_parent_at_the_most_urgent_childs_place(self):
        ask = {"author": "rev", "kind": "question", "last_at": "2026-11-05T00:00:00Z"}
        rows = [mine_row(1, "[ABC-2] be part", repo="api"),
                mine_row(2, "[ABC-3] fe part", waiting=True, threads_waiting=[ask]),
                mine_row(3, "[ABC-7] alone")]
        groups = {"ABC-2": {"key": "ABC-1", "summary": "the story"},
                  "ABC-3": {"key": "ABC-1", "summary": "the story"}}
        s = menu.build_sections(data(rows, groups), [], now=NOW)
        view_ = s["mine_view"]
        self.assertEqual([e.get("key") or e["row"]["number"] for e in view_], ["ABC-1", 3])
        g = view_[0]
        self.assertEqual((g["group"], g["color"]), ("needs_you", "red"))
        self.assertEqual([k["row"]["number"] for k in g["children"]], [2, 1])
        self.assertEqual(len(s["mine"]), 3)        # the flat list still has every PR
        self.assertEqual(s["title"], "🔴1")        # the title still counts PRs

    def test_prs_sharing_a_ticket_key_group_without_any_lookup(self):
        rows = [mine_row(1, "[ABC-4] api", repo="api"), mine_row(2, "[ABC-4] web")]
        [g] = menu.build_sections(data(rows, {}), [], now=NOW)["mine_view"]
        self.assertEqual((g["kind"], g["key"]), ("group", "ABC-4"))

    def test_a_group_row_nests_each_child_with_its_own_submenu(self):
        rows = [mine_row(1, "[ABC-2] be part", repo="api"), mine_row(2, "[ABC-3] fe part")]
        groups = {"ABC-2": {"key": "ABC-1", "summary": "the story"},
                  "ABC-3": {"key": "ABC-1", "summary": "the story"}}
        s = menu.build_sections(data(rows, groups), [], now=NOW)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            menu.mine_rows(s["mine_view"], {}, 2)
        lines = [x.split(" | ")[0] for x in out.getvalue().splitlines()]
        self.assertIn("ABC-1 · 2 PRs: 2 waiting on others · the story", lines)
        kids = [x for x in lines if x.startswith("--ABC-")]
        self.assertTrue(kids[0].startswith("--ABC-1 · the story"))
        self.assertTrue(any(x.startswith("--ABC-2 (api) · ") for x in kids))
        self.assertTrue(any(x.startswith("--ABC-3 (web) · ") for x in kids))
        self.assertTrue(any(x.startswith("----") and "Open on GitHub" in x for x in lines))
        self.assertEqual(menu._PREFIX, "")          # nesting is undone afterwards


if __name__ == "__main__":
    unittest.main()
