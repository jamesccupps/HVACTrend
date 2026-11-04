#!/usr/bin/env python
"""Try HVACTrend without a BACnet network: a made-up building with a week of readings.

    python scripts/demo.py              # builds data/demo/, then serves http://localhost:8001
    python scripts/demo.py --rebuild    # start the week over

Three air handlers and a boiler, every reading generated here: occupied-hours schedules,
one unit whose discharge air runs warm most afternoons (so the alarm rules have something to
find), an operator override on a setpoint, and outdoor weather. It runs in viewer mode: no
BACnet, nothing sent anywhere, and your own config and database are not touched.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
DEMO = REPO / "data" / "demo"
DAYS = 7
STEP = 120

UNITS = [  # (code, discharge setpoint °F, runs warm in the afternoon)
    ("AHU-1", 55.0, False), ("AHU-2", 55.0, True), ("AHU-3", 56.0, False)]
AHU_POINTS = [  # (object type, name, units)
    ("analog-input", "Discharge Air Temp", "degrees-fahrenheit"),
    ("analog-value", "Discharge Air Temp Setpoint", "degrees-fahrenheit"),
    ("analog-input", "Mixed Air Temp", "degrees-fahrenheit"),
    ("analog-input", "Return Air Temp", "degrees-fahrenheit"),
    ("analog-input", "Supply Fan Speed", "percent"),
    ("analog-output", "Outdoor Air Damper Command", "percent"),
    ("analog-input", "Duct Static Pressure", "inches-of-water"),
    ("analog-value", "Duct Static Pressure Setpoint", "inches-of-water"),
    ("binary-value", "Occupancy Status", None),
]
BOILER_POINTS = [
    ("binary-output", "Boiler 1 Enable Command", None),
    ("analog-input", "Boiler 1 Supply Water Temp", "degrees-fahrenheit"),
    ("analog-input", "Boiler 1 Return Water Temp", "degrees-fahrenheit"),
]


def _occupied(t: int) -> bool:
    lt = time.localtime(t)
    return lt.tm_wday < 5 and 6 <= lt.tm_hour < 18


def _oat(t: int) -> float:
    lt = time.localtime(t)
    hour = lt.tm_hour + lt.tm_min / 60
    return 48 + 12 * math.sin((hour - 9) / 24 * 2 * math.pi) + 4 * math.sin(t / 86400 / 3)


def _value(name: str, unit: tuple, t: int, rnd: random.Random) -> float:
    code, dat_sp, warm = unit
    occ = _occupied(t)
    oat = _oat(t)
    hour = time.localtime(t).tm_hour
    fan = (62 + 10 * math.sin(t / 3600)) if occ else 0.0
    if name == "Supply Fan Speed":
        return round(max(fan + rnd.uniform(-2, 2), 0) if occ else 0.0, 1)
    if name == "Discharge Air Temp Setpoint":
        return dat_sp
    if name == "Discharge Air Temp":
        if not occ:
            return round(66 + rnd.uniform(-1, 1), 1)
        drift = 8.5 if (warm and 13 <= hour < 17) else 0.0
        return round(dat_sp + drift + rnd.gauss(0, 0.6), 1)
    if name == "Mixed Air Temp":
        return round((0.7 * 71 + 0.3 * oat) + rnd.gauss(0, 0.4), 1)
    if name == "Return Air Temp":
        return round((72.5 if occ else 69.0) + rnd.gauss(0, 0.3), 1)
    if name == "Outdoor Air Damper Command":
        return round(20 + max(0, min(60, (65 - oat) * 3)) if occ else 0.0, 1)
    if name == "Duct Static Pressure Setpoint":
        return 1.5
    if name == "Duct Static Pressure":
        return round((1.5 + rnd.gauss(0, 0.04)) if occ else 0.0, 2)
    if name == "Occupancy Status":
        return 1.0 if occ else 0.0
    raise KeyError(name)


def build() -> Path:
    from hvactrend.models import Quality, Sample
    from hvactrend.storage.sqlite_dal import SQLiteDAL

    if DEMO.exists():
        shutil.rmtree(DEMO)
    DEMO.mkdir(parents=True)
    cfg = DEMO / "config.yaml"
    sys.path.insert(0, str(REPO / "scripts"))
    import configure
    text = configure.render((REPO / "config" / "config.example.yaml").read_text(encoding="utf-8"),
                            {"name": "Demo Building", "code": "DEMO", "location": "",
                             "ip": "127.0.0.1", "prefix": 8, "instance": 4194000,
                             "lan": False, "port": 8001})
    text = text.replace('path: "data/hvactrend.db"', f'path: "{(DEMO / "demo.db").as_posix()}"')
    text = text.replace('file: "logs/hvactrend.log"', f'file: "{(DEMO / "demo.log").as_posix()}"')
    text = text.replace('dir: "data/backups"', f'dir: "{(DEMO / "backups").as_posix()}"')
    cfg.write_text(text, encoding="utf-8")

    dal = SQLiteDAL(DEMO / "demo.db", REPO / "db" / "schema.sql")
    dal.apply_schema()
    c = dal._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'DEMO','Demo Building')")
    c.execute("INSERT INTO buildings(id,site_id,code,name) VALUES (1,1,'MAIN','Demo Building')")
    now = int(time.time()) // STEP * STEP
    t0 = now - DAYS * 86400
    rnd = random.Random(7)
    pid = 0
    plan = []
    for i, unit in enumerate(UNITS, start=1):
        c.execute("INSERT INTO systems(id,building_id,code,name,kind,occupancy_mode) "
                  "VALUES (?,1,?,?,'ahu','scheduled')", (i, unit[0], unit[0]))
        c.execute("INSERT INTO devices(id,site_id,device_instance,address,name,created_utc) "
                  "VALUES (?,1,?,?,?,0)", (i, 1000 + i, f"192.0.2.{10 + i}", f"{unit[0]}-CTRL"))
        for j, (otype, name, units) in enumerate(AHU_POINTS, start=1):
            pid += 1
            c.execute("INSERT INTO points(id,device_id,system_id,source,object_type,"
                      "object_instance,name,units_text,enabled,created_utc) "
                      "VALUES (?,?,?,'bacnet',?,?,?,?,1,0)", (pid, i, i, otype, j, name, units))
            plan.append((pid, name, unit))
    b = len(UNITS) + 1
    c.execute("INSERT INTO systems(id,building_id,code,name,kind,occupancy_mode) "
              "VALUES (?,1,'BLR-1','BLR-1','plant','always')", (b,))
    c.execute("INSERT INTO devices(id,site_id,device_instance,address,name,created_utc) "
              "VALUES (?,1,2001,'192.0.2.30','PLANT-CTRL',0)", (b,))
    boiler = []
    for j, (otype, name, units) in enumerate(BOILER_POINTS, start=1):
        pid += 1
        c.execute("INSERT INTO points(id,device_id,system_id,source,object_type,object_instance,"
                  "name,units_text,enabled,created_utc) VALUES (?,?,?,'bacnet',?,?,?,?,1,0)",
                  (pid, b, b, otype, j, name, units))
        boiler.append((pid, name))

    batch = []
    for t in range(t0, now + 1, STEP):
        for p, name, unit in plan:
            batch.append(Sample(point_id=p, ts_utc=t, value_num=_value(name, unit, t, rnd),
                                quality=Quality.GOOD))
        heating = _oat(t) < 50
        for p, name in boiler:
            v = (1.0 if heating else 0.0) if "Enable" in name else (
                (150 + rnd.gauss(0, 1)) if heating and "Supply" in name else
                (135 + rnd.gauss(0, 1)) if heating else 95.0)
            batch.append(Sample(point_id=p, ts_utc=t, value_num=round(v, 1), quality=Quality.GOOD))
        if len(batch) > 20000:
            dal.write_samples(batch)
            batch = []
    dal.write_samples(batch)

    from hvactrend.ai.weather import derive
    for t in range(t0 // 3600 * 3600, now, 3600):
        oat = _oat(t)
        dal.upsert_weather(derive({"ts_utc": t, "oat_f": round(oat, 1), "rh_pct": 55.0,
                                   "source": "demo"}), updated_utc=now)

    from hvactrend.ai.roles import assign_roles
    assign_roles(dal)
    dal.build_rollups(now)
    # an operator holding AHU-3's static setpoint by hand, as the priority read records it
    sp = next(p for p, name, unit in plan
              if unit[0] == "AHU-3" and name == "Duct Static Pressure Setpoint")
    dal.record_commands(now - 2 * 86400, [{"point_id": sp, "has_array": True, "level": 8,
                                           "value_num": 1.5, "value_text": None}])
    from hvactrend.config import load_config
    from hvactrend.rules import live
    live.run(dal, load_config(str(cfg)), now)          # the alarm rules over the week
    dal.close()
    return cfg


def found() -> list[str]:
    """What the alarm rules found in the demo week, one line each. On a real system these go
    to the phone; the demo has no notification topic, so it prints them instead."""
    import sqlite3
    db = DEMO / "demo.db"
    if not db.exists():
        return []
    c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        rows = c.execute("SELECT status, detail FROM incidents ORDER BY started_utc").fetchall()
    finally:
        c.close()
    return [f"  {'OPEN  ' if s == 'open' else 'closed'}  {d}" for s, d in rows]


def main() -> int:
    p = argparse.ArgumentParser(description="Run HVACTrend on a made-up building.")
    p.add_argument("--rebuild", action="store_true", help="regenerate the demo week")
    p.add_argument("--port", type=int, default=8001)
    args = p.parse_args()
    cfg = DEMO / "config.yaml"
    if args.rebuild or not cfg.exists():
        print("Building the demo week (a few seconds)...")
        cfg = build()
    os.chdir(REPO)
    os.environ["HVACTREND_CONFIG"] = str(cfg)
    os.environ["HVACTREND_NO_BACNET"] = "1"
    os.environ["HVACTREND_SECRETS"] = str(DEMO / "secrets.env")   # never your real secrets
    lines = found()
    if lines:
        print("What the alarm rules found this week (a real system sends these to your phone):")
        print("\n".join(lines))
    print(f"HVACTrend demo: http://localhost:{args.port}   (Ctrl+C to stop)")
    import uvicorn
    uvicorn.run("hvactrend.api.app:create_app", factory=True, host="127.0.0.1", port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
