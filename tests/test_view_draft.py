from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from review import view_draft as v


class PrepareTest(unittest.TestCase):
    def test_details_get_markdown_rendering(self):
        self.assertIn('<details markdown="1">', v._prepare("<details>\n| a |\n</details>"))

    def test_bare_links_become_clickable_but_not_inside_code(self):
        out = v._prepare("🔗 https://github.com/acme/api/pull/1\n"
                         "see `https://example.com/x`\n"
                         "[PR](https://github.com/acme/api/pull/2)\n"
                         "```\nhttps://example.com/in-fence\n```")
        self.assertIn("<https://github.com/acme/api/pull/1>", out)
        self.assertIn("`https://example.com/x`", out)
        self.assertIn("[PR](https://github.com/acme/api/pull/2)", out)
        self.assertIn("\nhttps://example.com/in-fence\n", out)

    def test_render_has_discuss_and_pr_buttons(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = Path(tmp) / "ABC-1-api.md"
            md.write_text("## ABC-1\n🔗 https://github.com/acme/api/pull/7\n\n| x | y |\n|---|---|\n| 1 | 2 |\n")
            old = v.OUT_DIR
            v.OUT_DIR = Path(tmp) / "html"
            try:
                page = v.render(md).read_text()
            finally:
                v.OUT_DIR = old
        self.assertIn("prdigest://discuss?file=", page)
        self.assertIn('href="https://github.com/acme/api/pull/7"', page)
        self.assertIn("<table>", page)


class HandleUrlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        self.drafts, self.reports = root / "drafts", root / "reports"
        self.drafts.mkdir()
        self.reports.mkdir()
        self.cfg = {"review": {"drafts_dir": str(self.drafts)},
                    "watcher": {"reports_dir": str(self.reports)}}
        self.draft = self.drafts / "ABC-1-api.md"
        self.draft.write_text("x")
        self.report = self.reports / "api-1-2-2.md"
        self.report.write_text("x")
        (root / "secret.md").write_text("x")

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, url):
        return v.validated_path(url, self.cfg)

    def test_accepts_drafts_and_reports(self):
        self.assertEqual(self.check(v.discuss_url(self.draft)), self.draft)
        self.assertEqual(self.check(v.discuss_url(self.report)), self.report)

    def test_prompt_is_built_here_by_kind(self):
        self.assertIn("review draft", v.prompt_for(self.draft, self.cfg))
        self.assertIn("follow-up", v.prompt_for(self.report, self.cfg))

    def test_refuses_anything_else(self):
        bad = [
            "prdigest://run?file=" + str(self.draft),            # wrong action
            "https://discuss?file=" + str(self.draft),            # wrong scheme
            "prdigest://discuss?file=" + str(self.drafts / ".." / "secret.md"),  # escapes
            "prdigest://discuss?file=/etc/hosts",                 # not a draft folder
            "prdigest://discuss?file=" + str(self.drafts / "missing.md"),
            "prdigest://discuss",                                 # no file
            f"prdigest://discuss?file={self.draft}&file={self.report}",
        ]
        for url in bad:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.check(url)


if __name__ == "__main__":
    unittest.main()
