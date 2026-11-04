"""Domain-model tests (ported from the old AHU program): roles, occupancy-split
baselines, and the baseline-aware pre-screen detectors. All hermetic — timestamps
use calendar.timegm + an injected gmtime so occupancy is deterministic regardless of
the test machine's timezone.
"""

import calendar
import time

from hvactrend.ai import baseline, roles
from hvactrend.ai.baseline import (  # noqa: F401 — extended below
    daymap_for_system, default_daymap, occ_pred,
)
from hvactrend.ai.baseline import (build_baselines, build_point_baseline,
                                      is_occupied, occupied_local)
from hvactrend.ai.prescreen import (detect_point_baseline, detect_relationships)
from hvactrend.models import Quality, Sample
from hvactrend.storage.sqlite_dal import SQLiteDAL

GM = time.gmtime                       # inject as 'localtime' -> deterministic UTC


def _ts(y, mo, d, h):
    return calendar.timegm((y, mo, d, h, 0, 0, 0, 0, 0))


# ── roles ────────────────────────────────────────────────────────────────────
def test_classify_core_roles():
    c = roles.classify
    cases = {
        "Discharge Air Temp|dac-3_UIP~_06": "DAT",
        "Discharge Air Temperature Setpoint Active|dac-3": "DAT_ACTIVE_SP",
        "Mixed Air Temp|dac-3_UIP~_03": "MAT",
        "Return Air Temp|dac-3_UIP~_05": "RAT",
        "Outdoor Air Temp|dac-3_TrAV~_06": "OAT",
        "HW Valve|dac-3_AOP~_03": "HTG_VALVE",
        "SF~-A VFD Mod|dac-3_AOP~_01": "FAN_SPEED",
        "SF~-A Run Status|dac-3_UIP~_01": "FAN_STATUS",
        "OA/RA Damper Mod|dac-3_AOP~_05": "OA_DAMPER",
        "Multistack Stage 1|dac-3_BOP~_03": "COOLING_STAGES",
        "Freezestat|dac-3_UIP~_07": "ALARM",
        # the minimum-OA position is an operator SETPOINT, paired with the damper so a
        # damper parked at a raised minimum isn't flagged as high/drifting/an outlier
        "Min OA Damper Position|dac-3_TrAV~_04": "OA_DAMPER_MIN_SP",
        "Minimum OA Damper Position": "OA_DAMPER_MIN_SP",
        "Economizer Minimum Position Setpoint BAS|dac-3": "OA_DAMPER_MIN_SP",
        "Static Pressure Setpoint|dac-3": "DUCT_PRESS_SP",
        "Duct Static Pressure Active|dac-3": "DUCT_PRESS",
        # AC-4 Symbio dialect (no pipe, verbose)
        "SF-A VFD Modulation": "FAN_SPEED",
        "Mixed Air Temperature Evap A": "MAT",
        # HV1 JCI abbreviations
        "SUP_AIR_TEMP": "DAT",
        "MIX_AIR_TEMP": "MAT",
        "ACT_SAT_SP": "DAT_ACTIVE_SP",
        "SUP_FAN_VFD": "FAN_SPEED",
    }
    for name, role in cases.items():
        assert c(name) == role, f"{name} -> {c(name)}, expected {role}"


def test_canonical_prefers_uip_then_enabled():
    pts = [
        {"id": 1, "name": "Discharge Air Temperature|dac-3", "role": "DAT",
         "system_id": 1, "enabled": 1},                       # logical twin
        {"id": 2, "name": "Discharge Air Temp|dac-3_UIP~_06", "role": "DAT",
         "system_id": 1, "enabled": 1},                       # UIP -> canonical
        {"id": 3, "name": "Low Outdoor Air Temp|dac-3_UIP~_17", "role": "OAT",
         "system_id": 1, "enabled": 0},                       # UIP but DISABLED (dead)
        {"id": 4, "name": "Outdoor Air Temp|dac-3_TrAV~_06", "role": "OAT",
         "system_id": 1, "enabled": 1},                       # enabled -> canonical
    ]
    assert roles.canonical_ids(pts) == {2, 4}   # UIP DAT; enabled OAT over disabled UIP


# ── occupancy ────────────────────────────────────────────────────────────────
def test_occupancy_rule():
    assert occupied_local(10, 2) is True         # Wed 10:00
    assert occupied_local(2, 2) is False         # Wed 02:00
    assert occupied_local(18, 2) is False        # boundary: 18:00 is unoccupied
    assert occupied_local(10, 5) is False        # Saturday
    assert occupied_local(10, 5, {"weekday_start": 5, "weekday_end": 18,
                                  "weekend_occupied": True}) is True
    # via timestamps (2026-06-30 is a Tuesday; 2026-07-04 a Saturday)
    assert is_occupied(_ts(2026, 6, 30, 12), localtime=GM) is True
    assert is_occupied(_ts(2026, 6, 30, 2), localtime=GM) is False
    assert is_occupied(_ts(2026, 7, 4, 12), localtime=GM) is False


# ── per-point baseline ───────────────────────────────────────────────────────
def test_build_point_baseline_splits_occupancy():
    rows = []
    for h, v in [(10, 70.0), (11, 71.0), (12, 69.0)]:        # occupied
        rows.append({"ts_utc": _ts(2026, 6, 30, h), "value_num": v,
                     "value_text": None, "quality": "good"})
    for h, v in [(2, 60.0), (3, 61.0), (4, 59.0)]:           # unoccupied
        rows.append({"ts_utc": _ts(2026, 6, 30, h), "value_num": v,
                     "value_text": None, "quality": "good"})
    rows.append({"ts_utc": _ts(2026, 6, 30, 13), "value_num": None,
                 "value_text": None, "quality": "comm-loss"})   # ignored
    b = build_point_baseline(rows, localtime=GM)
    assert round(b["occ"]["mean"]) == 70 and b["occ"]["n"] == 3
    assert round(b["unocc"]["mean"]) == 60 and b["unocc"]["n"] == 3
    assert 0.0 < b["changed_frac"] <= 1.0

    flat = [{"ts_utc": _ts(2026, 6, 30, 10 + i), "value_num": 50.0,
             "value_text": None, "quality": "good"} for i in range(5)]
    fb = build_point_baseline(flat, localtime=GM)
    assert fb["occ"]["std"] == baseline.STD_FLOOR      # std floored, not 0
    assert fb["changed_frac"] == 0.0                   # never moved


# ── occupancy-aware deviation detector (the headline win) ────────────────────
def test_deviation_is_occupancy_aware():
    p = {"id": 1, "name": "Supply Fan Speed", "role": "FAN_SPEED", "system_code": "AC-1"}
    bl = {"occ": {"mean": 60.0, "std": 5.0, "n": 200},
          "unocc": {"mean": 0.0, "std": 2.0, "n": 200}, "changed_frac": 0.5}

    # 60% during the day -> in the occupied band -> no flag
    day_ok = {"last_good": 61.0, "last_ts": _ts(2026, 6, 30, 12), "good_n": 50}
    assert detect_point_baseline(p, day_ok, bl, localtime=GM) == []

    # 0% during the day -> 12 sigma below the occupied band -> HIGH deviation
    day_off = {"last_good": 0.0, "last_ts": _ts(2026, 6, 30, 12), "good_n": 50}
    hits = detect_point_baseline(p, day_off, bl, localtime=GM)
    assert any(a.kind == "deviation" and a.severity.value == "high" for a in hits)

    # SAME 0% but at 2 AM -> matches the unoccupied band -> NOT flagged
    night_off = {"last_good": 0.0, "last_ts": _ts(2026, 6, 30, 2), "good_n": 50}
    assert detect_point_baseline(p, night_off, bl, localtime=GM) == []


def test_every_hardcoded_threshold_is_declared_against_plant_truth():
    """THE TEST THAT WOULD HAVE CAUGHT ALL FIVE. Three code reviews passed over
    `_ECON_ENTH_CHANGEOVER = 28.0  # ASHRAE ~28` because it is impeccable code — the
    evidence that it is a WRONG NUMBER lives in the points table, not the source tree.
    So every module-level numeric constant in the analysis code must be declared in
    ai/reconcile.CONSTANTS with an explicit answer to "does the plant publish its own
    version of this?". Adding one without answering fails here."""
    from pathlib import Path
    from hvactrend.ai import reconcile

    src = Path(__file__).resolve().parent.parent / "src" / "hvactrend" / "ai"
    for mod in ("derived", "prescreen"):
        text = (src / f"{mod}.py").read_text(encoding="utf-8")
        missing = reconcile.unregistered(text, mod)
        assert not missing, (
            f"{mod}.py defines {sorted(missing)} but ai/reconcile.CONSTANTS does not "
            f"declare them. Every threshold must say whether the plant publishes its "
            f"own version — that question is what three reviews failed to ask.")

    # the registry itself must stay honest
    for name, (mod, status, note) in reconcile.CONSTANTS.items():
        assert status in reconcile.VALID_STATUS, f"{name}: bad status {status}"
        assert note.strip(), f"{name}: a status with no reasoning is not an answer"

    # the two the plant demonstrably publishes must be wired, not merely noted
    assert reconcile.CONSTANTS["_ECON_ENTH_CHANGEOVER"][1] == "reads_plant"
    assert reconcile.CONSTANTS["_DAMPER_MIN"][1] == "reads_plant"
    # and the unverified ones must still be visible rather than quietly blessed
    assert reconcile.unverified(), "unverified list emptied without evidence?"


def test_unclassified_report_surfaces_a_commissioned_value_that_fell_through():
    """A setpoint/limit-shaped name landing in CONFIG/OTHER is the fingerprint of this
    bug class, and it used to be completely silent. The next instance must announce
    itself on the model rebuild instead of waiting for a wrong report."""
    from hvactrend.ai.roles import unclassified_report
    pts = [
        # the real one, as it looked before it had a role
        {"id": 1, "name": "Economizer Enable DB SP|dac-3", "role": "CONFIG", "enabled": 1},
        {"id": 2, "name": "Mixed Air Low Limit", "role": "OTHER", "enabled": 1},
        # noise that must NOT be reported
        {"id": 3, "name": "Discharge Air Temp", "role": "DAT", "enabled": 1},
        {"id": 4, "name": "SITE.HV1.SAS", "role": "OTHER", "enabled": 1},
        {"id": 5, "name": "Some Old Setpoint", "role": "CONFIG", "enabled": 0},
    ]
    rep = unclassified_report(pts)
    names = {f["name"] for f in rep["commissioned_values"]}
    assert "economizer enable db sp" in names
    assert "mixed air low limit" in names
    assert not any("discharge air temp" == n for n in names)   # classified -> fine
    assert not any("hv1 sas" in n for n in names)              # not a commissioned value
    assert not any("some old setpoint" in n for n in names)    # disabled -> not logged
    # the cryptic tag still counts as invisible, just not as a commissioned value
    # (_norm folds '.' to spaces, so SITE.HV1.SAS -> 'site hv1 sas')
    assert rep["invisible_points"] == 3 and "site hv1 sas" in rep["sample"]


def test_plant_published_limits_and_economizer_setpoints_are_classified():
    """Review #3 follow-up: the plant trends its own commissioned limits and its own
    economizer changeover on every unit, and ALL of them were landing in CONFIG/OTHER
    where nothing could pair them. The economizer one produced a wrong REPORT: the
    check compared measured OA enthalpy against a hardcoded 28.0 Btu/lb while this
    plant is commissioned at 19, so correct economizer lockout was reported as a
    fleet-wide missed-free-cooling fault in every prior report."""
    c = roles.classify
    assert c("Discharge Air High Limit") == "DAT_HI_LIMIT"
    assert c("Discharge Air Low Limit") == "DAT_LO_LIMIT"
    assert c("Mixed Air Low Limit") == "MAT_LO_LIMIT"
    assert c("Return Air High Limit") == "RAT_HI_LIMIT"
    assert c("Return Air Low Limit") == "RAT_LO_LIMIT"
    assert c("Economizer Enable DB SP|dac-3_TrAV~_11") == "ECON_DB_SP"
    assert c("Econ Enable Enthalpy|dac-3_TrAV~_07") == "ECON_ENTH_SP"
    assert c("Economizer Enable Enthalpy") == "ECON_ENTH_SP"
    assert c("OK to Economize|dac-3_BV~_004") == "ECON_OK"
    assert c("Disable Mech Cooling") == "MECH_COOL_DISABLE"

    # ...without disturbing the neighbours these rules sit between
    assert c("Low Limit Alarm") == "ALARM"          # an alarm, not a limit value
    assert c("Economizer Minimum Position Setpoint") == "OA_DAMPER_MIN_SP"
    assert c("Discharge Air Temp|dac-3_UIP~_06") == "DAT"
    assert c("Discharge Air Setpoint") == "DAT_ACTIVE_SP"
    assert c("Mixed Air Temp|dac-3_UIP~_03") == "MAT"
    # every new role must be declared, or canonical_ids/detectors silently ignore it
    for r in ("DAT_HI_LIMIT", "DAT_LO_LIMIT", "MAT_LO_LIMIT", "RAT_HI_LIMIT",
              "RAT_LO_LIMIT", "ECON_DB_SP", "ECON_ENTH_SP", "ECON_OK",
              "MECH_COOL_DISABLE"):
        assert r in roles.ALL_ROLES


def test_apply_plant_limits_only_alarms_on_real_thresholds(tmp_path):
    """The out-of-range detector had NEVER fired: 0 points had alarm_hi/lo while the
    BAS published limits on all 14 units. But blanket-enabling them would have been a
    worse flood than the one just fixed — measured over 14 days, breach-rate WHILE
    RUNNING was 1.1% for the discharge HIGH limit but 39% for the discharge LOW limit
    (a control floor the cooling loop rides, not an alarm). So: ingest every limit,
    alarm only on the ones that measure like alarms."""
    from hvactrend.ai.limits import apply_plant_limits

    dal = SQLiteDAL(tmp_path / "lim.db")
    dal.apply_schema()
    c = dal._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'DEMO','Demo Building')")
    c.execute("INSERT INTO buildings(id,site_id,code,name) VALUES (1,1,'B1','Main')")
    c.execute("INSERT INTO systems(id,building_id,code,name) VALUES (1,1,'AC-1','AC-1')")
    c.execute("INSERT INTO devices(id,site_id,device_instance,created_utc) VALUES (1,1,1,0)")

    def pt(pid, name, role, canon=0):
        c.execute("INSERT INTO points(id,device_id,system_id,source,object_type,"
                  "object_instance,name,role,role_canonical,enabled,created_utc) "
                  "VALUES (?,1,1,'bacnet','analogInput',?,?,?,?,1,0)",
                  (pid, pid, name, role, canon))

    pt(1, "Discharge Air Temp", "DAT", 1)
    pt(2, "Discharge Air High Limit", "DAT_HI_LIMIT")
    pt(3, "Discharge Air Low Limit", "DAT_LO_LIMIT")
    pt(4, "Mixed Air Temp", "MAT", 1)
    pt(5, "Mixed Air Low Limit", "MAT_LO_LIMIT")
    now = 1_800_000_000
    for pid, v in ((2, 70.0), (3, 55.0), (5, 52.0)):
        dal.write_samples([Sample(point_id=pid, ts_utc=now - 600, value_num=v,
                                  quality=Quality.GOOD)])

    out = apply_plant_limits(dal, None, now_utc=now)
    assert out["limit_points"] == 3 and out["applied"] == 2
    assert out["recorded_not_alarming"] == 1        # the DAT low control limit

    # THE POINT OF THE TEST: alarm_enabled is ONE flag gating BOTH sides, so the
    # non-alarming discharge LOW limit must never be written into alarm_lo — doing so
    # would switch it on alongside the genuine high limit and flood on a threshold the
    # cooling loop rides by design.
    dat = c.execute("SELECT alarm_hi, alarm_lo, alarm_enabled FROM points "
                    "WHERE id=1").fetchone()
    assert dat[0] == 70.0
    assert dat[1] is None, "a control limit must not occupy alarm_lo"
    assert dat[2] == 1

    mat = c.execute("SELECT alarm_hi, alarm_lo, alarm_enabled FROM points "
                    "WHERE id=4").fetchone()
    assert (mat[0], mat[1], mat[2]) == (None, 52.0, 1)   # freeze limit alarms

    # narrowing the alarm set must CLEAR what no longer qualifies. A withdrawn limit
    # that keeps alarming against a value nothing maintains is the silent-staleness
    # failure mode this project has already been bitten by twice.
    cfg = {"analysis": {"limits": {"alarm_roles": ["MAT_LO_LIMIT"]}}}
    out2 = apply_plant_limits(dal, cfg, now_utc=now)
    assert out2["cleared"] == 1
    dat = c.execute("SELECT alarm_hi, alarm_lo, alarm_enabled FROM points "
                    "WHERE id=1").fetchone()
    assert (dat[0], dat[1], dat[2]) == (None, None, 0)
    mat = c.execute("SELECT alarm_lo, alarm_enabled FROM points WHERE id=4").fetchone()
    assert (mat[0], mat[1]) == (52.0, 1)        # the still-qualifying one is untouched

    # and the whole thing is switchable off
    off = apply_plant_limits(dal, {"analysis": {"limits": {"from_plant": False}}},
                             now_utc=now)
    assert off["applied"] == 0
    dal.close()


def test_bas_alarm_and_manual_reset_points_become_first_class_findings():
    """102 ALARM points and 24 'Man Reset Required' points were trended and INVISIBLE:
    ALARM is a status role so no detector ran on it, and in the prompt each was one line
    among 611 showing a window mean of ~0.02. The cost: Multistack (compressor) alarms
    fired on four units at Mon 08:45 — the exact minute every unit began exceeding its
    discharge-air limit — and the report never mentioned them, blaming the economizer on
    a morning far too humid to economize."""
    from hvactrend.ai.prescreen import detect_point
    base = {"n": 288, "good_n": 288, "comm_n": 0, "fault_n": 0, "constant": False,
            "min": 0.0, "max": 1.0, "last_good": 0.0, "last_ts": _ts(2026, 6, 30, 12)}
    rows = [{"ts_utc": _ts(2026, 6, 30, 8) + i * 300, "value_num": 0.0,
             "value_text": "inactive", "quality": "good"} for i in range(6)]
    rows += [{"ts_utc": _ts(2026, 6, 30, 8) + (6 + i) * 300, "value_num": 1.0,
              "value_text": "active", "quality": "good"} for i in range(4)]

    alarm = {"id": 1, "name": "Multistack Alarm", "role": "ALARM", "system_code": "AC-3"}
    hits = detect_point(alarm, base, rows=rows, localtime=GM)
    fired = [a for a in hits if a.kind == "alarm"]
    assert fired and fired[0].severity.value == "high"
    assert "08:30" in fired[0].detail          # WHEN it fired — enables correlation
    assert fired[0].evidence["active_n"] == 4

    # a manual-reset flag is the equipment asking for a human — same treatment
    rst = {"id": 2, "name": "SF~-A Man Reset Required", "role": "RESET_REQUIRED",
           "system_code": "AC-3"}
    assert [a for a in detect_point(rst, base, rows=rows, localtime=GM)
            if a.kind == "alarm"]

    # never active -> silent
    quiet = [dict(r, value_num=0.0, value_text="inactive") for r in rows]
    assert not [a for a in detect_point(alarm, base, rows=quiet, localtime=GM)
                if a.kind == "alarm"]
    # and the decoded TEXT wins over the numeric encoding (1 != always bad)
    assert not [a for a in detect_point(
        alarm, base, rows=[dict(r, value_num=1.0, value_text="normal") for r in rows],
        localtime=GM) if a.kind == "alarm"]


def test_canonical_actuator_is_the_modulating_point_not_an_end_switch():
    """Found by auditing whether every point BEHAVES like its role says, rather than
    waiting to be shown a bad report. `EA Damper 100% Open` is a binary end-switch with
    exactly ONE distinct value ever recorded, and the UIP-first rule made it the
    canonical EF_DAMPER on ALL 13 UNITS — so every exhaust-damper check ran on a contact
    that never changes. A detector producing nothing is indistinguishable from a healthy
    plant, which is why this survived three reviews.

    UIP-first is right for a SENSOR (the wired transducer beats a logical twin) and
    wrong for an ACTUATOR (the wired point is the limit switch, not the position)."""
    pts = [
        {"id": 1, "name": "EF Damper Mod|dac-3_AOP~_06", "role": "EF_DAMPER",
         "system_id": 1, "enabled": 1, "units_text": "percent",
         "object_type": "analog-output"},
        {"id": 2, "name": "EA Damper 100% Open|dac-3_UIP~_13", "role": "EF_DAMPER",
         "system_id": 1, "enabled": 1, "units_text": None,
         "object_type": "binary-input"},
        # a sensor pair must KEEP the UIP preference
        {"id": 3, "name": "Discharge Air Temp|dac-3_UIP~_06", "role": "DAT",
         "system_id": 1, "enabled": 1, "units_text": "degrees-fahrenheit",
         "object_type": "analog-input"},
        {"id": 4, "name": "Discharge Air Temp Logical", "role": "DAT",
         "system_id": 1, "enabled": 1, "units_text": "degrees-fahrenheit",
         "object_type": "analog-value"},
    ]
    canon = roles.canonical_ids(pts)
    assert 1 in canon and 2 not in canon, "actuator must be the modulating position"
    assert 3 in canon and 4 not in canon, "sensor must still prefer the wired UIP"

    # ...and a damper is a damper even when its name says "Modulation", which the
    # generic fan-speed rule used to steal (dropping it out of _COMMANDED_ROLES so a
    # driven position got z-scored like a sensor)
    assert roles.classify("EF Damper Modulation") == "EF_DAMPER"
    assert roles.classify("OA/RA Damper Mod|dac-3_AOP~_05") == "OA_DAMPER"
    assert roles.classify("SF~-A VFD Mod|dac-3_AOP~_01") == "FAN_SPEED"


def test_new_roles_cover_the_operator_action_signals():
    c = roles.classify
    assert c("SF~-A Man Reset Required|dac-3_BV~_005") == "RESET_REQUIRED"
    assert c("SF~-A Enable|dac-3_BV~_001") == "FAN_CMD"
    assert c("Exhaust Fan S/S|dac-1") == "FAN_CMD"
    # ...without stealing the feedback half of the pair
    assert c("SF~-A Run Status|dac-3_UIP~_01") == "FAN_STATUS"
    assert c("SF~-A Failure|dac-3_BV~_001") == "ALARM"
    for r in ("RESET_REQUIRED", "FAN_CMD"):
        assert r in roles.ALL_ROLES


def test_out_of_range_is_gated_on_the_unit_actually_running():
    """An air-side limit only means something when air is moving. AC-9 (fan 0% for 24 h,
    unoccupied) sits at ~77 F against its own 70 F discharge high limit — that is still
    air equalising to room temperature, not an over-temperature fault. Found while
    validating the plant-limit ingest, and it is the SAME class as the occupancy-
    transition false positives: idle equipment judged against running thresholds."""
    from hvactrend.ai.prescreen import detect_point
    p = {"id": 1, "name": "Discharge Air Temp", "role": "DAT", "system_code": "AC-9",
         "object_type": "analog-input", "units_text": "degrees-fahrenheit",
         "alarm_enabled": 1, "alarm_hi": 70.0, "alarm_lo": None}
    s = {"n": 100, "good_n": 100, "comm_n": 0, "fault_n": 0, "constant": False,
         "min": 76.2, "max": 78.4, "last_good": 77.1, "last_ts": _ts(2026, 6, 30, 12)}

    assert [a for a in detect_point(p, s, running=True) if a.kind == "out-of-range"]
    assert not [a for a in detect_point(p, s, running=False) if a.kind == "out-of-range"]
    # unknown airflow must NOT suppress — a missing fan point can't silence a real limit
    assert [a for a in detect_point(p, s, running=None) if a.kind == "out-of-range"]
    assert [a for a in detect_point(p, s) if a.kind == "out-of-range"]


def test_deviation_suppressed_while_unit_is_still_settling():
    """Review #3: the z-score flipped bands the instant the schedule did, so equipment
    mid-ramp was judged against a steady-state band it had not reached. On the live
    plant 178/267 stored deviation alerts (67%) were raised in the 05:00/18:00
    transition hours. A fan at 0% three minutes after a 05:00 start is starting up,
    not faulted."""
    p = {"id": 1, "name": "SF-A VFD Mod", "role": "FAN_SPEED", "system_code": "AC-1"}
    bl = {"occ": {"mean": 96.5, "std": 13.6, "n": 2000},
          "unocc": {"mean": 0.0, "std": 2.0, "n": 2000}, "changed_frac": 0.5}
    t0503 = _ts(2026, 6, 30, 5) + 3 * 60      # 3 min after the 05:00 occupancy start
    ramping = {"last_good": 0.0, "last_ts": t0503, "good_n": 50}
    assert detect_point_baseline(p, ramping, bl, localtime=GM) == []

    # ...but the SAME reading mid-morning, long past the settle window, IS a fault.
    midday = {"last_good": 0.0, "last_ts": _ts(2026, 6, 30, 12), "good_n": 50}
    assert any(a.kind == "deviation"
               for a in detect_point_baseline(p, midday, bl, localtime=GM))

    # ...and a unit STILL dead an hour after its scheduled start is not "settling".
    late = {"last_good": 0.0, "last_ts": _ts(2026, 6, 30, 6) + 5 * 60, "good_n": 50}
    assert any(a.kind == "deviation"
               for a in detect_point_baseline(p, late, bl, localtime=GM))

    # settle_min=0 disables the gate entirely (operator escape hatch)
    assert any(a.kind == "deviation" for a in
               detect_point_baseline(p, ramping, bl, localtime=GM, settle_min=0))


def test_evening_shutdown_is_also_a_settle_window():
    """The 18:00 transition is the mirror image: a fan coasting down reads far ABOVE
    the unoccupied band it is being compared to."""
    p = {"id": 1, "name": "SF-A VFD Mod", "role": "FAN_SPEED", "system_code": "AC-1"}
    bl = {"occ": {"mean": 96.5, "std": 13.6, "n": 2000},
          "unocc": {"mean": 0.0, "std": 2.0, "n": 2000}, "changed_frac": 0.5}
    coasting = {"last_good": 60.0, "last_ts": _ts(2026, 6, 30, 18) + 4 * 60, "good_n": 50}
    assert detect_point_baseline(p, coasting, bl, localtime=GM) == []


def test_std_floor_is_units_aware():
    """Review #3: a flat 0.4 std floor is right for degF and catastrophic for
    inches-of-water — it inflated the 26 pressure points' bands 4x-400x and made
    |z| >= 3 physically unreachable, silently disabling their only detectors."""
    assert baseline.std_floor("degrees-fahrenheit") == 0.4
    assert baseline.std_floor(None) == baseline.STD_FLOOR       # unknown -> safe default
    assert baseline.std_floor("inches-of-water") < 0.05         # must not swamp the signal

    # a near-constant building-pressure point keeps a usable band...
    flat_press = [{"ts_utc": _ts(2026, 6, 30, 10) + i * 120, "value_num": 0.016,
                   "value_text": None, "quality": "good"} for i in range(30)]
    b = build_point_baseline(flat_press, localtime=GM, units_text="inches-of-water")
    assert b["occ"]["std"] == baseline.std_floor("inches-of-water")

    # ...and a real over-pressurization to 0.25 in wc now scores as a fault,
    # where the old 0.4 floor gave z = 0.59 and raised nothing.
    z = (0.25 - b["occ"]["mean"]) / b["occ"]["std"]
    assert z >= 3.0
    # the degF path is unchanged
    flat_t = [{"ts_utc": _ts(2026, 6, 30, 10) + i * 120, "value_num": 50.0,
               "value_text": None, "quality": "good"} for i in range(30)]
    bt = build_point_baseline(flat_t, localtime=GM, units_text="degrees-fahrenheit")
    assert bt["occ"]["std"] == 0.4


def test_baseline_stuck_and_thin_band_guard():
    p = {"id": 1, "name": "MAT", "role": "MAT", "system_code": "AC-1",
         "object_type": "analog-input"}
    s = {"last_good": 70.0, "last_ts": _ts(2026, 6, 30, 12), "good_n": 50}
    # stuck: changed in ~0% of polls
    stuck = detect_point_baseline(
        p, s, {"occ": {"mean": 70, "std": 1, "n": 200}, "changed_frac": 0.0}, localtime=GM)
    assert any(a.kind == "stuck" for a in stuck)
    # thin band (n below floor) -> deviation NOT trusted even if far off
    far = {"last_good": 999.0, "last_ts": _ts(2026, 6, 30, 12), "good_n": 50}
    thin = detect_point_baseline(
        p, far, {"occ": {"mean": 70, "std": 1, "n": 5}, "changed_frac": 0.5}, localtime=GM)
    assert not any(a.kind == "deviation" for a in thin)
    # non-analysis role -> baseline detectors skip entirely
    alarm = {"id": 2, "name": "Freezestat", "role": "ALARM", "system_code": "AC-1"}
    assert detect_point_baseline(alarm, far, {"occ": {"mean": 0, "std": 1, "n": 200},
                                              "changed_frac": 0.5}, localtime=GM) == []


# ── correlation build + relationship-break detection ─────────────────────────
def _seed_corr(db_path):
    dal = SQLiteDAL(db_path)
    dal.apply_schema()
    c = dal._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'DEMO','Demo Building')")
    c.execute("INSERT INTO buildings(id,site_id,code,name) VALUES (1,1,'MAIN','Main')")
    c.execute("INSERT INTO systems(id,building_id,code,name,kind) VALUES (1,1,'AC-1','AC-1','ahu')")
    c.execute("INSERT INTO devices(id,site_id,device_instance,address,created_utc) "
              "VALUES (1,1,100100,'192.0.2.19',0)")
    c.execute("INSERT INTO points(id,device_id,system_id,source,object_type,object_instance,"
              "name,units_text,enabled,created_utc) VALUES "
              "(1,1,1,'bacnet','analog-input',10,'Outdoor Air Temp|dac-1_TrAV~_06','degrees-fahrenheit',1,0)")
    c.execute("INSERT INTO points(id,device_id,system_id,source,object_type,object_instance,"
              "name,units_text,enabled,created_utc) VALUES "
              "(2,1,1,'bacnet','analog-input',11,'Mixed Air Temp|dac-1_UIP~_03','degrees-fahrenheit',1,0)")
    roles.assign_roles(dal)         # OAT + MAT, both canonical
    return dal


def _write_pairs(dal, base_ts, pairs):
    """Write OAT (point 1) and MAT (point 2) samples at matching timestamps."""
    ss = []
    for i, (x, y) in enumerate(pairs):
        t = base_ts + i * 3600
        ss.append(Sample(point_id=1, ts_utc=t, value_num=float(x), quality=Quality.GOOD))
        ss.append(Sample(point_id=2, ts_utc=t, value_num=float(y), quality=Quality.GOOD))
    dal.write_samples(ss)


def test_correlation_recovered_and_break_detected(tmp_path):
    dal = _seed_corr(tmp_path / "c.db")
    # fit data: MAT = 2*OAT + 5 (+ tiny alternating noise so residual_std is realistic)
    fit = [(40 + i * 0.5, 2 * (40 + i * 0.5) + 5 + (0.4 if i % 2 else -0.4))
           for i in range(30)]
    _write_pairs(dal, _ts(2026, 6, 1, 0), fit)
    build_baselines(dal, None, window_days=30, now_utc=_ts(2026, 6, 3, 0), localtime=GM)

    oat_mat = next(c for c in dal.corrs() if c["kind"] == "oat_mat")
    assert abs(oat_mat["slope"] - 2.0) < 0.1 and abs(oat_mat["intercept"] - 5.0) < 1.0

    # healthy recent window: still on the line -> no relationship anomaly
    _write_pairs(dal, _ts(2026, 6, 5, 0), [(50 + i, 2 * (50 + i) + 5) for i in range(12)])
    ok = detect_relationships(dal, _ts(2026, 6, 5, 0), _ts(2026, 6, 5, 23))
    assert not any(a.kind == "relationship" for a in ok)

    # broken recent window: MAT running +30 off the expected line -> flagged
    _write_pairs(dal, _ts(2026, 6, 6, 0), [(50 + i, 2 * (50 + i) + 35) for i in range(12)])
    broken = detect_relationships(dal, _ts(2026, 6, 6, 0), _ts(2026, 6, 6, 23))
    rel = [a for a in broken if a.kind == "relationship"]
    assert rel and rel[0].metric == "oat_mat" and rel[0].point_id == 2   # flags the MAT (y) point
    dal.close()


def test_screen_integrates_baseline_detectors(tmp_path):
    """End to end: screen() picks up the occupancy deviation via a stored baseline."""
    dal = _seed_corr(tmp_path / "s.db")
    # give OAT a tight occupied baseline, then a wildly off recent reading
    now = _ts(2026, 6, 30, 12)                      # Tuesday noon (occupied)
    dal.upsert_baseline(1, "occ", mean=45.0, std=1.0, n=300, changed_frac=0.5,
                        long_slope=0.0, window_start_utc=0, window_end_utc=now, updated_utc=now)
    dal.upsert_baseline(1, "unocc", mean=40.0, std=1.0, n=300, changed_frac=0.5,
                        long_slope=0.0, window_start_utc=0, window_end_utc=now, updated_utc=now)
    dal.write_samples([Sample(point_id=1, ts_utc=now, value_num=90.0, quality=Quality.GOOD)])
    found = dal_screen(dal, now - 3600, now + 60)
    assert any(a.kind == "deviation" and a.point_id == 1 for a in found)
    dal.close()


def dal_screen(dal, start, end):
    from hvactrend.ai.prescreen import screen
    return screen(dal, start, end, localtime=GM)


# ── role-derived physics checks (P1) ─────────────────────────────────────────
def _seed_roles_system(db_path, roled_points):
    """Seed one AC-1 system with explicit-role points. roled_points: list of
    (point_id, role, canonical, samples[(value_num, value_text)])."""
    dal = SQLiteDAL(db_path)
    dal.apply_schema()
    c = dal._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'DEMO','Demo Building')")
    c.execute("INSERT INTO buildings(id,site_id,code,name) VALUES (1,1,'MAIN','Main')")
    c.execute("INSERT INTO systems(id,building_id,code,name,kind) VALUES (1,1,'AC-1','AC-1','ahu')")
    c.execute("INSERT INTO devices(id,site_id,device_instance,address,created_utc) "
              "VALUES (1,1,100100,'192.0.2.19',0)")
    ss = []
    for pid, role, canon, samples in roled_points:
        c.execute("INSERT INTO points(id,device_id,system_id,source,object_type,"
                  "object_instance,name,units_text,enabled,role,role_canonical,created_utc) "
                  "VALUES (?,1,1,'bacnet','analog-input',?,?,?,1,?,?,0)",
                  (pid, pid, role, role, role, 1 if canon else 0))
        for i, (vn, vt) in enumerate(samples):
            ss.append(Sample(point_id=pid, ts_utc=1000 + i * 300,
                             value_num=vn, value_text=vt, quality=Quality.GOOD))
    dal.write_samples(ss)
    return dal


def test_derived_physics_checks(tmp_path):
    from hvactrend.ai.derived import diagnose
    dal = _seed_roles_system(tmp_path / "d.db", [
        (1, "DAT", 1, [(70.0, None)] * 3),             # 15F above setpoint
        (2, "DAT_ACTIVE_SP", 1, [(55.0, None)] * 3),
        (3, "HTG_VALVE", 1, [(50.0, None)] * 3),       # heating 50% ...
        (4, "COOLING_STAGES", 0, [(1.0, None)] * 3),   # ...while cooling on
        (5, "MAT", 1, [(40.0, None)] * 3),             # implausibly low mixed air
        (6, "RAT", 1, [(72.0, None)] * 3),
        (7, "OAT", 1, [(90.0, None)] * 3),             # hot outdoors ...
        (8, "OA_DAMPER", 1, [(80.0, None)] * 3),       # ...but damper wide open
        (9, "MODE", 0, [(1.0, "Heat")] * 3),           # mode=Heat but cooling active
        (10, "COMM", 0, [(0.0, "Not Communicating")] * 3),
    ])
    pts = dal.points_for_analysis()
    # the DAT-vs-setpoint alarm is opt-in (analysis.checks.dat_vs_setpoint) — enable it
    # here so this check exercises the full physics set
    cfg = {"analysis": {"checks": {"dat_vs_setpoint": True}}}
    kinds = {a.kind for a in diagnose(dal, pts, 0, 100000, config=cfg)}
    for expected in ("dat-deviation", "sim-heat-cool", "mixed-air",
                     "economizer", "mode", "comm-loss"):
        assert expected in kinds, f"missing {expected}: got {kinds}"
    # and with the alarm OFF (default) the discharge-vs-setpoint lead is suppressed
    off = {a.kind for a in diagnose(dal, pts, 0, 100000)}
    assert "dat-deviation" not in off
    dal.close()


def test_derived_no_false_positives_when_healthy(tmp_path):
    from hvactrend.ai.derived import diagnose
    dal = _seed_roles_system(tmp_path / "h.db", [
        (1, "DAT", 1, [(55.5, None)] * 3),             # tracking setpoint
        (2, "DAT_ACTIVE_SP", 1, [(55.0, None)] * 3),
        (3, "HTG_VALVE", 1, [(0.0, None)] * 3),        # no heating
        (4, "COOLING_STAGES", 0, [(1.0, None)] * 3),   # cooling only
        (5, "MAT", 1, [(74.0, None)] * 3),             # between OA and RA at 20% OA
        (6, "RAT", 1, [(75.0, None)] * 3),
        (7, "OAT", 1, [(70.0, None)] * 3),
        (8, "OA_DAMPER", 1, [(20.0, None)] * 3),
        (9, "MODE", 0, [(1.0, "Cool")] * 3),           # mode matches cooling
    ])
    pts = dal.points_for_analysis()
    kinds = {a.kind for a in diagnose(dal, pts, 0, 100000)}
    assert kinds == set(), f"unexpected findings on a healthy unit: {kinds}"
    dal.close()


def test_role_peers_across_systems(tmp_path):
    """'Add this reading across all AHUs' — role-based peers = the canonical point
    for the same role on every system."""
    dal = SQLiteDAL(tmp_path / "peers.db")
    dal.apply_schema()
    c = dal._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'DEMO','Demo Building')")
    c.execute("INSERT INTO buildings(id,site_id,code,name) VALUES (1,1,'MAIN','M')")
    c.execute("INSERT INTO devices(id,site_id,device_instance,address,created_utc) VALUES (1,1,1,'a',0)")
    pid = 1
    for sysc in ("AC-1", "AC-2", "AC-3"):
        c.execute("INSERT INTO systems(building_id,code,name,kind) VALUES (1,?,?,'ahu')", (sysc, sysc))
        sid = c.execute("SELECT id FROM systems WHERE code=?", (sysc,)).fetchone()["id"]
        # a logical DAT twin + the wired UIP twin (UIP should be the canonical peer)
        for nm in (f"Discharge Air Temperature|{sysc}", f"Discharge Air Temp|{sysc}_UIP~_06"):
            c.execute("INSERT INTO points(id,device_id,system_id,source,object_type,"
                      "object_instance,name,enabled,created_utc) VALUES (?,1,?,'bacnet','analog-input',?,?,1,0)",
                      (pid, sid, pid, nm))
            pid += 1
    roles.assign_roles(dal)
    dat_uip = c.execute("SELECT id FROM points WHERE name LIKE '%UIP%' AND system_id=1").fetchone()["id"]
    peers = dal.role_peers(dat_uip)
    assert {p["system_code"] for p in peers} == {"AC-1", "AC-2", "AC-3"}      # one per system
    assert all("UIP" in p["name"] for p in peers)                            # the wired sensor
    dal.close()


def test_fleet_outlier_compares_control_error_not_raw_readings():
    """THE AC-1 CASE, from a real report: 'AC-1 running as a fleet DAT outlier at 68.7F
    occupied mean (+8.3 sigma vs 57.5F fleet mean)'. AC-1's discharge setpoint is 69.0
    while the rest of the fleet runs 55-61 — so AC-1 was 0.3F off its OWN target, which
    is near-perfect control, and was reported as an 8-sigma fault in every report. The
    mirror image is worse: a unit genuinely 3F off its setpoint scored near ZERO sigma,
    because 3F is small next to the 14F spread of setpoints. The detector was
    anti-informative until it compared CONTROL ERROR."""
    from hvactrend.ai.derived import detect_fleet_outliers

    def unit(code, dat, sp, pid):
        return {"code": code, "running": True,
                "roles": {"DAT": {"mean": dat, "pid": pid, "name": f"{code} DAT"},
                          "DAT_ACTIVE_SP": {"mean": sp, "pid": pid + 100,
                                            "name": f"{code} SP"}}}

    # the real fleet: everyone holding their own setpoint, AC-1 commissioned 14F warmer
    srv = {1: unit("AC-1", 68.7, 69.0, 1),      # 0.3F off its own target
           2: unit("AC-3", 58.0, 58.2, 2),
           3: unit("AC-5", 55.1, 55.0, 3),
           4: unit("AC-6", 55.9, 55.7, 4),
           5: unit("AC-7", 55.5, 55.7, 5),
           6: unit("AC-8", 56.9, 56.7, 6)}
    hits = detect_fleet_outliers(srv)
    assert not [a for a in hits if a.system_code == "AC-1"], \
        "a unit holding its own setpoint is not a peer outlier"

    # ...and the unit that is ACTUALLY failing to make setpoint now stands out, even
    # though its raw temperature sits comfortably inside the fleet's range
    srv[7] = unit("AC-12", 61.0, 55.0, 7)       # 6F above its target
    hits = detect_fleet_outliers(srv)
    flagged = {a.system_code for a in hits if a.kind == "fleet-outlier"}
    assert "AC-12" in flagged and "AC-1" not in flagged
    ac12 = [a for a in hits if a.system_code == "AC-12"][0]
    assert ac12.evidence["vs_own_setpoint"] is True
    assert "error" in ac12.detail and "setpoint" in ac12.detail


def test_fleet_outlier_needs_a_physically_meaningful_gap():
    """Sigma alone is not evidence. On a tightly-matched fleet a trivial spread still
    scores high — a 0.12 in-wc duct-static difference is a well-controlled plant, not a
    '+3.0 sigma outlier'."""
    from hvactrend.ai.derived import detect_fleet_outliers

    def unit(code, v, pid):
        return {"code": code, "running": True,
                "roles": {"DUCT_PRESS": {"mean": v, "pid": pid, "name": f"{code} SP"}}}

    srv = {i: unit(f"AC-{i}", 1.00 + (i % 2) * 0.005, i) for i in range(1, 7)}
    srv[7] = unit("AC-7", 1.12, 7)              # statistically far, physically nothing
    assert not [a for a in detect_fleet_outliers(srv) if a.system_code == "AC-7"]

    srv[7] = unit("AC-7", 1.60, 7)              # a real 0.6 in-wc gap still flags
    assert [a for a in detect_fleet_outliers(srv) if a.system_code == "AC-7"]


def test_fleet_outlier_falls_back_to_raw_when_the_plant_has_no_setpoints():
    """Being strict about needing a target would SILENTLY DISABLE the detector on a
    plant that doesn't trend setpoints — the dead-detector failure this codebase keeps
    producing. Fall back to raw comparison instead, but never MIX errors and raw values
    in one population (that mixture is how the OA-damper false positive worked)."""
    from hvactrend.ai.derived import detect_fleet_outliers

    def unit(code, dat, pid, sp=None):
        roles = {"DAT": {"mean": dat, "pid": pid, "name": f"{code} DAT"}}
        if sp is not None:
            roles["DAT_ACTIVE_SP"] = {"mean": sp, "pid": pid + 100, "name": "sp"}
        return {"code": code, "running": True, "roles": roles}

    # nobody publishes a setpoint -> raw comparison still works
    srv = {i: unit(f"AC-{i}", 55.0 + (i % 2), i) for i in range(1, 7)}
    srv[7] = unit("AC-7", 85.0, 7)
    hits = detect_fleet_outliers(srv)
    assert {a.system_code for a in hits} == {"AC-7"}
    assert hits[0].evidence["vs_own_setpoint"] is False

    # only ONE unit publishes one -> still below the quorum, so still raw (not a
    # population of one error compared against five raw temperatures)
    srv[2] = unit("AC-2", 56.0, 2, sp=56.0)
    assert {a.system_code for a in detect_fleet_outliers(srv)} == {"AC-7"}


def test_fleet_outlier_detection():
    from hvactrend.ai.derived import detect_fleet_outliers

    def sysd(code, dat, running=True):
        return {"code": code, "running": running,
                "roles": {"DAT": {"mean": dat, "pid": 1, "name": f"{code} DAT"}}}

    # 6 units near 55 F, one at 85 F -> the 85 is the fleet outlier
    srv = {i: sysd(f"AC-{i}", 55.0 + (i % 2)) for i in range(1, 7)}
    srv[7] = sysd("AC-7", 85.0)
    hits = detect_fleet_outliers(srv)
    outliers = {a.system_code for a in hits if a.kind == "fleet-outlier"}
    assert outliers == {"AC-7"}

    # an IDLE unit that's far off is NOT compared (legitimately different)
    srv[8] = sysd("AC-8", 40.0, running=False)
    codes = {a.system_code for a in detect_fleet_outliers(srv)}
    assert "AC-8" not in codes

    # too few peers -> no comparison
    assert detect_fleet_outliers({1: sysd("AC-1", 55), 2: sysd("AC-2", 90)}) == []


def test_derived_idle_unit_not_flagged(tmp_path):
    """A fan that's off must NOT be flagged for 'not making setpoint' — the
    vacant-floor units idle this way (real false-positive source)."""
    from hvactrend.ai.derived import diagnose
    dal = _seed_roles_system(tmp_path / "idle.db", [
        (1, "DAT", 1, [(76.0, None)] * 3),             # far off setpoint...
        (2, "DAT_ACTIVE_SP", 1, [(55.0, None)] * 3),
        (3, "DUCT_PRESS", 1, [(0.05, None)] * 3),      # ...and no static...
        (4, "DUCT_PRESS_SP", 1, [(1.2, None)] * 3),
        (5, "FAN_SPEED", 1, [(0.0, None)] * 3),        # ...because the fan is OFF
    ])
    pts = dal.points_for_analysis()
    kinds = {a.kind for a in diagnose(dal, pts, 0, 100000)}
    assert "dat-deviation" not in kinds and "duct-static" not in kinds, kinds
    dal.close()


# ── per-unit occupancy schedules (staggered starts / weekend variance) ────────
def test_daymap_resolves_mode_then_override_then_building_default():
    building = default_daymap({"weekday_start": 5, "weekday_end": 18,
                               "weekend_occupied": False})
    assert building[0] == (300, 1080) and building[5] is None      # Mon 5-18, Sat off

    # no override -> inherits the building default
    assert daymap_for_system({"occupancy_mode": "scheduled"}, building)[0] == (300, 1080)
    # VACANT wins over everything: never occupied
    assert all(v is None for v in daymap_for_system({"occupancy_mode": "vacant"}, building).values())
    # ALWAYS -> 24/7
    assert daymap_for_system({"occupancy_mode": "always"}, building)[2] == (0, 1440)

    # a staggered start + a Saturday shift; unspecified days inherit
    sched = '{"mon":[350,1080],"sat":[480,780]}'
    dm = daymap_for_system({"occupancy_mode": "scheduled", "occ_schedule": sched}, building)
    assert dm[0] == (350, 1080)          # Mon starts 05:50 (staggered)
    assert dm[5] == (480, 780)           # Sat 08:00-13:00
    assert dm[1] == (300, 1080)          # Tue not specified -> building default
    assert dm[6] is None                 # Sun still off

    # malformed JSON must not explode — fall back to the building default
    assert daymap_for_system({"occ_schedule": "{not json"}, building)[0] == (300, 1080)


def test_staggered_start_changes_the_occupied_window():
    """The accuracy point: a unit that starts at 05:50 must NOT count 05:00-05:50 as
    occupied (that hour of legitimately-off operation would pollute its occupied band)."""
    import time as _t
    building = default_daymap({"weekday_start": 5, "weekday_end": 18})
    early = occ_pred(daymap_for_system({}, building))
    late = occ_pred(daymap_for_system({"occ_schedule": '{"tue":[350,1080]}'}, building))
    # Tue 05:20 local
    ts = int(_t.mktime((2026, 7, 21, 5, 20, 0, 1, 202, -1)))
    assert early(ts) is True             # building schedule says occupied at 05:20
    assert late(ts) is False             # this unit doesn't start until 05:50
    ts2 = int(_t.mktime((2026, 7, 21, 6, 0, 0, 1, 202, -1)))
    assert early(ts2) is True and late(ts2) is True   # both occupied by 06:00


# ── baseline window: one configured value, honored by every trigger ───────────
def test_baseline_window_days_comes_from_config():
    """It previously did not: scripts/build_model.py hard-coded 30 while the UI's
    'Rebuild model' button fell through to the module default of 14, so the SAME rebuild
    produced different bands depending on how it was started — and the configured
    analysis.baseline.window_days was read by neither."""
    from hvactrend.ai.baseline import baseline_window_days, _DEFAULT_WINDOW_DAYS

    assert baseline_window_days({"analysis": {"baseline": {"window_days": 30}}}) == 30
    # absent / malformed / nonsensical -> the documented default, never a crash
    assert baseline_window_days(None) == _DEFAULT_WINDOW_DAYS
    assert baseline_window_days({}) == _DEFAULT_WINDOW_DAYS
    assert baseline_window_days({"analysis": {}}) == _DEFAULT_WINDOW_DAYS
    assert baseline_window_days({"analysis": {"baseline": {"window_days": 0}}}) == _DEFAULT_WINDOW_DAYS
    assert baseline_window_days({"analysis": {"baseline": {"window_days": "x"}}}) == _DEFAULT_WINDOW_DAYS


def test_build_baselines_uses_the_configured_window(tmp_path):
    """The window actually reaches the query: a 30-day config window must include a
    sample 20 days old that a 14-day window would miss entirely."""
    from hvactrend.ai.baseline import build_baselines
    from hvactrend.models import Quality, Sample
    from hvactrend.storage.sqlite_dal import SQLiteDAL

    dal = SQLiteDAL(tmp_path / "win.db")
    dal.apply_schema()
    c = dal._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'DEMO','Demo Building')")
    c.execute("INSERT INTO buildings(id,site_id,code,name) VALUES (1,1,'MAIN','M')")
    c.execute("INSERT INTO systems(id,building_id,code,name,kind) VALUES (1,1,'AC-1','AC-1','ahu')")
    c.execute("INSERT INTO devices(id,site_id,device_instance,address,created_utc) "
              "VALUES (1,1,1,'192.0.2.1',0)")
    c.execute("INSERT INTO points(id,device_id,system_id,source,object_type,object_instance,"
              "name,units_text,enabled,role,created_utc) VALUES "
              "(1,1,1,'bacnet','analog-input',1,'Discharge Air Temp','degrees-fahrenheit',1,'DAT',0)")
    now = 40 * 86400
    # 30 samples at ~20 days old — inside a 30-day window, outside a 14-day one
    dal.write_samples([Sample(point_id=1, ts_utc=now - 20 * 86400 + i * 120,
                              value_num=55.0 + (i % 3), quality=Quality.GOOD)
                       for i in range(30)])

    def _n():   # both modes are always written (n=0 when empty), so sum them
        b = dal.baselines()[1]
        return sum((b.get(m) or {}).get("n", 0) or 0 for m in ("occ", "unocc"))

    wide = build_baselines(dal, {"analysis": {"baseline": {"window_days": 30}}}, now_utc=now)
    assert wide["window_days"] == 30 and wide["points"] == 1
    n_wide = _n()

    narrow = build_baselines(dal, {"analysis": {"baseline": {"window_days": 14}}}, now_utc=now)
    assert narrow["window_days"] == 14
    n_narrow = _n()
    assert n_wide == 30 and n_narrow == 0, (n_wide, n_narrow)

    # an explicit argument still wins over config (the CLI override)
    assert build_baselines(dal, {"analysis": {"baseline": {"window_days": 30}}},
                           window_days=7, now_utc=now)["window_days"] == 7
    dal.close()


# ── OA damper vs its operator-set minimum (2026-07-23) ────────────────
def test_oa_damper_at_raised_minimum_is_not_a_fleet_outlier():
    """The operator raised min-OA to 35% on some units. A damper parked at its OWN minimum is
    not a peer outlier just because other units sit at a different (20%) minimum — the
    fleet comparison is on EXCESS ABOVE each unit's minimum, so 'all at their min' ties."""
    from hvactrend.ai.derived import detect_fleet_outliers

    def unit(code, damper, minoa, *, pid):
        return {"code": code, "running": True, "vacant": False,
                "roles": {"OA_DAMPER": {"mean": damper, "pid": pid, "name": code+" OAD"},
                          "OA_DAMPER_MIN_SP": {"mean": minoa, "pid": pid+100, "name": code+" min"}}}
    # AC-1/2 at 35% min, others at 20% min — every damper sitting at its own minimum
    fleet = {1: unit("AC-1", 35.0, 35.0, pid=1), 2: unit("AC-2", 35.0, 35.0, pid=2),
             3: unit("AC-3", 20.0, 20.0, pid=3), 4: unit("AC-4", 40.0, 40.0, pid=4),
             5: unit("AC-5", 20.0, 20.0, pid=5), 6: unit("AC-6", 20.0, 20.0, pid=6)}
    outs = [a for a in detect_fleet_outliers(fleet) if a.metric == "OA_DAMPER"]
    assert outs == [], f"a damper at its own minimum was flagged: {[a.detail for a in outs]}"

    # ...but a genuinely stuck-OPEN damper (80% while its min is 20%, peers ~0 excess)
    # still stands out as an outlier
    stuck = dict(fleet); stuck[6] = unit("AC-6", 80.0, 20.0, pid=6)
    flagged = [a.system_code for a in detect_fleet_outliers(stuck) if a.metric == "OA_DAMPER"]
    assert "AC-6" in flagged, "a stuck-open damper (60% above its min) must still flag"


def test_commanded_actuator_not_z_score_flagged_on_a_setpoint_change():
    """The +5.8sigma the report showed: an OA damper stepping from a 20% to a 35%
    operator-set minimum is a setpoint change, not a fault. Commanded outputs are not
    z-scored against their historical band (sensors still are)."""
    from hvactrend.ai.prescreen import detect_point_baseline
    # baseline learned at 20%, damper now parked at 35% -> would be a huge z-score
    band = {"occ": {"mean": 20.0, "std": 2.5, "n": 500},
            "unocc": {"mean": 20.0, "std": 2.5, "n": 500}, "changed_frac": 0.3}
    s = {"last_good": 35.0, "last_ts": 1_700_000_000}
    damper = {"id": 1, "role": "OA_DAMPER", "system_code": "AC-1", "name": "OA Damper",
              "object_type": "analog-output"}
    assert detect_point_baseline(damper, s, band) == []      # commanded -> no deviation
    # a real SENSOR at +6 sigma IS still flagged
    sensor = {"id": 2, "role": "MAT", "system_code": "AC-1", "name": "Mixed Air",
              "object_type": "analog-input"}
    devs = [a for a in detect_point_baseline(sensor, s, band) if a.kind == "deviation"]
    assert devs, "a real sensor deviation must still be caught"


def test_tower_approach_is_measured_only_while_the_tower_is_loaded(tmp_path):
    """A performance number averaged over hours the equipment was NOT working is not a
    performance number. Real data from CT-1: 12 daytime hours at 100% fan with an
    11-14 F approach, 12 night hours at its 33% idle floor with a 7-8 F approach. The
    24-hour mean is 9.6 F — under any sane threshold — so a window-average check reports
    a tower that is flat out and still losing ground as perfectly healthy. This is the
    same shape as the window-average airflow gate that once claimed eleven AHUs breached
    their DAT alarm limit overnight with the fans off."""
    from hvactrend.ai.derived import detect_tower_approach

    day, night = 12 * 30, 12 * 30          # 2-min samples
    fan = [100.0] * day + [33.0] * night   # 33% is the VFD floor, NOT "loaded"
    sup = [78.0] * day + [72.5] * night
    wb = 66.0
    t0 = 1_700_000_000
    rows = {"fan": [], "sup": []}
    for i, (f, sv) in enumerate(zip(fan, sup)):
        ts = t0 + i * 120
        rows["fan"].append({"ts_utc": ts, "value_num": f, "value_text": None, "quality": "good"})
        rows["sup"].append({"ts_utc": ts, "value_num": sv, "value_text": None, "quality": "good"})

    class _DAL:
        def query_series(self, pid, s, e):
            return rows["fan"] if pid == 1 else rows["sup"]

    points = [
        {"id": 1, "name": "CT1.SPD.FBK", "role": "CT_FAN_SPEED", "role_canonical": 1,
         "system_id": 9, "system_code": "CT-1"},
        {"id": 2, "name": "CT1.SUP.TEMP", "role": "CT_SUP_TEMP", "role_canonical": 1,
         "system_id": 9, "system_code": "CT-1"},
    ]
    end = t0 + len(fan) * 120
    found = detect_tower_approach(_DAL(), points, t0, end, {"wb_avg": wb})
    assert len(found) == 1, "a tower at full fan with a 12 F approach must be reported"
    ev = found[0].evidence
    # the loaded mean is 78-66 = 12 F; the 24 h blend would be ~9.25 F and report nothing
    assert 11.5 <= ev["approach_mean_f"] <= 12.5, ev
    assert ev["min_load_pct"] == 80.0

    # ...and a tower that is merely idling must NOT be reported, however wide the gap
    rows["fan"] = [dict(r, value_num=33.0) for r in rows["fan"]]
    assert detect_tower_approach(_DAL(), points, t0, end, {"wb_avg": wb}) == []


def test_command_feedback_ignores_a_drive_at_rest(tmp_path):
    """CT-1's fan tracks its command to 0.2% (100 commanded -> 99.8 proved) yet a
    mean-based comparison called it a 16-point drive fault, because with the command at
    0 the VFD feedback sits on its 33% minimum-speed floor and the 24 h means become
    52 vs 68. A drive at rest is not a drive at fault: only samples where something is
    actually being commanded may be compared."""
    from hvactrend.ai.derived import detect_command_vs_feedback_water

    t0 = 1_700_000_000
    cmd, fbk = [], []
    for i in range(720):
        ts = t0 + i * 120
        driven = 180 <= i < 540                       # 12 h commanded, 12 h off
        c = 100.0 if driven else 0.0
        f = 99.8 if driven else 33.3                  # healthy drive + its floor
        cmd.append({"ts_utc": ts, "value_num": c, "value_text": None, "quality": "good"})
        fbk.append({"ts_utc": ts, "value_num": f, "value_text": None, "quality": "good"})

    class _DAL:
        def query_series(self, pid, s, e):
            return cmd if pid == 1 else fbk

    points = [
        {"id": 1, "name": "CT1.SPDO", "role": "CT_FAN_SPEED_CMD", "role_canonical": 1,
         "system_id": 9, "system_code": "CT-1"},
        {"id": 2, "name": "CT1.SPD.FBK", "role": "CT_FAN_SPEED", "role_canonical": 1,
         "system_id": 9, "system_code": "CT-1"},
    ]
    end = t0 + 720 * 120
    assert detect_command_vs_feedback_water(_DAL(), points, t0, end) == [], \
        "a drive tracking its command to 0.2% must never be reported as a fault"

    # a genuinely stuck drive — commanded hard, proved nothing — still IS reported
    fbk[:] = [dict(r, value_num=10.0) if 180 <= i < 540 else r
              for i, r in enumerate(fbk)]
    found = detect_command_vs_feedback_water(_DAL(), points, t0, end)
    assert len(found) == 1 and found[0].evidence["worst_commanded_pct"] == 100.0, found


def test_tower_approach_names_an_out_of_service_peer_instead_of_blaming_fill():
    """A single tower carrying a two-tower load saturates its fan and opens its approach
    by arithmetic. Reporting that as "fouled fill, scale or restricted airflow" sends a
    technician up a ladder to inspect a tower that is working correctly — the real cause
    being the OTHER tower, which is down for a fan shaft.

    The finding must still appear (the plant genuinely has no spare capacity) but must
    name the absent peer. Availability is judged from DATA — fan never above its floor
    AND the isolation valve never opened — so it is right even before anyone marks the
    unit out of service in the UI."""
    from hvactrend.ai.derived import detect_tower_approach, _towers_out_of_service

    t0, n = 1_700_000_000, 720
    live_fan = [100.0] * n
    dead_fan = [-1.9] * n            # a stopped VFD reads a small negative offset
    dead_iso = [0.0] * n
    sup = [78.0] * n

    def series(vals):
        return [{"ts_utc": t0 + i * 120, "value_num": v, "value_text": None,
                 "quality": "good"} for i, v in enumerate(vals)]

    data = {1: series(live_fan), 2: series(sup), 3: series(dead_fan), 4: series(dead_iso)}

    class _DAL:
        def query_series(self, pid, s, e):
            return data[pid]

    points = [
        {"id": 1, "name": "CT1.SPD", "role": "CT_FAN_SPEED", "role_canonical": 1,
         "system_id": 1, "system_code": "CT-1"},
        {"id": 2, "name": "CT1.TEMP", "role": "CT_SUP_TEMP", "role_canonical": 1,
         "system_id": 1, "system_code": "CT-1"},
        {"id": 3, "name": "CT2.SPD", "role": "CT_FAN_SPEED", "role_canonical": 1,
         "system_id": 2, "system_code": "CT-2"},
        {"id": 4, "name": "CT2.ISO", "role": "CT_ISO_VALVE", "role_canonical": 1,
         "system_id": 2, "system_code": "CT-2"},
    ]
    end = t0 + n * 120
    assert _towers_out_of_service(_DAL(), points, t0, end) == ["CT-2"]

    found = detect_tower_approach(_DAL(), points, t0, end, {"wb_avg": 66.0})
    ct1 = [f for f in found if f.system_code == "CT-1"]
    assert len(ct1) == 1
    d, ev = ct1[0].detail, ct1[0].evidence
    assert ev["n_minus_1"] is True and ev["peers_out_of_service"] == ["CT-2"]
    assert "CT-2" in d and "OUT OF SERVICE" in d
    assert "fouling" not in d.lower() or "not evidence of fouling" in d.lower(), \
        "must not accuse the working tower's fill while its partner is down"

    # ...and with BOTH towers available the fouling wording is the correct one
    data[3] = series([100.0] * n)
    data[4] = series([100.0] * n)
    both = detect_tower_approach(_DAL(), points, t0, end, {"wb_avg": 66.0})
    ct1b = [f for f in both if f.system_code == "CT-1"][0]
    assert ct1b.evidence["n_minus_1"] is False
    assert "fouled fill" in ct1b.detail


def test_bypass_valve_command_and_feedback_survive_as_two_points():
    """The condenser bypass valve publishes a commanded position (SITE.CHW.BYPO) and a
    proved one (SITE.CHW.BYP.POS). Both classified as CW_BYPASS_VALVE, and because
    canonical_ids keeps exactly ONE point per (system, role), the command was silently
    discarded -- leaving the plant's only modulating bypass with no way to detect the
    one thing that actually goes wrong with it: sticking.

    Every other water-side actuator (fan speed, pump speed, isolation valve) already
    splits command from feedback; this one was the exception."""
    from hvactrend.ai.roles import classify, canonical_ids, ANALYSIS_ROLES, STATUS_ROLES
    from hvactrend.ai.derived import detect_command_vs_feedback_water

    fbk_role = classify("SITE.CHW.BYP.POS", "Bypass Valve", None)
    cmd_role = classify("SITE.CHW.BYPO", "Bypass Valve SP", None)
    assert fbk_role == "CW_BYPASS_VALVE"
    assert cmd_role == "CW_BYPASS_VALVE_CMD"
    assert fbk_role != cmd_role, "collapsing these loses one of them to canonical_ids"

    # the panel's trailing-O output convention must carry the split on its own, so
    # clearing the operator's rename cannot quietly re-fuse the pair
    assert classify("SITE.CHW.BYPO", None, None) == "CW_BYPASS_VALVE_CMD"

    # a position is z-scored; a command is discrete and must never be
    assert fbk_role in ANALYSIS_ROLES and cmd_role not in ANALYSIS_ROLES
    assert cmd_role in STATUS_ROLES

    pts = [
        {"id": 1, "name": "SITE.CHW.BYP.POS", "role": fbk_role, "system_id": 15,
         "enabled": 1, "units_text": "percent", "object_type": "analogInput"},
        {"id": 2, "name": "SITE.CHW.BYPO", "role": cmd_role, "system_id": 15,
         "enabled": 1, "units_text": "percent", "object_type": "analogOutput"},
    ]
    assert canonical_ids(pts) == {1, 2}, "both halves of the pair must stay canonical"

    # ...and the pair is actually COMPARED -- a split role that nothing reads is not a check
    t0, n = 1_700_000_000, 400
    cmd_rows, fbk_rows = [], []
    for i in range(n):
        ts = t0 + i * 120
        c = 80.0                       # driven hard for the whole window
        f = 4.0                        # valve stuck nearly shut
        cmd_rows.append({"ts_utc": ts, "value_num": c, "value_text": None, "quality": "good"})
        fbk_rows.append({"ts_utc": ts, "value_num": f, "value_text": None, "quality": "good"})

    class _DAL:
        def query_series(self, pid, s, e):
            return cmd_rows if pid == 2 else fbk_rows

    points = [dict(p, role_canonical=1, system_code="CT") for p in pts]
    found = detect_command_vs_feedback_water(_DAL(), points, t0, t0 + n * 120)
    assert len(found) == 1, "a bypass valve stuck shut while commanded open must report"
    assert "bypass valve" in found[0].detail
    assert found[0].evidence["worst_commanded_pct"] == 80.0

    # a healthy valve tracking its command stays silent
    fbk_rows[:] = [dict(r, value_num=79.4) for r in fbk_rows]
    assert detect_command_vs_feedback_water(_DAL(), points, t0, t0 + n * 120) == []


def test_the_separator_pump_is_not_a_condenser_pump():
    """The Lakos separator's pump runs all the time. Classified CW_PUMP_STATUS (its tag
    reads "FLT STS", its description "FLT PUMP STS"), it was proof that a condenser pump
    was on for every building-wide check, whatever the real pumps did; its DP sat in the
    loop DP's role beside the header DP."""
    from hvactrend.ai.roles import classify, STATUS_ROLES, ANALYSIS_ROLES
    assert classify("SITE.CHW.LKS.FLT.STS", None, "FLT  PUMP STS") == "CW_FILTER_STATUS"
    assert classify("SITE.CHW.LKS.FLT.DPTE", None, "FILTER DP") == "CW_FILTER_DP"
    assert classify("SITE.CHW.LKS.FLT.ALM", None, None) == "CW_FILTER_ALARM"
    assert "CW_FILTER_STATUS" in STATUS_ROLES and "CW_FILTER_DP" in ANALYSIS_ROLES
    # the real ones are untouched
    assert classify("SITE.CHW.PMP1.STS", None, None) == "CW_PUMP_STATUS"
    assert classify("SITE.CHW.HDR.DPTE", None, None) == "CW_DP"
    assert classify("SITE.CHW.PMP2.ALM", None, None) == "CW_PUMP_ALARM"


def test_a_standby_pump_at_rest_is_not_a_drive_fault():
    """CWP-2 sat at a 25% speed REFERENCE for 13 days with its status point reading 0 the
    whole time -- it is the standby pump and has never once run. The -4% feedback is the
    idle 4-20 mA zero offset on a stopped drive. Comparing the two produced a HIGH
    "100% disagreement -- drive fault, broken linkage" on healthy, switched-off equipment.

    _CMD_MIN_PCT was the first-order guard and it reads the wrong signal: the speed
    reference says what the drive was ASKED for, not whether it is turning. Only the
    proved run status can tell those apart."""
    from hvactrend.ai.derived import detect_command_vs_feedback_water

    t0, n = 1_700_000_000, 400
    cmd, fbk, sts = [], [], []
    for i in range(n):
        ts = t0 + i * 120
        cmd.append({"ts_utc": ts, "value_num": 25.0, "value_text": None, "quality": "good"})
        fbk.append({"ts_utc": ts, "value_num": -4.12, "value_text": None, "quality": "good"})
        sts.append({"ts_utc": ts, "value_num": 0.0, "value_text": None, "quality": "good"})

    class _DAL:
        def query_series(self, pid, s, e):
            return {1: cmd, 2: fbk, 3: sts}[pid]

    points = [
        {"id": 1, "name": "PMP2VSO", "role": "CW_PUMP_SPEED_CMD", "role_canonical": 1,
         "system_id": 17, "system_code": "CWP-2"},
        {"id": 2, "name": "PMP2.SPD.FBK", "role": "CW_PUMP_SPEED", "role_canonical": 1,
         "system_id": 17, "system_code": "CWP-2"},
        {"id": 3, "name": "PMP2.STS", "role": "CW_PUMP_STATUS", "role_canonical": 1,
         "system_id": 17, "system_code": "CWP-2"},
    ]
    end = t0 + n * 120
    assert detect_command_vs_feedback_water(_DAL(), points, t0, end) == [],         "a pump that never ran must not be reported as a failed drive"

    # the SAME numbers with the pump actually proving RUN are a genuine fault
    sts[:] = [dict(r, value_num=1.0) for r in sts]
    found = detect_command_vs_feedback_water(_DAL(), points, t0, end)
    assert len(found) == 1 and found[0].severity.name == "HIGH", found

    # status can arrive as the controller's own state text, not a number
    sts[:] = [dict(r, value_num=None, value_text="inactive") for r in sts]
    assert detect_command_vs_feedback_water(_DAL(), points, t0, end) == []
    sts[:] = [dict(r, value_num=None, value_text="Active") for r in sts]
    assert len(detect_command_vs_feedback_water(_DAL(), points, t0, end)) == 1

    # a device with NO run status (a modulating valve) keeps the old command-only gate
    valve = [dict(p) for p in points[:2]]
    valve[0]["role"], valve[1]["role"] = "CW_BYPASS_VALVE_CMD", "CW_BYPASS_VALVE"
    cmd[:] = [dict(r, value_num=80.0) for r in cmd]
    assert len(detect_command_vs_feedback_water(_DAL(), valve, t0, end)) == 1


def test_time_series_water_checks_get_more_window_than_the_alert_screen_asks_for():
    """The alert path screens a 30-minute window (alerts.window_s=1800). At a 120 s poll
    that is 15 samples -- fewer than the 30 the command/feedback comparison requires, and
    far short of the 2 loaded hours the approach check needs. Both were therefore
    STRUCTURALLY unable to fire there, while the report path that would have caught them
    has never been scheduled. Measured before the fix: 30 min -> nothing, 6 h -> both.

    The floor must not SHRINK a caller that asked for more: the 24 h / 7 d report path
    has to keep its full window."""
    from hvactrend.ai.derived import diagnose, _WATER_TS_WINDOW_S

    seen, wx = [], []

    class _DAL:
        def query_series(self, pid, s, e):
            seen.append((s, e))
            return []
        def weather_window(self, s, e):
            wx.append((s, e))
            # Weather rows are HOUR-FLOORED, so a short window genuinely contains none.
            # Modelling that is the point: widening only the sample window left the
            # approach check just as silent, because it returns early without a wet-bulb.
            return {"wb_avg": 66.0} if (e - s) >= 3600 else {}
        def equipment_by_system(self):
            return {}

    def _pt(i, name, role, sid, code):
        return {"id": i, "name": name, "role": role, "role_canonical": 1,
                "system_id": sid, "system_code": code, "enabled": 1}

    points = [
        _pt(1, "CT1.SPD.FBK", "CT_FAN_SPEED", 18, "CT-1"),
        _pt(2, "CT1.SUP.TEMP", "CT_SUP_TEMP", 18, "CT-1"),
        # a full command/feedback pair, or that detector returns before it queries
        _pt(3, "PMP1VSO", "CW_PUMP_SPEED_CMD", 16, "CWP-1"),
        _pt(4, "PMP1.SPD.FBK", "CW_PUMP_SPEED", 16, "CWP-1"),
    ]
    end = 1_700_000_000

    seen.clear(); wx.clear()
    diagnose(_DAL(), points, end - 1800, end, preds={})
    assert seen, "expected the water detectors to query something"
    assert min(s for s, _ in seen) <= end - _WATER_TS_WINDOW_S,         "a 30-minute alert window must be floored up for the time-series checks"
    assert min(s for s, _ in wx) <= end - _WATER_TS_WINDOW_S,         "the WEATHER window must be floored too, or the approach check has no wet-bulb"

    # ...and a caller that asked for 7 days keeps all 7
    seen.clear(); wx.clear()
    week = 7 * 86400
    diagnose(_DAL(), points, end - week, end, preds={})
    assert min(s for s, _ in seen) == end - week,         "the floor must never shorten the weekly report's window"


def test_a_tower_below_its_setpoint_is_not_a_fault():
    """A cooling tower can only REJECT heat. On a cool night the supply falls below
    setpoint and the only control action left is to stage fans off and open the bypass --
    the plant working, not failing. The check was symmetric, so it reported that.

    Measured over 14 days on this plant: 110 of 305 hours breached +/-3 F, but 83 were
    BELOW (worst 9.1 F) and only 27 above -- about 75% false. Same conclusion the
    DAT_LO_LIMIT / RAT_HI_LIMIT breach-rate measurement reached in config.yaml: a limit the
    loop controls TOWARD is not an alarm threshold."""
    from hvactrend.ai.derived import detect_tower_plant

    def srv(sup, sp):
        return {1: {"code": "CT", "roles": {
            "CW_SUP_TEMP": {"mean": sup, "pid": 1, "name": "SITE.CHW.HDR.SUP.TEMP"},
            "CW_SUP_TEMP_SP": {"mean": sp, "pid": 2, "name": "SITE.CHW.HDR.SUP.TEMP.SP"},
        }}}

    # 9 F BELOW setpoint -- the exact worst case seen in the record. Must be silent.
    assert [a for a in detect_tower_plant(srv(66.0, 75.0), {})
            if a.kind == "cw-supply-setpoint"] == []

    # 6 F ABOVE setpoint -- the tower cannot make setpoint. That IS a finding.
    hot = [a for a in detect_tower_plant(srv(81.0, 75.0), {})
           if a.kind == "cw-supply-setpoint"]
    assert len(hot) == 1
    assert hot[0].evidence["direction"] == "above"
    assert hot[0].evidence["deviation_f"] == 6.0
    assert "ABOVE" in hot[0].detail

    # inside the band, either way -- silent
    for sup in (73.0, 77.0):
        assert [a for a in detect_tower_plant(srv(sup, 75.0), {})
                if a.kind == "cw-supply-setpoint"] == []
