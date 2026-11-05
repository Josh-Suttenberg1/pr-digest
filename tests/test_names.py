import tempfile
import unittest
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

    def book(self, fetch, now=1_000_000):
        return names.Book(fetch, self.path, self.lookup, lambda: now)

    def test_fetching_book_looks_up_once_and_caches(self):
        b = self.book(fetch=True)
        self.assertEqual(b.get("alovelace", "alovelace"), "Ada Lovelace")
        self.assertEqual(b.get("alovelace", "alovelace"), "Ada Lovelace")
        self.assertEqual(self.calls, ["alovelace"])
        self.assertEqual(self.book(fetch=False).get("alovelace"), "Ada Lovelace")

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
