#!/usr/bin/env python
"""Replay the alarm rules over the stored record: what WOULD have fired?

Nothing is sent and nothing is written: the database is opened read-only (SQLite refuses a
write). Run it before switching delivery on, and after changing a threshold, so thresholds are
tuned against the record instead of by waiting a month for alarms. The live evaluation runs
the same code (src/hvactrend/rules/).

    python scripts/rules_backtest.py                       # the last 30 days, summarised
    python scripts/rules_backtest.py --days 7 --list       # every episode
    python scripts/rules_backtest.py --rule boiler-rise --list
"""

from __future__ import annotations

import argparse
import collections
import os
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
os.chdir(_REPO)

from hvactrend.config import load_config  # noqa: E402
from hvactrend.rules import evaluate as ev  # noqa: E402
from hvactrend.rules.group import group  # noqa: E402
from hvactrend.storage.sqlite_dal import SQLiteDAL  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--days", type=float, default=30.0)
    ap.add_argument("--rule", action="append", help="only this rule (repeatable)")
    ap.add_argument("--list", action="store_true", help="every episode, not only the summary")
    args = ap.parse_args()

    cfg = load_config(args.config)
    dal = SQLiteDAL(cfg.storage.path, read_only=True)
    end = int(time.time())
    start = end - int(args.days * 86400)
    t0 = time.monotonic()
    found = ev.evaluate(dal, cfg.get("rules"), start, end, knowledge=cfg.get("site_knowledge"),
                        only=set(args.rule) if args.rule else None)
    took = time.monotonic() - t0

    print(f"{len(found)} episode(s) over {args.days:g} days "
          f"({time.strftime('%m-%d %H:%M', time.localtime(start))} to now), {took:.1f} s")
    by = collections.defaultdict(list)
    for f in found:
        by[(f.rule, f.tier)].append(f)
    for (rule, tier), fs in sorted(by.items()):
        units = collections.Counter(f.unit for f in fs)
        still = sum(1 for f in fs if f.episode.end is None)
        print(f"  {rule:16} {tier:8} {len(fs):4}  ({len(fs) / args.days:.2f}/day, {still} still "
              f"open)  " + ", ".join(f"{u} x{n}" for u, n in sorted(units.items())))
    groups = group(found)
    tiers = collections.Counter((g.kind, g.tier) for g in groups)
    print()
    print(f"As MESSAGES (findings opening within 30 min on 3+ units are one): {len(groups)}, "
          f"{len(groups) / args.days:.2f}/day")
    for (kind, tier), n in sorted(tiers.items()):
        print(f"  {kind:9} {tier:8} {n:4}  ({n / args.days:.2f}/day)")
    if args.list:
        print()
        for g in groups:
            when = time.strftime("%a %m-%d %H:%M", time.localtime(g.opened))
            print(f"  [{g.tier:8}] {when}  {g.summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
