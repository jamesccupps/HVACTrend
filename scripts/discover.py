#!/usr/bin/env python
"""Phase 1 discovery CLI — run the REAL Who-Is sweep on your BACnet network.

This is the tool that turns config placeholders into real device addresses. Run
it on the VLAN-5 collector desktop AFTER filling bacnet.local_address and
bacnet.device_instance in config/config.yaml.

    python scripts/discover.py                     # broadcast Who-Is on the bound NIC
    python scripts/discover.py --directed 192.0.2.5 # unicast Who-Is to one device
    python scripts/discover.py --devices-only      # sweep only, skip object reads
    python scripts/discover.py --no-cache          # dry run; don't write the DB

Read-only. It binds the one BACnet application; do NOT run it while the collector
service is already running (they would both bind the UDP port).
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hvactrend.bacnet.application import (  # noqa: E402
    BACnetConfigError,
    ReadOnlyBACnetApplication,
)
from hvactrend.bacnet.discovery import (  # noqa: E402
    identify_devices,
    to_model_device,
    who_is_sweep,
)
from hvactrend.config import ConfigError, load_config  # noqa: E402
from hvactrend.discovery_service import run_discovery  # noqa: E402
from hvactrend.safety import assert_read_only_default  # noqa: E402
from hvactrend.storage.sqlite_dal import SQLiteDAL  # noqa: E402


def _print_roster(devices, *, cached: bool) -> None:
    if not devices:
        print("\nNo devices answered Who-Is. Check the NIC/VLAN, or try --directed <ip>.")
        return
    tag = " (cached)" if cached else ""
    print(f"\n{len(devices)} device(s){tag}:")
    print(f"  {'instance':>9}  {'address':<16} {'net':>4}  {'vendor':<24} "
          f"{'name':<26} model")
    print("  " + "-" * 96)
    for d in sorted(devices, key=lambda x: (x.network_number or 0, x.device_instance)):
        vend = (f"{d.vendor_id if d.vendor_id is not None else '?'} "
                f"{d.vendor_name or ''}").strip()
        net = d.network_number if d.network_number is not None else ""
        print(f"  {d.device_instance:>9}  {d.address:<16} {str(net):>4}  "
              f"{vend[:24]:<24} {(d.object_name or '')[:26]:<26} {d.model_name or ''}")


def _print_results(results, *, cached: bool) -> None:
    if not results:
        print("\nNo devices answered Who-Is. Check the NIC/VLAN, or try --directed <ip>.")
        return
    for r in results:
        d = r.device
        tag = f"  [cached id={r.device_id}]" if cached and r.device_id else ""
        print(f"\n== device {d.device_instance} @ {d.address}"
              f"  vendor={d.vendor_id or '?'} {d.vendor_name or ''}"
              f" {d.model_name or ''}  rpm={d.rpm_supported}"
              f"{f' net={d.network_number}' if d.network_number else ''}{tag}")
        if not r.objects:
            continue
        print(f"    {'type':<18} {'inst':>5}  {'name':<24} {'units':<20} value")
        for o in r.objects:
            val = o.value_text if o.value_text is not None else (
                "" if o.value_num is None else f"{o.value_num:g}")
            flag = " *" if o.loggable else "  "
            err = f"  ERR:{o.error}" if o.error else ""
            print(f"  {flag}{o.object_type:<18} {o.object_instance:>5}  "
                  f"{(o.name or ''):<24} {(o.units_text or ''):<20} {val}{err}")
    print("\n( * = loggable point.  Nothing is enabled for logging yet - that's the "
          "selection step in the browser, plan sec 9.1.)")


async def _main(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    assert_read_only_default(cfg)

    dal = SQLiteDAL(cfg.storage.path, cfg.get("storage").get("schema_file"))
    dal.apply_schema()
    site = cfg.get("site")
    site_id = dal.ensure_site(site.get("code", "SITE"), site.get("name", "Site"),
                              site.get("timezone", "America/New_York"))

    app = await ReadOnlyBACnetApplication.create(cfg)
    try:
        await asyncio.sleep(0.3)  # let the endpoint finish binding
        if args.identify:
            import time
            devices = await who_is_sweep(app, address=args.directed, timeout=args.timeout)
            await identify_devices(app, devices)
            if not args.no_cache:
                now = int(time.time())
                for dd in devices:
                    dal.upsert_device(to_model_device(dd, site_id=site_id, created_utc=now))
            _print_roster(devices, cached=not args.no_cache)
        else:
            results = await run_discovery(
                app, None if args.no_cache else dal,
                site_id=site_id, address=args.directed, timeout=args.timeout,
                enumerate_objects=not args.devices_only, cache=not args.no_cache,
            )
            _print_results(results, cached=not args.no_cache)
    finally:
        await app.close()
        dal.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="HVACTrend Phase 1 BACnet discovery.")
    p.add_argument("--config", default="config/config.yaml")
    p.add_argument("--directed", default=None,
                   help="unicast Who-Is to a single address instead of broadcasting")
    p.add_argument("--timeout", type=float, default=3.0, help="Who-Is timeout seconds")
    p.add_argument("--devices-only", action="store_true",
                   help="Who-Is only; skip per-object enumeration")
    p.add_argument("--identify", action="store_true",
                   help="Who-Is + read each device's name/vendor/model (roster), no object reads")
    p.add_argument("--no-cache", action="store_true",
                   help="dry run; do not write the inventory DB")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return asyncio.run(_main(args))
    except (BACnetConfigError, ConfigError) as exc:
        print(f"\nConfig error: {exc}\n"
              f"Fill config/config.yaml (copy from config/config.example.yaml) first.",
              file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"\n{exc}\nCopy config/config.example.yaml to config/config.yaml and "
              f"fill the placeholders.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
