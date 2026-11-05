import tempfile
import unittest
from pathlib import Path

import settings


class LoadTest(unittest.TestCase):
    def load(self, text: str) -> dict:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text(text)
            return settings.load(path)

    def test_repos_root_defaults_to_the_workspace(self):
        cfg = self.load('workspace = "~/work"\n')
        self.assertEqual(cfg["repos_root"], "~/work")
        self.assertEqual(settings.workspace(cfg), Path("~/work").expanduser())

    def test_repos_root_can_differ_from_the_workspace(self):
        cfg = self.load('workspace = "~/work"\nrepos_root = "~/code"\n')
        self.assertEqual(cfg["repos_root"], "~/code")

    def test_review_notes_live_under_the_workspace(self):
        cfg = self.load('workspace = "/tmp/ws"\n')
        self.assertEqual(settings.review_notes_dir(cfg), Path("/tmp/ws/.claude/review"))

    def test_outputs_default_to_the_checkout(self):
        cfg = self.load("")
        self.assertEqual(Path(cfg["review"]["drafts_dir"]), settings.OUTPUT_DIR / "reviews")
        self.assertEqual(Path(cfg["watcher"]["reports_dir"]), settings.OUTPUT_DIR / "followups")
        self.assertEqual(cfg["lessons"]["repos"], [])


if __name__ == "__main__":
    unittest.main()
