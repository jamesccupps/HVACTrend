#!/usr/bin/env python
"""Rebuild the analysis model — semantic roles + learned baselines (plan §8, ported
from the old AHU program's domain model).

    python scripts/build_model.py                 # roles + baselines (30-day window)
    python scripts/build_model.py --roles-only    # just re-classify point roles
    python scripts/build_model.py --window-days 14

Run nightly under Task Scheduler AFTER the collector has accumulated data, so the
occupancy-partitioned normal bands (and the setpoint↔DAT / OAT↔MAT / fan↔pressure
correlations) stay current on a rolling window. Read-only wrt BACnet — pure reads of
stored samples + local DB writes. The pre-screen / reports pick up whatever is here.
"""

import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
import os  # noqa: E402
os.chdir(_REPO)   # anchor to repo root (Task Scheduler cwd defaults to System32)

from hvactrend.ai.baseline import build_baselines  # noqa: E402
from hvactrend.ai.limits import apply_plant_limits  # noqa: E402
from hvactrend.ai.roles import assign_roles  # noqa: E402
from hvactrend.config import ConfigError, load_config  # noqa: E402
from hvactrend.storage.sqlite_dal import SQLiteDAL  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Rebuild roles + learned baselines.")
    p.add_argument("--config", default="config/config.yaml")
    p.add_argument("--roles-only", action="store_true")
    p.add_argument("--window-days", type=int, default=None,
                   help="override analysis.baseline.window_days from config "
                        "(default: use the configured value, so the CLI, the UI button "
                        "and the scheduled rebuild all agree)")
    args = p.parse_args()

    try:
        cfg = load_config(args.config)
    except (ConfigError, FileNotFoundError) as exc:
        print(f"config error: {exc}\nCopy config/config.example.yaml to "
              f"config/config.yaml and fill the placeholders.", file=sys.stderr)
        return 2
    dal = SQLiteDAL(cfg.storage.path, cfg.get("storage").get("schema_file"))
    dal.apply_schema()
    try:
        r = assign_roles(dal)
        print(f"roles: {r['classified']}/{r['total']} classified, "
              f"{r['canonical']} canonical")
        top = sorted(r["distribution"].items(), key=lambda kv: -kv[1])[:8]
        print("       top: " + ", ".join(f"{k}={v}" for k, v in top))
        if args.roles_only:
            return 0
        lim = apply_plant_limits(dal, cfg)
        print(f"limits:    {lim['applied']} points limited from the plant's own "
              f"trended limits ({lim['alarming']} alarming, "
              f"{lim['limit_points']} limit points found)")
        b = build_baselines(dal, cfg, window_days=args.window_days)
        print(f"baselines: {b['points']} point bands, {b['corrs']} correlations "
              f"(window {b['window_days']}d)")
    finally:
        dal.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
