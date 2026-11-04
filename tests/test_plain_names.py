"""Buildings that name points in plain English, not in tags; and the demo building."""

import importlib.util
from pathlib import Path

from hvactrend.ai.roles import classify

REPO = Path(__file__).resolve().parents[1]


def test_a_numbered_boiler_in_plain_english_is_the_plant_and_its_room_is_not():
    assert classify("Boiler 1 Enable Command") == "BLR_CMD"
    assert classify("Boiler 1 Supply Water Temp") == "BLR_SUP_TEMP"
    assert classify("Boiler 1 Return Water Temp") == "BLR_RET_TEMP"
    assert classify("Boiler 2 Status") == "BLR_STATUS"
    assert classify("Boiler Rm. Space Temp") == "ROOM_TEMP"        # the room, not the plant
    assert classify("BLR1.ST") == "BLR_SUP_TEMP"                   # tags still work


def test_the_demo_building_has_a_week_the_rules_can_judge(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("demo_t", REPO / "scripts" / "demo.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    monkeypatch.setattr(demo, "DEMO", tmp_path / "demo")
    cfg = demo.build()
    import sqlite3
    c = sqlite3.connect(tmp_path / "demo" / "demo.db")
    roles = {r for (r,) in c.execute("SELECT role FROM points")}
    assert {"DAT", "DAT_ACTIVE_SP", "FAN_SPEED", "BLR_SUP_TEMP", "BLR_CMD"} <= roles
    units = {u for (u,) in c.execute("SELECT system_code FROM incidents")}
    assert units == {"AHU-2"}                     # only the unit that runs warm
    assert "demo.db" in cfg.read_text(encoding="utf-8")
