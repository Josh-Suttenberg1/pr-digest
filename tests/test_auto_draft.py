from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import settings
from review import auto_draft as A
from review import record
from triage import priority as P

NOW = datetime(2026, 11, 9, 18, 0, tzinfo=timezone.utc).timestamp()
HEAD, NEW = "a" * 40, "b" * 40
URL = "https://github.com/acme/api/pull/7"


def job(**kw):
    return {"key": "api#7", "seen_at": int(NOW - 600), "status": "idle",
            "head": {"sha": HEAD, "at": int(NOW - 3600)}, **kw}


def rec(head=HEAD, **kw):
    return {"repo": "api", "number": 7, "head": head, "draft": "", **kw}


class RowStateTest(unittest.TestCase):
    def test_hidden_while_the_first_draft_is_being_written(self):
        self.assertIsNone(A.row_state(job(status="running"), None, HEAD, None, NOW))

    def test_ready_once_a_draft_covers_the_head(self):
        self.assertEqual(A.row_state(job(status="done"), rec(), HEAD, None, NOW), A.READY)

    def test_a_failed_draft_shows_at_once(self):
        self.assertEqual(A.row_state(job(status="failed", run_head=HEAD), None, HEAD, None, NOW),
                         A.FAILED)

    def test_no_draft_after_the_fallback(self):
        late = job(status="running", seen_at=int(NOW - 46 * 60))
        self.assertEqual(A.row_state(late, None, HEAD, None, NOW, 45), A.NO_DRAFT)

    def test_a_review_the_scheduler_has_not_seen_uses_the_request_time(self):
        self.assertIsNone(A.row_state(None, None, HEAD, "2026-11-09T17:50:00Z", NOW))
        self.assertEqual(A.row_state(None, None, HEAD, "2026-11-08T17:50:00Z", NOW), A.NO_DRAFT)

    def test_an_outdated_draft_stays_hidden_until_announced(self):
        fresh = job(status="idle", seen_at=int(NOW - 60))
        self.assertIsNone(A.row_state(fresh, rec(), NEW, None, NOW))
        self.assertEqual(A.row_state({**fresh, "announced_at": int(NOW - 30)}, rec(), NEW,
                                     None, NOW), A.OUTDATED)

    def test_a_failed_update_says_failed_only_for_the_head_it_failed_on(self):
        failed = job(status="failed", run_head=NEW, announced_at=1)
        self.assertEqual(A.row_state(failed, rec(), NEW, None, NOW), A.FAILED)
        newer = "c" * 40
        self.assertEqual(A.row_state(failed, rec(), newer, None, NOW), A.OUTDATED)


class ShownTest(unittest.TestCase):
    def test_the_menu_waits_for_the_notification(self):
        st = [{"pr_url": URL, "head_sha": HEAD, "waiting_since": "2026-11-01T00:00:00Z"}]
        done = job(status="done", seen_at=int(NOW - 600))
        recs = {"api#7": rec()}
        self.assertEqual(A.states(st, {"api#7": done}, recs, NOW)[URL]["state"], A.READY)
        self.assertIsNone(A.states(st, {"api#7": done}, recs, NOW, menu=True)[URL]["state"])
        announced = {**done, "announced_at": int(NOW - 5)}
        self.assertEqual(A.states(st, {"api#7": announced}, recs, NOW, menu=True)[URL]["state"],
                         A.READY)

    def test_past_the_fallback_or_unhandled_it_shows(self):
        self.assertTrue(A.shown(None, NOW))
        self.assertTrue(A.shown(job(seen_at=int(NOW - 46 * 60)), NOW, 45))
        self.assertFalse(A.shown(job(seen_at=int(NOW - 60)), NOW, 45))


class NextStepTest(unittest.TestCase):
    def test_a_review_with_no_draft_starts_a_full_one(self):
        self.assertEqual(A.next_step(None, None, HEAD, NOW), "full")
        self.assertEqual(A.next_step(job(), None, HEAD, NOW), "full")

    def test_nothing_while_running_or_current(self):
        with mock.patch.object(A, "_alive", return_value=True):
            self.assertIsNone(A.next_step(job(status="running", pid=1), None, HEAD, NOW))
        self.assertIsNone(A.next_step(job(status="done"), rec(), HEAD, NOW))

    def test_a_failed_draft_retries_only_on_a_new_quiet_head(self):
        failed = job(status="failed", run_head=HEAD)
        self.assertIsNone(A.next_step(failed, None, HEAD, NOW))
        moved = {**failed, "head": {"sha": NEW, "at": int(NOW - 60)}}
        self.assertIsNone(A.next_step(moved, None, NEW, NOW))
        quiet = {**failed, "head": {"sha": NEW, "at": int(NOW - 11 * 60)}}
        self.assertEqual(A.next_step(quiet, None, NEW, NOW), "full")

    def test_new_commits_are_checked_at_once_and_updated_once_quiet(self):
        pushed = job(status="done", head={"sha": NEW, "at": int(NOW - 60)})
        self.assertEqual(A.next_step(pushed, rec(), NEW, NOW), "check")
        checked = {**pushed, "checked_head": NEW}
        self.assertIsNone(A.next_step(checked, rec(), NEW, NOW))
        self.assertEqual(A.next_step(checked, rec(), NEW, NOW + 10 * 60), "update")

    def test_an_update_that_failed_on_this_head_is_not_retried(self):
        failed = job(status="failed", run_head=NEW, checked_head=NEW,
                     head={"sha": NEW, "at": int(NOW - 3600)})
        self.assertIsNone(A.next_step(failed, rec(), NEW, NOW))


class TooltipTest(unittest.TestCase):
    def test_cost_by_kind(self):
        r = rec(history=[{"kind": "full", "cost_usd": 3.1}, {"kind": "carried", "cost_usd": 0},
                         {"kind": "update", "cost_usd": 0.4}])
        self.assertEqual(A.tooltip(r, None), "Draft cost $3.50 (full review $3.10, 1 update $0.40)")

    def test_failure_reason(self):
        self.assertIn("Last run failed: boom", A.tooltip(None, job(status="failed", error="boom")))


class ClassifyTest(unittest.TestCase):
    def st(self, **kw):
        return {"pr_url": URL, "title": "ABC-7: thing", "author": "dev", "bucket": "needs_you",
                "tier": 1, "waiting_since": "2026-11-04T18:00:00Z", "approvals": ["mykal"],
                "engaged": ["mykal"], **kw}

    def classify(self, drafts, **kw):
        reviews = {"needs_you": [self.st(**kw)], "author": [], "well_approved": [], "done": [],
                   "unreadable": []}
        return P.classify([], reviews, [], nick={"mykal": "Mykal"},
                          now=datetime.fromtimestamp(NOW, timezone.utc), drafts=drafts)

    def test_the_row_says_the_draft_state_and_the_why_names_who_approved(self):
        [e] = self.classify({URL: {"state": A.READY, "tooltip": "Draft cost $3.00"}})
        self.assertEqual(e["reason"], "dev opened it · draft ready · 1 of 2 approvals · 5d")
        self.assertIn("1 approval (Mykal)", e["why"])
        self.assertEqual(e["tooltip"], "Draft cost $3.00")

    def test_no_approvals(self):
        [e] = self.classify({URL: {"state": A.NO_DRAFT}}, approvals=[], engaged=[])
        self.assertEqual(e["reason"], "dev opened it · no draft · 0 of 2 approvals · 5d")

    def test_a_review_without_a_state_has_no_row(self):
        self.assertEqual(self.classify({URL: {"state": None}}), [])
        self.assertEqual(self.classify({}), [])

    def test_off_leaves_the_row_as_it_was(self):
        [e] = self.classify(None)
        self.assertEqual(e["reason"], "dev opened it · 1 of 2 approvals · 5d")


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [mock.patch.object(A, "DRAFTS_STATE", root / "drafts"),
                        mock.patch.object(record, "INDEX_DIR", root / "reviews")]
        for p in self.patches:
            p.start()
        self.root = root

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_jobs_round_trip(self):
        A.save_job(job())
        self.assertEqual(A.load_job("api#7")["status"], "idle")
        self.assertEqual(list(A.all_jobs()), ["api#7"])

    def test_carry_forward_moves_the_head_and_records_it(self):
        draft = self.root / "ABC-7-api.md"
        draft.write_text("Reviewer: x\n")
        r = rec(draft=str(draft), history=[{"kind": "full", "to": HEAD, "cost_usd": 3}])
        delta = {"new_merge_base": "m" * 40, "new_fingerprint": {"sha": "f", "files": {"x": "1"}}}
        out = A.carry_forward(r, NEW, delta)
        self.assertEqual(out["head"], NEW)
        self.assertEqual(out["history"][-1]["kind"], "carried")
        self.assertEqual(record.load("api", 7)["head"], NEW)
        self.assertEqual(json.loads(draft.with_suffix(".json").read_text())["head"], NEW)

    def test_start_records_the_run(self):
        popen = mock.Mock(return_value=mock.Mock(pid=4242))
        j = job()
        A.start(j, "full", HEAD, {}, NOW, popen=popen)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[1:], ["-m", "watcher", "draft-run", "api#7"])
        saved = A.load_job("api#7")
        self.assertEqual((saved["status"], saved["kind"], saved["pid"]), ("running", "full", 4242))


class RunJobTest(unittest.TestCase):
    """The detached run's routing: carry forward, update, or full review."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [mock.patch.object(A, "DRAFTS_STATE", root / "drafts"),
                        mock.patch.object(record, "INDEX_DIR", root / "reviews"),
                        mock.patch.object(A, "_try_pr_diff", lambda *a: None),
                        mock.patch.object(A.interdiff, "fetch", lambda *a: "tip")]
        for p in self.patches:
            p.start()
        record.save({"repo": "api", "number": 7, "head": HEAD, "draft": ""})
        A.save_job({**job(status="running", kind="update", run_head=NEW),
                    "pr": {"url": URL, "repo": "api", "number": 7, "base": "main"}})

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def delta(self, **kw):
        base = {"empty": False, "files": {"a.py": ""}, "lines": 5, "pr_lines": 100,
                "new_files": [], "commits": 1, "method": "replay", "new_merge_base": "m",
                "new_fingerprint": {"sha": "s", "files": {}}}
        return {**base, **kw}

    def run_job(self, delta=None, compute_error=None):
        from review import review_pr, update_review
        compute = mock.Mock(return_value=delta, side_effect=compute_error)
        full = mock.Mock(return_value=[])
        upd = mock.Mock(return_value={"history": [{"summary": "Since x: no new issues."}]})
        with mock.patch.object(A.interdiff, "compute", compute), \
                mock.patch.object(review_pr, "review", full), \
                mock.patch.object(update_review, "run", upd), \
                mock.patch.object(update_review, "fetch_threads", lambda *a: []), \
                mock.patch("names.book", lambda fetch=False: {}):
            rc = A.run_job("api#7", cfg())
        return rc, full, upd, A.load_job("api#7")

    def test_a_small_delta_gets_an_update_review(self):
        rc, full, upd, j = self.run_job(self.delta())
        self.assertEqual((rc, j["status"], j["outcome"]), (0, "done", "Since x: no new issues."))
        upd.assert_called_once()
        full.assert_not_called()

    def test_an_empty_delta_carries_the_draft_forward(self):
        rc, full, upd, j = self.run_job(self.delta(empty=True, files={}, lines=0))
        self.assertEqual(record.load("api", 7)["head"], NEW)
        full.assert_not_called()
        upd.assert_not_called()

    def test_a_new_file_gets_a_full_review(self):
        rc, full, upd, j = self.run_job(self.delta(new_files=["b.py"]))
        full.assert_called_once()
        self.assertIn("outside what was reviewed", j["outcome"])

    def test_an_uncomputable_delta_gets_a_full_review(self):
        rc, full, upd, j = self.run_job(compute_error=A.interdiff.InterdiffError("gone"))
        full.assert_called_once()
        self.assertIn("can't be computed", j["full_because"])

    def test_a_failure_is_recorded(self):
        rc, full, upd, j = self.run_job(compute_error=None, delta=self.delta())
        self.assertEqual(rc, 0)
        from review import update_review
        with mock.patch.object(A.interdiff, "compute", return_value=self.delta()), \
                mock.patch.object(update_review, "fetch_threads", side_effect=RuntimeError("boom")), \
                mock.patch("names.book", lambda fetch=False: {}):
            j2 = {**A.load_job("api#7"), "status": "running", "kind": "update"}
            A.save_job(j2)
            record.save({"repo": "api", "number": 7, "head": HEAD, "draft": ""})
            with mock.patch("sys.stderr"):
                self.assertEqual(A.run_job("api#7", cfg()), 1)
        self.assertEqual((A.load_job("api#7")["status"], A.load_job("api#7")["error"]),
                         ("failed", "boom"))


def cfg(**review):
    c = settings._merge(settings.DEFAULTS, {"me": "me", "review": {"auto_draft": True, **review},
                                            "triage": {"pod": ["pal"]}})
    c["repos_root"] = "/nonexistent"
    return c


class TickTest(unittest.TestCase):
    """The pass, with the GitHub search, the cached triage and the runs stood in for."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        self.started = []
        self.cache = {"reviews": {"needs_you": [], "author": [], "well_approved": [],
                                  "done": [], "unreadable": []}}
        self.patches = [
            mock.patch.object(A, "DRAFTS_STATE", root / "drafts"),
            mock.patch.object(A, "MANUAL_STATE", root / "manual"),
            mock.patch.object(record, "INDEX_DIR", root / "reviews"),
            mock.patch.object(A, "cached_triage", lambda: (self.cache, 10)),
            mock.patch.object(A, "requested_heads", lambda c: {}),
            mock.patch.object(A, "refresh_triage", lambda: False),
            mock.patch.object(A, "start", self.fake_start),
            mock.patch("watcher.followup.all_jobs", lambda: []),
            mock.patch("names.book", lambda fetch=False: {}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def fake_start(self, j, kind, head, c, now, state_dir=None, popen=None):
        j.update(status="running", kind=kind, run_head=head, pid=999999)
        A.save_job(j)
        self.started.append((j["key"], kind))

    def add(self, n, bucket="needs_you", **kw):
        st = {"pr_url": f"https://github.com/acme/api/pull/{n}", "title": f"ABC-{n}: t",
              "author": "dev", "bucket": bucket, "tier": 1, "head_sha": HEAD,
              "waiting_since": "2026-11-09T17:00:00Z", "approvals": [], **kw}
        self.cache["reviews"][bucket].append(st)
        return st

    def tick(self, now=NOW, **kw):
        notes = []
        A.tick(cfg(), now=now, deliver=notes.extend, log=lambda m: None, **kw)
        return notes

    def test_every_owed_review_starts_in_ranked_order_and_stays_quiet(self):
        self.add(3)
        self.add(1)
        self.add(2, bucket="well_approved", author="pal", approvals=["x", "y"])
        self.add(4, bucket="well_approved", approvals=["x", "y"])   # 2 approvals, not pod
        notes = self.tick()
        self.assertEqual(self.started, [("api#3", "full"), ("api#1", "full"), ("api#2", "full")])
        self.assertEqual(notes, [])

    def test_the_popup_comes_once_with_the_draft(self):
        self.add(1)
        self.tick()
        draft = self.root / "ABC-1-api.md"
        draft.write_text("Reviewer: x\n")
        record.save({"repo": "api", "number": 1, "head": HEAD, "draft": str(draft),
                     "history": [{"kind": "full", "cost_usd": 3.3}]})
        j = A.load_job("api#1")
        j.update(status="done")
        A.save_job(j)
        [note] = self.tick(NOW + 120)
        self.assertEqual(note["state"], A.READY)
        self.assertEqual(note["reason"], "dev opened it · draft ready · 0 of 2 approvals · today")
        self.assertEqual(note["draft"], str(draft))
        self.assertEqual(note["color"], P.GREEN)
        self.assertEqual(self.tick(NOW + 240), [])

    def test_a_late_draft_arrives_as_no_draft(self):
        self.add(1)
        self.tick()
        with mock.patch.object(A, "_alive", return_value=True):
            self.assertEqual(self.tick(NOW + 44 * 60), [])
            [note] = self.tick(NOW + 46 * 60)
        self.assertEqual(note["state"], A.NO_DRAFT)
        self.assertIsNone(note["draft"])

    def test_a_dead_run_counts_as_failed_and_shows(self):
        self.add(1)
        self.tick()
        [note] = self.tick(NOW + 120)
        self.assertEqual(note["state"], A.FAILED)
        self.assertEqual(A.load_job("api#1")["error"], "the draft process died")

    def test_a_request_the_triage_lacks_refreshes_it_at_most_every_few_minutes(self):
        self.add(1)
        calls = []
        unknown = {"https://github.com/acme/api/pull/99": {"head": NEW}}
        with mock.patch.object(A, "requested_heads", lambda c: unknown), \
                mock.patch.object(A, "refresh_triage", lambda: calls.append(1) or True):
            self.tick()
            self.assertEqual(calls, [])
            with mock.patch.object(A, "cached_triage", lambda: (self.cache, A.NEW_REQUEST_AGE + 1)):
                self.tick()
        self.assertEqual(calls, [1])

    def test_limit_and_only(self):
        self.add(1)
        self.add(2)
        self.tick(limit=1)
        self.assertEqual(self.started, [("api#1", "full")])
        self.tick(only="https://github.com/acme/api/pull/2")
        self.assertEqual(self.started[-1], ("api#2", "full"))

    def test_a_menu_review_in_flight_isnt_doubled(self):
        self.add(1)
        (self.root / "manual").mkdir()
        (self.root / "manual" / "inflight-api#1").write_text("1")
        with mock.patch.object(A, "_alive", return_value=True):
            self.tick()
        self.assertEqual(self.started, [])

    def test_new_commits_with_an_empty_interdiff_keep_the_draft_ready(self):
        self.add(1, head_sha=NEW)
        record.save({"repo": "api", "number": 1, "head": HEAD, "draft": ""})
        A.save_job({"key": "api#1", "seen_at": int(NOW - 3600), "status": "done",
                    "announced_at": int(NOW - 3000), "head": {"sha": HEAD, "at": int(NOW - 3600)}})

        def check(j, r, pr, head, c, index_dir=None):
            A.carry_forward(r, head, {"new_merge_base": "m", "new_fingerprint": {"sha": "s", "files": {}}})
            return "carried", None
        with mock.patch.object(A, "check", check):
            self.assertEqual(self.tick(), [])
        self.assertEqual(record.load("api", 1)["head"], NEW)
        self.assertEqual(self.started, [])
        st = self.cache["reviews"]["needs_you"]
        self.assertEqual(A.states(st, A.all_jobs(), A.records_for(st), NOW)[st[0]["pr_url"]]["state"],
                         A.READY)

    def test_new_commits_that_change_the_pr_start_an_update_once_quiet(self):
        self.add(1, head_sha=NEW)
        record.save({"repo": "api", "number": 1, "head": HEAD, "draft": ""})
        A.save_job({"key": "api#1", "seen_at": int(NOW - 3600), "status": "done",
                    "announced_at": int(NOW - 3000), "head": {"sha": HEAD, "at": int(NOW - 3600)}})
        with mock.patch.object(A, "check", lambda *a, **k: ("changed", None)):
            self.tick()
            self.assertEqual(self.started, [])
            self.tick(NOW + 11 * 60)
        self.assertEqual(self.started, [("api#1", "update")])
        st = self.cache["reviews"]["needs_you"]
        self.assertEqual(A.states(st, A.all_jobs(), A.records_for(st), NOW)[st[0]["pr_url"]]["state"],
                         A.OUTDATED)


if __name__ == "__main__":
    unittest.main()
