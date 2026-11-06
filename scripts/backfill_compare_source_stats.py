#!/usr/bin/env python3
"""Backfill market-compare source usage stats from site_runs (ROBIN-1906).

  .venv/bin/python scripts/backfill_compare_source_stats.py
  .venv/bin/python scripts/backfill_compare_source_stats.py --limit 50
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("TRADINGAGENTS_ROOT", str(ROOT))
os.environ.setdefault("TRADINGAGENTS_RUNS_DIR", str(ROOT / "data" / "site_runs"))

from tradingagents.market_compare.source_registry import backfill_from_runs  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None, help="Only scan the newest N run files")
    ap.add_argument(
        "--no-reset",
        action="store_true",
        help="Accumulate onto existing stats instead of resetting first",
    )
    args = ap.parse_args()
    result = backfill_from_runs(limit=args.limit, reset_stats=not args.no_reset)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
