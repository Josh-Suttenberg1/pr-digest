from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from review import interdiff


class Repo:
    """A throwaway git repo with a main branch and a PR branch."""

    def __init__(self, root: Path):
        self.root = root
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, files: dict[str, str], msg: str) -> str:
        for name, text in files.items():
            (self.root / name).write_text(text)
            self.git("add", name)
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")


def lines(n: int, tag: str = "") -> str:
    return "".join(f"line {i}{tag}\n" for i in range(n))


class InterdiffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = Repo(Path(self.tmp.name))
        self.r.commit({"a.txt": lines(20), "b.txt": lines(20)}, "base")
        self.r.git("checkout", "-q", "-b", "pr")
        self.h0 = self.r.commit({"a.txt": lines(20).replace("line 5\n", "line 5 changed\n")},
                                "pr change")
        self.r.git("checkout", "-q", "main")
        self.main = self.r.commit({"b.txt": lines(20).replace("line 15\n", "main 15\n")},
                                  "main moves")
        self.r.git("checkout", "-q", "pr")

    def tearDown(self):
        self.tmp.cleanup()

    def merge_main(self) -> str:
        self.r.git("merge", "-q", "--no-edit", "main")
        return self.r.git("rev-parse", "HEAD")

    def test_a_merge_from_main_alone_is_an_empty_interdiff(self):
        h1 = self.merge_main()
        d = interdiff.compute(self.r.root, self.main, self.h0, h1)
        self.assertTrue(d["empty"])
        self.assertEqual(d["method"], "replay")
        self.assertEqual(d["old_fingerprint"]["sha"], d["new_fingerprint"]["sha"])
        self.assertEqual(d["commits"], 1)

    def test_an_author_change_after_a_main_merge_is_all_the_interdiff_holds(self):
        self.merge_main()
        text = lines(20).replace("line 5\n", "line 5 changed\n").replace("line 9\n", "fix 9\n")
        h1 = self.r.commit({"a.txt": text}, "address review")
        d = interdiff.compute(self.r.root, self.main, self.h0, h1)
        self.assertFalse(d["empty"])
        self.assertEqual(list(d["files"]), ["a.txt"])
        self.assertIn("+fix 9", d["diff"])
        self.assertNotIn("main 15", d["diff"])
        self.assertEqual(d["lines"], 2)
        self.assertEqual(d["new_files"], [])
        self.assertEqual(d["ranges"]["a.txt"][0][0] <= 10 <= d["ranges"]["a.txt"][0][1], True)
        self.assertEqual(d["commits"], 2)

    def test_a_file_the_review_never_saw_is_a_new_file(self):
        h1 = self.r.commit({"c.txt": "new\n"}, "add c")
        d = interdiff.compute(self.r.root, self.main, self.h0, h1)
        self.assertEqual(d["new_files"], ["c.txt"])
        self.assertIn("outside what was reviewed", interdiff.needs_full_review(d, 0.5))

    def test_a_large_share_of_the_pr_calls_for_a_full_review(self):
        h1 = self.r.commit({"a.txt": lines(20, " rewritten")}, "rewrite")
        d = interdiff.compute(self.r.root, self.main, self.h0, h1)
        self.assertGreater(d["lines"], 0.5 * d["pr_lines"])
        self.assertIn("more than 50%", interdiff.needs_full_review(d, 0.5))

    def test_a_small_change_is_an_update(self):
        text = lines(20).replace("line 5\n", "line 5 changed\n").replace("line 9\n", "fix 9\n")
        h1 = self.r.commit({"a.txt": text}, "small")
        d = interdiff.compute(self.r.root, self.main, self.h0, h1)
        self.assertEqual(interdiff.needs_full_review(d, 1.5), "")

    def test_a_force_push_has_no_commit_count(self):
        self.r.git("reset", "-q", "--hard", "HEAD~1")
        h1 = self.r.commit({"a.txt": lines(20).replace("line 6\n", "other\n")}, "redo")
        d = interdiff.compute(self.r.root, self.main, self.h0, h1)
        self.assertIsNone(d["commits"])
        self.assertFalse(d["empty"])

    def test_a_conflicting_replay_falls_back_to_the_changed_files(self):
        # main edits the line the PR edited, and the PR resolves the conflict in a merge
        self.r.git("checkout", "-q", "main")
        tip = self.r.commit({"a.txt": lines(20).replace("line 5\n", "main 5\n")}, "clash")
        self.r.git("checkout", "-q", "pr")
        subprocess.run(["git", "-C", str(self.r.root), "merge", "-q", "main"],
                       capture_output=True)
        (self.r.root / "a.txt").write_text(lines(20).replace("line 5\n", "resolved 5\n"))
        self.r.git("add", "a.txt")
        self.r.git("commit", "-q", "--no-edit")
        h1 = self.r.git("rev-parse", "HEAD")
        d = interdiff.compute(self.r.root, tip, self.h0, h1)
        self.assertEqual(d["method"], "files")
        self.assertEqual(list(d["files"]), ["a.txt"])


class PureTest(unittest.TestCase):
    def test_fingerprint_ignores_line_positions(self):
        a = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n ctx\n-old\n+new\n"
        b = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -40,2 +41,2 @@\n other\n-old\n+new\n"
        self.assertEqual(interdiff.fingerprint({"x": a}), interdiff.fingerprint({"x": b}))

    def test_file_patches_names_a_deleted_file_by_its_old_path(self):
        diff = "diff --git a/gone b/gone\ndeleted file mode 100644\n--- a/gone\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
        self.assertEqual(list(interdiff.file_patches(diff)), ["gone"])

    def test_new_side_ranges(self):
        patch = "@@ -1,3 +1,4 @@\n a\n+b\n@@ -10 +11 @@\n-x\n+y\n"
        self.assertEqual(interdiff.new_side_ranges(patch), [(1, 4), (11, 11)])


if __name__ == "__main__":
    unittest.main()
