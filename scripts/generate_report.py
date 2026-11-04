#!/usr/bin/env python
"""Generate the AI summary now, by hand.

    python scripts/generate_report.py --kind daily      # last 24h
    python scripts/generate_report.py --kind weekly     # last 7 days

Reads config/config.yaml, gathers what the alarm rules, the watchdog and the override read
found (ai/summary.py), calls the configured provider (Claude by default), and stores the
report and its actions in the DB. The server writes the scheduled ones itself
(config.ai.schedule); this is for a test or a re-run. The API key is read from
config/secrets.env, where the Settings page stores it. Read-only against BACnet: it reads
stored samples and never touches the network stack.

Exit code 0 on a complete report, 1 on a provider error (still recorded in the DB).
"""

import argparse
import asyncio
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
import os  # noqa: E402
os.chdir(_REPO)   # anchor to repo root (Task Scheduler cwd defaults to System32)

from hvactrend import secrets_store  # noqa: E402
from hvactrend.ai.reports import generate_report  # noqa: E402
from hvactrend.config import ConfigError, load_config  # noqa: E402
from hvactrend.storage.sqlite_dal import SQLiteDAL  # noqa: E402


async def _run(config_path: str, kind: str) -> dict:
    cfg = load_config(config_path)
    dal = SQLiteDAL(cfg.storage.path, cfg.get("storage").get("schema_file"))
    dal.apply_schema()
    try:
        return await generate_report(dal, cfg, kind)
    finally:
        dal.close()


def main() -> int:
    p = argparse.ArgumentParser(description="Write the AI summary now.")
    p.add_argument("--kind", choices=["daily", "weekly", "manual"], default="daily")
    p.add_argument("--config", default="config/config.yaml")
    args = p.parse_args()
    secrets_store.load_into_env()          # ANTHROPIC_API_KEY, as the server does

    try:
        res = asyncio.run(_run(args.config, args.kind))
    except (ConfigError, FileNotFoundError) as exc:
        print(f"\nConfig error: {exc}\nCopy config/config.example.yaml to "
              f"config/config.yaml and fill the placeholders.", file=sys.stderr)
        return 2
    status = res.get("status")
    print(f"[{args.kind}] report #{res.get('report_id')} status={status}, "
          f"{res.get('items', 0)} item(s) handed over")
    call = res.get("call") or {}
    if call:
        print(f"  call: stop_reason={call.get('stop_reason')} usage={call.get('usage')} "
              f"request_id={call.get('request_id')}")
    if status == "error":
        print(f"  ERROR: {res.get('error')}")
        print("  (check the key on the Settings page, the credit balance, or config.ai) "
              "-- report recorded as 'error'.")
        return 1
    print(f"  {res.get('headline')}")
    print(f"  actions kept: {len(res.get('actions') or [])}, dropped (cited nothing): "
          f"{res.get('dropped', 0)}; provider={res.get('provider')} model={res.get('model')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
