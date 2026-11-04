#!/usr/bin/env python
"""Commission the building: enumerate its controllers and group their points into units.

Reads the `equipment` map from config/config.yaml (see docs/commissioning.md and
src/hvactrend/commissioning.py):
  * point_systems:  points of one controller assigned by object-name glob;
  * device_systems: a whole controller is one unit (an AHU's own controller);
  * sc_dac_map:     a Trane Tracer SC, whose points carry a |dac-N suffix per unit.

Enumeration uses batched ReadPropertyMultiple (a large supervisory controller can hold
thousands of objects). Read-only; idempotent: re-run it after changing the map, and it
refreshes the inventory and the grouping while keeping your selections. Nothing is enabled
for logging here: you choose that on the Point Selection page.

    python scripts/commission.py                 # every controller the map names
    python scripts/commission.py --only 1001     # one device instance

Stop the HVACTrend server first: this binds the BACnet port, and only one program can.
"""

import argparse
import asyncio
import logging
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hvactrend import commissioning  # noqa: E402
from hvactrend.bacnet.application import (  # noqa: E402
    BACnetConfigError,
    ReadOnlyBACnetApplication,
)
from hvactrend.bacnet.discovery import (  # noqa: E402
    dac_number,
    enumerate_device_fast,
    to_model_device,
    to_model_point,
    who_is_sweep,
)
from hvactrend.config import ConfigError, load_config  # noqa: E402
from hvactrend.safety import assert_read_only_default  # noqa: E402
from hvactrend.storage.sqlite_dal import SQLiteDAL  # noqa: E402


def _natural(label: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", label)]


async def _main(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    assert_read_only_default(cfg)

    eq_node = cfg.get("equipment")
    equipment = eq_node.as_dict() if eq_node is not None else {}
    targets = [args.only] if args.only else commissioning.targets(equipment)
    if not targets:
        print("The `equipment` map in the config names no controller yet. Run "
              "scripts/discover.py to list them, then fill it in (docs/commissioning.md).")
        return 2

    dal = SQLiteDAL(cfg.storage.path, cfg.get("storage").get("schema_file"))
    dal.apply_schema()
    site = cfg.get("site")
    site_id = dal.ensure_site(site.get("code", "SITE"), site.get("name", "Site"),
                              site.get("timezone", "America/New_York"))
    b = equipment.get("building", {})
    building_id = dal.ensure_building(site_id, b.get("code", "MAIN"),
                                      b.get("name", site.get("name", "Main")))

    sys_ids: dict[str, int] = {}
    for label, kind in sorted(commissioning.all_systems(equipment).items(),
                              key=lambda kv: _natural(kv[0])):
        sys_ids[label] = dal.ensure_system(building_id, label, label, kind)

    app = await ReadOnlyBACnetApplication.create(cfg)
    try:
        await asyncio.sleep(0.3)
        print(f"sweeping to locate {len(targets)} target device(s)...", flush=True)
        found = {d.device_instance: d for d in await who_is_sweep(app, timeout=args.timeout)}
        now = int(time.time())

        for inst in targets:
            dd = found.get(inst)
            if dd is None:
                print(f"  ! device {inst} did not answer Who-Is; skipping", flush=True)
                continue
            print(f"enumerating {inst} @ {dd.address} (batched RPM)...", flush=True)
            t0 = time.monotonic()
            objs = await enumerate_device_fast(app, dd, batch_size=args.batch)
            dev_id = dal.upsert_device(to_model_device(dd, site_id=site_id, created_utc=now))

            counts: dict[str, int] = {}
            for o in objs:
                label, _kind = commissioning.system_for(inst, o.name, equipment,
                                                        dac_number(o.name))
                sid = sys_ids.get(label) if label else None
                dal.upsert_point(to_model_point(o, device_id=dev_id, system_id=sid))
                key = label or "(not placed by the map)"
                counts[key] = counts.get(key, 0) + 1

            dt = time.monotonic() - t0
            summary = ", ".join(f"{k}: {v}" for k, v in
                                sorted(counts.items(), key=lambda kv: _natural(kv[0])))
            print(f"  {len(objs)} objects in {dt:.0f}s -> {summary}", flush=True)

        # classify semantic roles on the freshly-cached inventory (local DB only)
        from hvactrend.ai.roles import assign_roles
        r = assign_roles(dal)
        print(f"roles: {r['classified']}/{r['total']} classified, "
              f"{r['canonical']} canonical", flush=True)
    finally:
        await app.close()
        dal.close()
    print("\nNext: start the server and choose what to log on the Point Selection page.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Enumerate the building's controllers and group "
                                            "their points into units.")
    p.add_argument("--config", default="config/config.yaml")
    p.add_argument("--only", type=int, default=None, help="commission a single device instance")
    p.add_argument("--timeout", type=float, default=5.0, help="Who-Is timeout seconds")
    p.add_argument("--batch", type=int, default=10, help="objects per RPM batch")
    args = p.parse_args()

    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return asyncio.run(_main(args))
    except (BACnetConfigError, ConfigError, FileNotFoundError) as exc:
        print(f"\nConfig error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
