#!/usr/bin/env python
"""Fetch + store outdoor weather for the report context.

    python scripts/fetch_weather.py

Pulls recent hourly weather (Open-Meteo, free/no-key) for the site coordinates in
config.weather.open_meteo, computes wet-bulb / dewpoint / enthalpy locally, and upserts
into the `weather` table. Run hourly under Task Scheduler alongside the collector.
Read-only: fetches public weather + writes the local DB (no BACnet).
"""

import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
import os  # noqa: E402
os.chdir(_REPO)   # anchor to repo root (Task Scheduler cwd defaults to System32)

from hvactrend import secrets_store  # noqa: E402
from hvactrend.ai.weather import update_weather  # noqa: E402
from hvactrend.config import ConfigError, load_config  # noqa: E402
from hvactrend.storage.sqlite_dal import SQLiteDAL  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Fetch + store outdoor weather.")
    p.add_argument("--config", default="config/config.yaml")
    args = p.parse_args()

    try:
        cfg = load_config(args.config)
    except (ConfigError, FileNotFoundError) as exc:
        print(f"config error: {exc}\nCopy config/config.example.yaml to "
              f"config/config.yaml and fill the placeholders.", file=sys.stderr)
        return 2
    secrets_store.load_into_env()          # TEMPEST_TOKEN for the cloud provider
    dal = SQLiteDAL(cfg.storage.path, cfg.get("storage").get("schema_file"))
    dal.apply_schema()
    try:
        res = update_weather(dal, cfg)
    finally:
        dal.close()
    if res.get("error"):
        print(f"weather: {res['error']}")
        return 1
    print(f"weather: stored {res['stored']} hourly obs from {res['source']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
