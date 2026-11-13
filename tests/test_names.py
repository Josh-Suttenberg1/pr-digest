import tempfile
import unittest
from unittest import mock
from pathlib import Path

import names


class BookTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "names.json"
        self.calls = []
        self.profiles = {"alovelace": "Ada Lovelace", "noname": ""}

    def tearDown(self):
        self.dir.cleanup()

    def lookup(self, login):
        self.calls.append(login)
        return self.profiles.get(login)          # None: the lookup failed

    def book(self, fetch, now=1_000_000, overrides=None):
        return names.Book(fetch, self.path, self.lookup, lambda: now, overrides)

    def test_configured_name_wins_without_a_lookup(self):
        b = self.book(fetch=True, overrides={"alovelace": "Ada", "noname": "Nemo"})
        self.assertEqual(b.get("alovelace", "alovelace"), "Ada")
        self.assertEqual(b["noname"], "Nemo")
        self.assertIn("noname", b)
        self.assertEqual(b.get("other", "other"), "other")
        self.assertEqual(self.calls, ["other"])

    def test_book_reads_the_names_table(self):
        with mock.patch("settings.load", return_value={"names": {"alovelace": "Ada", "x": ""}}):
            b = names.book()
        self.assertEqual(b.get("alovelace"), "Ada")
        self.assertEqual(b._overrides, {"alovelace": "Ada"})

    def test_fetching_book_looks_up_once_and_caches(self):
        b = self.book(fetch=True)
        self.assertEqual(b.get("alovelace", "alovelace"), "Ada")
        self.assertEqual(b.get("alovelace", "alovelace"), "Ada")
        self.assertEqual(self.calls, ["alovelace"])
        self.assertEqual(self.book(fetch=False).get("alovelace"), "Ada")
        # The cache keeps the whole profile name; only what's shown is the first word.
        self.assertIn("Ada Lovelace", self.path.read_text())

    def test_cache_only_book_never_looks_up(self):
        b = self.book(fetch=False)
        self.assertEqual(b.get("alovelace", "alovelace"), "alovelace")
        self.assertNotIn("alovelace", b)
        self.assertEqual(self.calls, [])

    def test_profile_without_a_name_falls_back_to_the_login(self):
        b = self.book(fetch=True)
        self.assertEqual(b.get("noname", "noname"), "noname")
        with self.assertRaises(KeyError):
            b["noname"]

    def test_failed_lookup_isnt_cached(self):
        self.book(fetch=True).get("ghost")
        self.book(fetch=True).get("ghost")
        self.assertEqual(self.calls, ["ghost", "ghost"])

    def test_stale_names_are_looked_up_again(self):
        self.book(fetch=True).get("alovelace")
        later = 1_000_000 + (names.REFRESH_DAYS + 1) * 86400
        self.book(fetch=True, now=later).get("alovelace")
        self.assertEqual(self.calls, ["alovelace", "alovelace"])

    def test_teams_are_never_looked_up(self):
        self.assertEqual(self.book(fetch=True).get("team:ui", "team:ui"), "team:ui")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
