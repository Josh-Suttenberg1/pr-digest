"""Is pr-review-verifier earning its cost? Aggregates the review drafts' sidecars.

    review/verifier_stats.py

Every bin/review-pr run records in its draft's .json sidecar whether the verifier ran,
how many claims it confirmed, refuted or couldn't verify, the findings before and
after it, and what it cost. A review counts as "changed" when the verifier refuted or
couldn't verify a claim, or the findings differ after it. A story review writes the
same record into each PR's sidecar, so records are de-duplicated by run directory.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import settings  # noqa: E402


def load_runs(drafts_dir: Path) -> list[dict]:
    runs: dict[str, dict] = {}
    for path in sorted(drafts_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except ValueError:
            continue
        if "verification" not in record:
            continue
        runs[record.get("run_dir") or str(path)] = record
    return list(runs.values())


def summarize(runs: list[dict]) -> dict:
    ran = [r for r in runs if r["verification"]["ran"]]
    changed = [r for r in ran if r["verification"]["changed_review"]]
    cost = sum(r["verification"]["cost_usd"] for r in ran)
    totals = {k: sum(r["verification"][k] for r in ran) for k in ("confirmed", "refuted", "unverifiable")}
    return {
        "reviews": len(runs),
        "verifier_ran": len(ran),
        "verifier_skipped": len(runs) - len(ran),
        **totals,
        "reviews_changed": len(changed),
        "changed_rate": round(len(changed) / len(ran), 2) if ran else None,
        "verifier_cost_usd": round(cost, 2),
        "cost_per_changed_review_usd": round(cost / len(changed), 2) if changed else None,
        "changed": [f"{r['key']} {r['repo']}#{r['number']}: refuted {r['verification']['refuted']}, "
                    f"unverifiable {r['verification']['unverifiable']}" for r in changed],
    }


def main() -> int:
    cfg = settings.load()
    print(json.dumps(summarize(load_runs(settings.path_of(cfg["review"]["drafts_dir"]))), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
