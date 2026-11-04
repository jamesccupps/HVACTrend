#!/usr/bin/env python
"""Auto-select the trend-worthy points per equipment system.

Enables (`points.enabled=1`) the operationally-important points — temps, setpoints,
static pressure, fan/VFD speed + status, compressors, heating/cooling valves,
dampers, humidity/enthalpy, flows, refrigerant/condenser (HV1), and key alarms/
status — while excluding the internal PID/gain/counter/calc/reset noise and static
config setpoints/limits. Names are normalized so one keyword set spans the Trane
(verbose English), Symbio (AC-4), and JCI (HV1 cryptic tags) conventions.

Purely a local DB operation — no BACnet writes. Refine further in the browser.

⚠️ IT SEEDS; IT DOES NOT RE-CURATE. `--commit` used to RECONCILE every system to the name
heuristic: enable what it picks, DISABLE everything else. Measured 2026-09-29 against the live
building, that would have silently stopped logging 262 of 664 hand-curated points and switched
on 213 others -- among them the dead sensors pruned by hand (HV1 OA_TEMP / ZONE_TEMP stuck at
-20, absent compressors C-F). DEPLOYMENT.md listed it as a commissioning step. Now:

    python scripts/select_trend_points.py                         # dry run, every system
    python scripts/select_trend_points.py --commit                # seed NEW systems only
    python scripts/select_trend_points.py --system AC-1           # dry run, one system
    python scripts/select_trend_points.py --system AC-1 --commit  # ADD its picks, remove nothing
    python scripts/select_trend_points.py --system AC-1 --commit --prune   # full reconcile
    python scripts/select_trend_points.py --show                  # ...plus the point names

A system is NEW when none of its points is enabled. A bare `--commit` never touches a system
someone has already curated; changing one is an explicit, one-system-at-a-time act, and
disabling anything additionally requires --prune, which prints every point it turns off.
2026-09-29 audit, A1.
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hvactrend.config import load_config  # noqa: E402
from hvactrend.storage.sqlite_dal import SQLiteDAL  # noqa: E402

# Matched against a normalized name (underscores / ~ / - / punctuation -> spaces),
# so 'air temp' hits both "Discharge Air Temperature" and "SUP_AIR_TEMP".
INCLUDE = (
    # temperatures
    "air temp", "oa temp", "zone temp", "room temp", "space temp", "flr temp",
    "water temp", "wtr temp", "dewp", "dew pnt", "evap air",
    # active setpoints (compare actual vs target)
    "air setpoint", "temperature setpoint active", "sat sp", "act sat",
    # pressures (process, not config limits)
    "static pressure", "stat pr", "duct static", "space pressure", "act dsp",
    # fans / vfd
    "vfd", "fan speed", "fan status", "run status", "fan failure", "fan proof",
    "supply fan", "exhaust fan", "sup fan",
    # dampers
    "damper", "dampr",
    # compressors / cooling
    "comp", "multistack", "cooling capacity", "cooling control", "cooling stag",
    # heating / valves
    "hw valve", "heating capacity", "heating valv", "heating stat", "heat stages",
    "valve", "valv", "wtr val", "reheat",
    # economizer / air quality
    "econ", "enthalpy", "humid", "air rh",
    # flow
    "flow", "flw", "airfl",
    # refrigerant / condenser (HV1)
    "prs", "disch", "sucton", "suction", "cond wtr", "cond prs", "bypass", "byp valv",
    # status / alarms / mode
    "alarm", "freeze", "filter", "diagnostic", "deviation", "smoke", "fan s s",
    "unit stat", "unit mode", "supply stat", "vent stat", "heat cool mode",
    "occupancy status", "occ state", "occ mode", "flow sw",
)

EXCLUDE = (
    # internal / tuning / diagnostics-of-diagnostics
    "pid", "pgain", "p gain", "counter", "calc", "protection", "feedback",
    "reset", "rst", "test", "override", "manual", "rotate", "delay", "bypass time",
    # commands / config echoes (we trend actuals + active setpoints, not commands)
    "bas", "enable", "cmd", "request", "curr bas",
    # static config setpoints / limits
    "high limit", "low limit", "high sp", "low sp", "hi sp", "lo sp",
    "spmax", "spmin", "sp max", "sp min", "damp max", "damp min",
    "occupied cool", "occupied heat", "unoccupied", "standby", "uocc",
    "zn cool", "zn heat", "rat cool", "rat heat", "stg 1 c", "stg 2 c",
    "installed", "method",
    # granular mode/ready clutter
    "rdy", "night", "morn", "warm up", "comfort", "slab", "setbac",
    # ambiguous identically-named compressor counters (Trane "MS Comp N Runs" x8) —
    # compressor operation is already covered by capacity/staging/lead-compressor.
    "runs", "instal", "max bypass",
    # operator curation (verified vs Desigo): drop diagnostics, capacity/control
    # outputs, sequencing indices, command-output duplicates (_AOPR~/_BOPR~), and
    # a few known dead/duplicate sensors. See NOTE below.
    "diagnostic", "capacity status", "control output", "aopr", "bopr",
    "low outdoor air", "lead compressor", "lag compressor", "stage compressor",
    "supply fan speed status", "duct static pressure setpoint", "space static pressure",
    "heat cool mode", "outdoor air flow", "space humidity",
)

# NOTE: this name-based allow/deny list is the INITIAL SEED. The live selection was
# further curated against the operator's Desigo overview to prune DEAD sensors whose
# names look fine but whose data is garbage (e.g. HV1 OA_TEMP/ZONE_TEMP stuck at -20,
# absent compressors C-F). Those data-driven drops can't be expressed by name here, so
# `data/hvactrend.db` (points.enabled) is the source of truth. This note used to end
# "re-running --commit ... will re-enable those dead points; re-prune if you do" -- it
# documented the hazard instead of preventing it. A bare --commit now leaves every curated
# system alone; see the module docstring.


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[_~/\-.,()#]+", " ", (s or "").lower())).strip()


def is_selected(name: str) -> bool:
    n = _norm(name)
    if any(x in n for x in EXCLUDE):
        return False
    return any(x in n for x in INCLUDE)


def _base_key(name: str) -> str:
    base = (name or "").split("|", 1)[0].lower().replace("temperature", "temp")
    return re.sub(r"[^a-z0-9]+", " ", base).strip()


def _is_uip(name: str) -> bool:
    # Trane: the '|dac-N_UIP~_NN' suffix marks a Universal Input Point — the actual
    # wired physical sensor (vs a logical/derived duplicate that can read stale/faulted).
    suffix = (name or "").split("|", 1)[1] if "|" in (name or "") else ""
    return "uip" in suffix.lower()


def prefer_uip(points: list) -> list:
    """When a physical UIP sensor and a logical duplicate share a base measurement,
    keep ONLY the UIP. e.g. 'Discharge Air Temp|dac-3_UIP~_06' (real, wired) wins over
    'Discharge Air Temperature|dac-3' (logical). Non-Trane names (no '|', no UIP) are
    unaffected — HV1/AC-4 sensors are already the direct point."""
    groups: dict[str, list] = {}
    for p in points:
        groups.setdefault(_base_key(p["name"]), []).append(p)
    out = []
    for grp in groups.values():
        uips = [p for p in grp if _is_uip(p["name"])]
        out.extend(uips if uips else grp)
    return out


def plan(dal, system: "str | None" = None) -> list:
    """What the heuristic would do to each system. READ-ONLY.

    For every system: whether it is NEW (nothing enabled yet), what the heuristic selects,
    what it would ADD (selected but off), and what a --prune would REMOVE (on, but not
    selected -- which on a curated system means hand-picked)."""
    out = []
    for s in dal.list_systems():
        if system and s["code"] != system:
            continue
        pts = dal.points_for_system(s["id"], loggable_only=True)
        sel = prefer_uip([pt for pt in pts if is_selected(pt["name"])])
        sel_ids = {pt["id"] for pt in sel}
        out.append({
            "code": s["code"],
            "new": not any(pt["enabled"] for pt in pts),
            "loggable": len(pts),
            "selected": sel,
            "add": [pt for pt in sel if not pt["enabled"]],
            "remove": [pt for pt in pts if pt["enabled"] and pt["id"] not in sel_ids],
        })
    return out


def apply(dal, sysplan: dict, *, prune: bool) -> "tuple[list, list]":
    """Enable a system's additions and, only with prune, disable its removals."""
    for pt in sysplan["add"]:
        dal.set_point_enabled(pt["id"], True)
    removed = []
    if prune:
        for pt in sysplan["remove"]:
            dal.set_point_enabled(pt["id"], False)
            removed.append(pt)
    return sysplan["add"], removed


def main(argv: "list | None" = None) -> int:
    p = argparse.ArgumentParser(description="Seed trend points for newly discovered systems.")
    p.add_argument("--config", default="config/config.yaml")
    p.add_argument("--commit", action="store_true",
                   help="write: seed NEW systems (or add to --system); default is a dry run")
    p.add_argument("--prune", action="store_true",
                   help="with --system and --commit: ALSO disable points the heuristic "
                        "would not pick (a full reconcile of that one system)")
    p.add_argument("--show", action="store_true", help="list point names")
    p.add_argument("--system", default=None, help="only this system code (e.g. AC-1)")
    args = p.parse_args(argv)
    if args.prune and not args.system:
        p.error("--prune disables points; it only runs on ONE named system (--system X)")

    cfg = load_config(args.config)
    dal = SQLiteDAL(cfg.storage.path, cfg.get("storage").get("schema_file"))
    dal.apply_schema()

    added = removed = skipped = 0
    for sp in plan(dal, args.system):
        # A bare --commit seeds NEW systems only. A curated one changes only when named.
        writes = args.commit and (sp["new"] or bool(args.system))
        state = "new" if sp["new"] else "curated"
        drift = f"+{len(sp['add'])} to add, -{len(sp['remove'])} logged but not picked"
        if args.commit and not writes:
            skipped += 1
            print(f"  {sp['code']:<6} {state:<8} SKIPPED (curated; name it with --system "
                  f"to change it)   [{drift}]")
            continue
        if writes:
            a, r = apply(dal, sp, prune=args.prune)
            added += len(a)
            removed += len(r)
            print(f"  {sp['code']:<6} {state:<8} enabled {len(a)}"
                  + (f", DISABLED {len(r)}" if args.prune else ""))
            for pt in a if args.show else []:
                print(f"        + {pt['object_type']:<16} {pt['name']}")
            for pt in r:                                   # always listed, never silent
                print(f"        - {pt['object_type']:<16} {pt['name']}")
        else:
            print(f"  {sp['code']:<6} {state:<8} {len(sp['selected']):>4} / "
                  f"{sp['loggable']:<4} selected   [{drift}]")
            if args.show:
                for pt in sp["add"]:
                    print(f"        + {pt['object_type']:<16} {pt['name']}")
            if args.show or args.prune:
                for pt in sp["remove"]:
                    print(f"        - {pt['object_type']:<16} {pt['name']}   "
                          f"(would be DISABLED by --prune)")
    if args.commit:
        print(f"\nCOMMITTED: enabled {added}, disabled {removed}"
              + (f", left {skipped} curated system(s) untouched" if skipped else ""))
    else:
        print("\nDRY RUN -- nothing written. A bare --commit seeds only NEW systems.")
    dal.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
