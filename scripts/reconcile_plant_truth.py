"""Check the program's hardcoded assumptions against what the plant actually publishes.

Read-only. Run after commissioning, after a controller is replaced, or whenever a report
quotes a threshold that looks wrong:

    python scripts/reconcile_plant_truth.py --config config/config.yaml

It answers three questions no code review can:
  1. Which enabled points fell through to CONFIG/OTHER and look like commissioned
     values? (the exact shape of the economizer-changeover bug — five instances so far)
  2. Which hardcoded constants has nobody verified against this building?
  3. For those, does the plant appear to trend its own version?
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hvactrend.ai import reconcile  # noqa: E402
from hvactrend.ai.roles import classify, unclassified_report  # noqa: E402
from hvactrend.config import ConfigError, load_config  # noqa: E402
from hvactrend.storage.sqlite_dal import SQLiteDAL  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config/config.yaml")
    args = ap.parse_args()
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    dal = SQLiteDAL(cfg.storage.path, cfg.get("storage").get("schema_file"))

    try:
        pts = dal.points_min()
        for p in pts:
            p["role"] = classify(p["name"])
        rep = unclassified_report(pts)

        print("=" * 78)
        print("1. ENABLED POINTS THAT LOOK LIKE COMMISSIONED VALUES BUT ARE INVISIBLE")
        print("=" * 78)
        cv = rep["commissioned_values"]
        if not cv:
            print("   none — every setpoint/limit-shaped name is classified.\n")
        else:
            print("   Each of these MAY be a value a detector is currently approximating")
            print("   with a hardcoded constant. That has happened five times.\n")
            for f in cv:
                print(f"   x{f['count']:<3d} [{f['role']:12s}] {f['name']}")
            print()

        print(f"   ({rep['invisible_points']} enabled points carry no analysis role at "
              f"all, {rep['invisible_names']} distinct names — usually status bits and")
        print("    cryptic BAS tags, but a real sensor can hide in here.)")
        for n in rep["sample"][:12]:
            print(f"      · {n}")
        print()

        print("=" * 78)
        print("2. HARDCODED CONSTANTS NOBODY HAS VERIFIED AGAINST THIS BUILDING")
        print("=" * 78)
        unv = reconcile.unverified()
        if not unv:
            print("   none — every constant is either wired to a plant point or "
                  "explicitly recorded as having no plant equivalent.\n")
        for name, mod, note in unv:
            print(f"   {name:26s} ({mod})")
            print(f"       {note}")
        print()

        print("=" * 78)
        print("3. POINTS THAT MAY BE THE PLANT'S OWN VERSION OF THOSE CONSTANTS")
        print("=" * 78)
        probes = reconcile.probe_plant(dal)
        if not probes:
            print("   no name matches. Absence of evidence only — the plant may publish")
            print("   these under an abbreviation the probe does not know.\n")
        for const, hits in probes.items():
            print(f"   {const}:")
            for h in hits:
                print(f"       {h['system'] or '?':6s} [{h['role'] or '-':16s}] {h['name']}")
        print()
        print("Nothing was changed. Wire a match by adding a role in ai/roles.py and")
        print("reading it in the detector, then move the constant to 'reads_plant'.")
    finally:
        dal.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
