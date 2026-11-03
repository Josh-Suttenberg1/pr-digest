from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from review import verifier_stats as vs


def record(run, ran=True, refuted=0, changed=False, cost=1.0, key="ABC-1", number=1):
    v = {"ran": ran}
    if ran:
        v.update(confirmed=3, refuted=refuted, unverifiable=0, changed_review=changed, cost_usd=cost,
                 findings_before={}, findings_after={}, duration_s=60)
    return {"run_dir": run, "key": key, "repo": "ui", "number": number, "verification": v}


class SummarizeTests(unittest.TestCase):
    def test_story_runs_count_once_and_rates_are_per_verified_review(self):
        with tempfile.TemporaryDirectory() as d:
            drafts = Path(d)
            rows = {
                "a-ui.json": record("run1", refuted=1, changed=True, cost=2.0),
                "a-processservice.json": record("run1", refuted=1, changed=True, cost=2.0),
                "b-ui.json": record("run2", cost=1.0, number=2),
                "c-ui.json": record("run3", ran=False, number=3),
                "old-ui.json": {"key": "ABC-9", "repo": "ui", "number": 9},
            }
            for name, data in rows.items():
                (drafts / name).write_text(json.dumps(data))
            out = vs.summarize(vs.load_runs(drafts))
        self.assertEqual((out["reviews"], out["verifier_ran"], out["verifier_skipped"]), (3, 2, 1))
        self.assertEqual((out["refuted"], out["reviews_changed"], out["changed_rate"]), (1, 1, 0.5))
        self.assertEqual((out["verifier_cost_usd"], out["cost_per_changed_review_usd"]), (3.0, 3.0))

    def test_nothing_verified_yet(self):
        out = vs.summarize([])
        self.assertIsNone(out["changed_rate"])
        self.assertIsNone(out["cost_per_changed_review_usd"])


if __name__ == "__main__":
    unittest.main()
