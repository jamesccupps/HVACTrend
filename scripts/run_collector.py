#!/usr/bin/env python
"""Run the HVACTrend collector headless (no web UI).

Polls the ENABLED points every interval and writes samples with status/quality.
Read-only. This is the pure-historian entry point; the web app
(`uvicorn hvactrend.api.app:create_app`) runs its OWN collector on the shared
stack, so do NOT run both against the same NIC — they would double-bind the BACnet
port (guardrail #2).

    python scripts/run_collector.py            # run continuously (Ctrl+C to stop)
    python scripts/run_collector.py --once     # one poll cycle, then exit (smoke test)
"""

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
import os  # noqa: E402
os.chdir(_REPO)   # anchor to repo root (Task Scheduler cwd defaults to System32)

from hvactrend.bacnet.application import (  # noqa: E402
    BACnetConfigError,
    ReadOnlyBACnetApplication,
)
from hvactrend.collector.service import Collector  # noqa: E402
from hvactrend.config import ConfigError, load_config  # noqa: E402
from hvactrend.safety import assert_read_only_default  # noqa: E402
from hvactrend.storage.sqlite_dal import SQLiteDAL  # noqa: E402


def _report(dal: SQLiteDAL, dt: float) -> None:
    c = dal._conn
    total = c.execute("SELECT count(*) FROM samples").fetchone()[0]
    print(f"\npoll cycle completed in {dt:.1f}s — {total} samples in DB")
    print("quality breakdown:")
    for q, n in c.execute("SELECT quality, count(*) FROM samples GROUP BY quality ORDER BY 2 DESC"):
        print(f"  {q:<10} {n}")
    print("per-device poll health (latest cycle):")
    for r in c.execute(
            "SELECT d.device_instance, ph.reads_ok, ph.reads_attempted, "
            " ph.latency_ms, ph.method FROM poll_health ph "
            "JOIN devices d ON ph.device_id = d.id ORDER BY ph.id DESC LIMIT 12"):
        print(f"  dev {r[0]:<7} {r[1]:>3}/{r[2]:<3} ok  {r[3]:>5}ms  {r[4]}")


async def _main(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    assert_read_only_default(cfg)
    dal = SQLiteDAL(cfg.storage.path, cfg.get("storage").get("schema_file"))
    dal.apply_schema()

    app = await ReadOnlyBACnetApplication.create(cfg)
    collector = Collector(app, dal, cfg)
    try:
        await asyncio.sleep(0.3)  # let the endpoint finish binding
        targets = dal.poll_targets()
        print(f"{len(targets)} enabled points to poll.", flush=True)
        if args.once:
            t0 = time.monotonic()
            await collector._tick()
            _report(dal, time.monotonic() - t0)
        else:
            print("collector running (interval="
                  f"{collector._interval_s}s); Ctrl+C to stop.", flush=True)
            await collector.run()
    finally:
        await app.close()
        dal.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Run the HVACTrend collector.")
    p.add_argument("--config", default="config/config.yaml")
    p.add_argument("--once", action="store_true", help="one poll cycle, then exit")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return asyncio.run(_main(args))
    except (BACnetConfigError, ConfigError, FileNotFoundError) as exc:
        print(f"\nConfig error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ncollector stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
