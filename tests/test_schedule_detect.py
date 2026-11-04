"""Occupancy-schedule auto-detection tests.

The two behaviours that matter most, both grounded in real plant facts:
  * a collector GAP must never read as "occupied the whole time" (a weekend with zero
    samples once made every unit look occupied Fri->Mon),
  * in cold weather the fans run ~30% for freeze protection while UNOCCUPIED, so the
    fan-speed fallback must not report those units occupied 24/7 all winter.
"""

import calendar
import time

from hvactrend.ai.schedule_detect import detect_schedules
from hvactrend.models import Quality, Sample
from hvactrend.storage.sqlite_dal import SQLiteDAL

GM = time.gmtime            # inject as localtime -> deterministic regardless of TZ


def _ts(y, mo, d, h, mi=0):
    return calendar.timegm((y, mo, d, h, mi, 0, 0, 0, 0))


def _seed(db, *, role, name, object_type="multi-state-input"):
    dal = SQLiteDAL(db)
    dal.apply_schema()
    c = dal._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'DEMO','Demo Building')")
    c.execute("INSERT INTO buildings(id,site_id,code,name) VALUES (1,1,'MAIN','Main')")
    c.execute("INSERT INTO systems(id,building_id,code,name,kind) VALUES (1,1,'AC-1','AC-1','ahu')")
    c.execute("INSERT INTO devices(id,site_id,device_instance,address,created_utc) "
              "VALUES (1,1,1,'192.0.2.1',0)")
    c.execute("INSERT INTO points(id,device_id,system_id,source,object_type,object_instance,"
              "name,enabled,role,role_canonical,created_utc) "
              "VALUES (1,1,1,'bacnet',?,1,?,1,?,1,0)", (object_type, name, role))
    return dal


def _day_samples(dal, day, *, occ_from, occ_to, occ_val=1.0, unocc_val=2.0, step_min=5):
    """A full day of 5-min samples (288/day — above the coverage floor), occupied
    between the given local hours."""
    rows = []
    for m in range(0, 24 * 60, step_min):
        ts = _ts(2026, 7, day, m // 60, m % 60)
        v = occ_val if (occ_from * 60 <= m < occ_to * 60) else unocc_val
        rows.append(Sample(point_id=1, ts_utc=ts, value_num=v, quality=Quality.GOOD))
    dal.write_samples(rows)


def test_detects_schedule_from_occupancy_point(tmp_path):
    dal = _seed(tmp_path / "s.db", role="OCCUPANCY", name="Occupancy Status|dac-3")
    # three Tuesdays-equivalent weekdays, occupied 06:00-18:00
    for day in (20, 21, 22):        # 2026-07-20 Mon, 21 Tue, 22 Wed
        _day_samples(dal, day, occ_from=6, occ_to=18)
    res = detect_schedules(dal, None, now_utc=_ts(2026, 7, 23, 0), lookback_days=14,
                           localtime=GM)
    days = res["systems"][0]["days"]
    assert res["systems"][0]["signal"] == "occupancy-point"
    for d in ("mon", "tue", "wed"):
        assert days[d]["start_min"] == 360 and days[d]["end_min"] >= 1050, (d, days[d])
    # days never observed are reported as insufficient — NOT guessed
    assert days["sat"]["insufficient"] is True and days["sun"]["insufficient"] is True
    dal.close()


def test_self_calibrates_inverted_multistate(tmp_path):
    """Which value means 'occupied' is learned from mid-workday data, not hardcoded —
    a controller using 2=occupied must still resolve correctly."""
    dal = _seed(tmp_path / "inv.db", role="OCCUPANCY", name="Occupancy Status|dac-9")
    for day in (20, 21, 22):
        _day_samples(dal, day, occ_from=6, occ_to=18, occ_val=2.0, unocc_val=1.0)
    res = detect_schedules(dal, None, now_utc=_ts(2026, 7, 23, 0), localtime=GM)
    assert res["systems"][0]["days"]["mon"]["start_min"] == 360
    dal.close()


def test_gap_day_is_insufficient_not_occupied(tmp_path):
    """A day whose samples don't span the transition windows (collector outage) must be
    reported insufficient rather than inferred."""
    dal = _seed(tmp_path / "g.db", role="OCCUPANCY", name="Occupancy Status|dac-3")
    _day_samples(dal, 20, occ_from=6, occ_to=18)          # full Monday
    # Tuesday: only a few afternoon samples (collector came back late)
    dal.write_samples([Sample(point_id=1, ts_utc=_ts(2026, 7, 21, 14, m), value_num=1.0,
                              quality=Quality.GOOD) for m in range(0, 60, 10)])
    res = detect_schedules(dal, None, now_utc=_ts(2026, 7, 23, 0), localtime=GM)
    days = res["systems"][0]["days"]
    assert days["mon"]["start_min"] == 360                 # good day used
    assert days["tue"]["insufficient"] is True             # partial day NOT used
    dal.close()


def test_fan_fallback_excludes_cold_weather_freeze_circulation(tmp_path):
    """Below the cold threshold the fans idle at ~30% while UNOCCUPIED; that must not be
    read as occupancy (otherwise every unit looks occupied 24/7 all winter)."""
    dal = _seed(tmp_path / "f.db", role="FAN_SPEED", name="SF~-A VFD Mod",
                object_type="analog-value")
    # cold day: 30% all night (freeze circulation), 80% only 07:00-17:00 (real occupancy)
    for day in (20, 21, 22):
        rows = []
        for m in range(0, 24 * 60, 5):
            ts = _ts(2026, 7, day, m // 60, m % 60)
            v = 80.0 if (7 * 60 <= m < 17 * 60) else 30.0
            rows.append(Sample(point_id=1, ts_utc=ts, value_num=v, quality=Quality.GOOD))
        dal.write_samples(rows)
    # weather: 20 F -> cold, so the 30% running is freeze protection
    for day in (20, 21, 22):
        for h in range(24):
            dal.upsert_weather({"ts_utc": _ts(2026, 7, day, h), "oat_f": 20.0,
                                "rh_pct": 50.0}, updated_utc=0)
    res = detect_schedules(dal, None, now_utc=_ts(2026, 7, 23, 0), localtime=GM)
    s = res["systems"][0]
    assert s["signal"] == "fan-speed"
    # occupancy = the 80% window only, NOT the 24 h of 30% circulation
    assert s["days"]["mon"]["start_min"] == 420             # 07:00
    assert s["days"]["mon"]["end_min"] <= 1020              # ~17:00, not 23:50
    dal.close()


# ── review #2: both live mis-detections, reproduced from the real data patterns ──
def test_mostly_unoccupied_unit_is_not_inverted_to_24x7(tmp_path):
    """AC-9 on the live plant: unoccupied ~22 h/day with a short midday call. The
    UNOCCUPIED state therefore dominates mid-workday, so calibrating on 'the dominant
    midday value' picks it as OCCUPIED and reports the unit occupied 00:00-24:00 —
    the exact inverse of the truth, on the one unit the operator most needs right."""
    dal = _seed(tmp_path / "vac.db", role="OCCUPANCY", name="Occupancy Status|dac-1")
    for day in (20, 21, 22):
        rows = []
        for m in range(0, 24 * 60, 5):
            ts = _ts(2026, 7, day, m // 60, m % 60)
            v = 1.0 if (11 * 60 <= m < 12 * 60) else 2.0     # occupied 11:00-12:00 only
            rows.append(Sample(point_id=1, ts_utc=ts, value_num=v, quality=Quality.GOOD))
        dal.write_samples(rows)
    res = detect_schedules(dal, None, now_utc=_ts(2026, 7, 23, 0), localtime=GM)
    mon = res["systems"][0]["days"]["mon"]
    assert not mon.get("unoccupied"), mon
    assert mon["start_min"] == 660, mon        # 11:00, not 00:00
    assert mon["end_min"] <= 720, mon          # ~12:00, not 24:00
    dal.close()


def test_always_occupied_point_reports_no_schedule_rather_than_guessing(tmp_path):
    """A point that never leaves its overnight state carries no schedule information.
    Say so — don't emit a fabricated window."""
    dal = _seed(tmp_path / "always.db", role="OCCUPANCY", name="Occupancy Status|dac-2")
    for day in (20, 21, 22):
        dal.write_samples([Sample(point_id=1, ts_utc=_ts(2026, 7, day, m // 60, m % 60),
                                  value_num=1.0, quality=Quality.GOOD)
                           for m in range(0, 24 * 60, 5)])
    res = detect_schedules(dal, None, now_utc=_ts(2026, 7, 23, 0), localtime=GM)
    s = res["systems"][0]
    assert s["days"]["mon"]["insufficient"] is True
    assert "never leaves its overnight state" in s["note"]
    dal.close()


def test_isolated_fan_samples_do_not_stretch_the_detected_day(tmp_path):
    """HV1 on the live plant: real occupied span 06:18-16:47 (10th-90th pct), but ONE
    stray sample at 02:28 and one at 22:17 made min()..max() report 02:30-22:15. The
    window must come from the longest CONTIGUOUS block, not the extremes."""
    dal = _seed(tmp_path / "spike.db", role="FAN_SPEED", name="SUP_FAN_VFD",
                object_type="analog-value")
    for day in (20, 21, 22):
        rows = []
        for m in range(0, 24 * 60, 5):
            ts = _ts(2026, 7, day, m // 60, m % 60)
            v = 66.0 if (5 * 60 <= m < 18 * 60) else 1.0
            if m in (2 * 60 + 30, 22 * 60 + 15):      # isolated stray excursions
                v = 66.0
            rows.append(Sample(point_id=1, ts_utc=ts, value_num=v, quality=Quality.GOOD))
        dal.write_samples(rows)
    res = detect_schedules(dal, None, now_utc=_ts(2026, 7, 23, 0), localtime=GM)
    mon = res["systems"][0]["days"]["mon"]
    assert mon["start_min"] == 300, mon        # 05:00 — not 02:30
    assert mon["end_min"] <= 1080, mon         # ~18:00 — not 22:15
    dal.close()
