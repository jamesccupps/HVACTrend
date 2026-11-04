"""Point coverage (coverage.py): what each monitoring rule needs, unit by unit.

2026-09-29: "make sure the points I have selected are the actual ones that we would
want and all of the ones we should be monitoring." These pin the review's answers:
  * a rule is ready only when every point it needs is LOGGED; one the controller has but
    that is not logged is "needs logging", and that point is offered;
  * the damper rule needs TWO distinct points (the command and a real feedback), not one
    point counted twice;
  * outdoor conditions are satisfied by ONE logged reading anywhere in the building;
  * a rule a unit has no points for at all is "n/a", not a gap;
  * plant rules apply by what the system holds, not by unit type;
  * "logged but used by no rule" never lists a role some rule reads, even elsewhere.
"""

import pathlib

import pytest

from hvactrend import coverage
from hvactrend.ai import roles


def _p(pid, code, kind, role, enabled, ot="analog-input", name=None):
    return {"id": pid, "system_code": code, "system_kind": kind, "role": role,
            "enabled": enabled, "object_type": ot, "name": name or f"{role} {pid}",
            "display_name": None}


def _ahu(code="AC-1", **over):
    """A Trane-like air handler: the curated points logged, a few left on the controller."""
    pts = [
        _p(1, code, "ahu", "DAT", 1), _p(2, code, "ahu", "DAT_ACTIVE_SP", 1),
        _p(3, code, "ahu", "DUCT_PRESS", 1), _p(4, code, "ahu", "DUCT_PRESS_SP", 1),
        _p(5, code, "ahu", "FAN_STATUS", 1, "binary-input"),
        _p(6, code, "ahu", "FAN_CMD", 1, "binary-output"),
        _p(7, code, "ahu", "FAN_SPEED", 1), _p(8, code, "ahu", "MAT", 1),
        _p(9, code, "ahu", "RAT", 1), _p(10, code, "ahu", "COOLING_STAGES", 1),
        _p(11, code, "ahu", "OA_DAMPER", 1, name="OA/RA Damper Mod"),          # the command
        _p(12, code, "ahu", "OA_DAMPER", 0, name="Outdoor Air Damper Position"),  # feedback
        _p(13, code, "ahu", "FILTER", 0, "binary-input", name="Filter Status"),
        _p(14, code, "ahu", "OCCUPANCY", 1),
        _p(15, code, "ahu", "OTHER", 1, name="Mystery Tag"),
        _p(16, code, "ahu", "HUMIDITY", 1),
    ]
    return pts


def _building_oat(enabled=1):
    return [_p(90, None, None, "OAT", enabled, name="Outdoor Temp (Trane)")]


def _rules(report, code):
    s = next(x for x in report["systems"] if x["code"] == code)
    return {r["id"]: r["status"] for r in s["rules"]}, s


def test_a_curated_unit_is_ready_where_it_is_logged_and_says_what_is_missing():
    rep = coverage.build(_ahu() + _building_oat())
    st, s = _rules(rep, "AC-1")
    assert st["sat-setpoint"] == "ready"
    assert st["static-setpoint"] == "ready"
    assert st["fan-proof"] == "ready"
    assert st["mat-range"] == "ready"            # OAT from the building, not per unit
    assert st["economizer"] == "ready"
    assert st["filter"] == "needs-logging"
    assert st["space-pressure"] == "n/a"          # this unit has no pressure points at all
    offered = {x["name"] for x in s["suggest"]}
    assert "Filter Status" in offered and "Outdoor Air Damper Position" in offered
    assert s["applicable"] == len(st) - 1 and s["ready"] == sum(v == "ready" for v in st.values())


def test_the_damper_rule_needs_two_distinct_points_not_one_counted_twice():
    rep = coverage.build(_ahu() + _building_oat())
    assert _rules(rep, "AC-1")[0]["damper-feedback"] == "needs-logging"
    pts = _ahu() + _building_oat()
    pts[11]["enabled"] = 1                        # log the position feedback too
    assert _rules(coverage.build(pts), "AC-1")[0]["damper-feedback"] == "ready"


def test_outdoor_conditions_need_one_logged_reading_somewhere():
    rep = coverage.build(_ahu() + _building_oat(enabled=0))
    st, _ = _rules(rep, "AC-1")
    assert st["mat-range"] == "needs-logging"            # the reading exists, unlogged
    assert [c["name"] for c in rep["unassigned_suggest"]] == ["Outdoor Temp (Trane)"]
    st, _ = _rules(coverage.build(_ahu()), "AC-1")       # no outdoor reading at all
    assert st["mat-range"] == "missing" and st["economizer"] == "missing"


def test_the_plant_is_judged_only_while_its_master_enable_says_it_should_run():
    """PPCL 2026-09-26: SITE.CHWSYS gates the whole plant. Unlogged, no plant alarm could
    tell 'shut down on purpose' from 'failed' -- the review must say so, and offer it,
    listed once under the system that owns it."""
    loop = [_p(60, "CT", "plant", "CW_SUP_TEMP", 1), _p(61, "CT", "plant", "CW_SUP_TEMP_SP", 1),
            _p(62, "CT", "plant", "CW_SYS_ENABLE", 0, "binary-value", name="SITE.CHWSYS"),
            _p(63, "CT", "plant", "CT_FAN_SPEED", 1)]
    pumps = [_p(70, "CWP-1", "plant", "CW_PUMP_CMD", 1, "binary-output"),
             _p(71, "CWP-1", "plant", "CW_PUMP_STATUS", 1, "binary-input")]
    towers = [_p(80, "CT-1", "plant", "CT_ISO_VALVE_CMD", 1),
              _p(81, "CT-1", "plant", "CT_ISO_VALVE", 1)]
    rep = coverage.build(loop + pumps + towers)
    st, s = _rules(rep, "CT")
    assert st["cw-flow-lost"] == "needs-logging" and st["cws-setpoint"] == "needs-logging"
    assert [c["name"] for c in s["suggest"]] == ["SITE.CHWSYS"]
    assert set(s["suggest"][0]["rules"]) >= {"cw-flow-lost", "cws-setpoint"}
    tower = next(x for x in rep["systems"] if x["code"] == "CT-1")
    assert tower["suggest"] == []                       # listed once, under its owner
    loop[2]["enabled"] = 1
    st, _ = _rules(coverage.build(loop + pumps + towers), "CT")
    assert st["cw-flow-lost"] == "ready" and st["cws-setpoint"] == "ready"


def test_the_plant_gate_points_are_classified():
    """Named in the 2026-09-26 PPCL; all four classified OTHER before."""
    assert roles.classify("SITE.CHWSYS", None, "MASTER ENABLE") == "CW_SYS_ENABLE"
    assert roles.classify("SITE.CTSYS", None, "TWR SYS ENABLE") == "CT_SYS_ENABLE"
    assert roles.classify("SITE.CT1.ABORT", None, "CT1 ABORT") == "CT_ABORT"
    assert roles.classify("SITE.NODE6.COMM.TM", None, "NODE6 COMM TMR") == "COMM"
    # and nothing that already classified moved
    assert roles.classify("SITE.CHW.PMP1PMP2ENA", None, "PUMP SYS ENABLE") == "CW_SYS_ENABLE"
    assert roles.classify("SITE.CT.LVL.FAIL", None, "LVL SENS FAILURE") == "CT_LEVEL_ALARM"
    assert roles.classify("SITE.CT1.SS", None, "TWR FAN S/S") == "CT_FAN_CMD"


def test_the_boiler_plant_is_classified_from_its_fused_tags():
    """PPCL 2026-09-18, the boiler sequence of operations. Before, every boiler point classified OTHER,
    and "BLR 1 ENABLE" landed in INTERNAL. This site fuses its boiler tags (HB3SUP, BLR2ENB.BAC,
    DHWTEMPBAC), and several have no description at all."""
    c = roles.classify
    assert c("SITE.BLR1.ST", None, "SUPPLY TEMP") == "BLR_SUP_TEMP"
    assert c("SITE.BLR3.RT", None, "RETURN TEMP") == "BLR_RET_TEMP"
    assert c("BLRST.BAC", None, "BLR HW SUP BAC") == "HW_SUP_TEMP"          # the plant header
    assert c("BLRSTPT.BAC", None, "BLRSTPT CS TK") == "HW_SUP_TEMP_SP"
    assert c("DHWTEMPBAC") == "DHW_TEMP"
    assert c("SITE.BLR1.CMD", None, "BLR 1 ENABLE") == "BLR_CMD"
    assert c("BLR2ENB.BAC", None, "BLR2ENB CS TK") == "BLR_CMD"
    assert c("BLR3ALM.BAC", None, "BLR3ALM CS TK") == "BLR_ALARM"
    assert c("SITE.BLR1.ISO", None, "BLR1 ISO COMD") == "BLR_ISO_VALVE_CMD"
    assert c("SITE.BLR1.CALL", None, "BLR 1 CALL") == "BLR_CALL"
    assert c("BLRBB2.BAC", None, "BLRBB2 CS TK") == "BLR_STAGING"
    assert c("SITE.BLR2.LT", None, "BLR2 LOW TEMP") == "BLR_LOW_TEMP_SP"
    assert c("HB3SUP", None, "BLR3 SUPPLY") == "BLR_SUP_TEMP"
    assert c("HB3FF", None, "BLR3 FLAME F") == "BLR_ALARM"
    assert c("HB3P1", None, "BLR3 CIRC 1") == "HW_PUMP_CMD"
    assert c("HB3P1F", None, "BLR3 CIRC1") == "HW_PUMP_STATUS"
    assert c("HB3HW0", None, "BLR ON TEMP") == "BLR_ON_SP"
    assert c("HB3HWS", None, "BLR OFF TEMP") == "BLR_OFF_SP"    # 'hws' is not a supply here
    assert c("HB3STATUS") == "BLR_STATUS"
    assert c("HB3DHWCALL") == "BLR_CALL"
    # by name alone a manual low-temp enable reads as a boiler enable; its label says what it is
    assert c("SITE.BLR1ENB", "Boiler 1 Low-Temp Enable (manual)", "BLR 1 LT ENABLE") == "BLR_CALL"
    assert c("DHWTEMPBAC", "Domestic Hot Water Temp") == "DHW_TEMP"
    assert c("BLRBB2.BAC", "Lag Boiler On (2 boilers)") == "BLR_STAGING"
    # the boiler ROOM is not the boiler plant (Trane SC-2's program 8): unchanged
    assert c("Boiler Rm. Space Temp|pgm-8_UIP~_04") == "ROOM_TEMP"
    assert c("Boiler Room Low Temp SP|pgm-8_AV~_001") == "ROOM_SP"
    # nor did the air or condenser side move (AC-1's "HW Valve" is 'hw', not a hot tag)
    assert c("HW Valve|dac-3_AOP~_03") == "HTG_VALVE"
    assert c("SITE.CHW.HDR.SUP.TEMP") == "CW_SUP_TEMP"
    for r in ("HW_SUP_TEMP", "HW_SUP_TEMP_SP", "BLR_SUP_TEMP", "BLR_RET_TEMP", "DHW_TEMP",
              "BLR_CMD", "BLR_STATUS", "BLR_ALARM", "BLR_ISO_VALVE_CMD", "BLR_CALL",
              "BLR_STAGING", "BLR_ON_SP", "BLR_OFF_SP", "BLR_LOW_TEMP_SP", "HW_PUMP_CMD",
              "HW_PUMP_STATUS", "HW_PUMP_ALARM"):
        assert r in roles.ALL_ROLES, r


def test_the_boiler_rules_attach_where_the_points_are():
    """The plant's rules land on the plant, each boiler's on that boiler, HB3's on HB3; no
    condenser rule attaches to a boiler and no boiler rule to an air handler."""
    plant = [_p(200, "BLR", "plant", "HW_SUP_TEMP", 1), _p(201, "BLR", "plant", "HW_SUP_TEMP_SP", 1),
             _p(202, "BLR", "plant", "DHW_TEMP", 1),
             _p(203, "BLR", "plant", "BLR_STAGING", 1, "binary-value")]
    b1 = [_p(210, "BLR-1", "plant", "BLR_SUP_TEMP", 1), _p(211, "BLR-1", "plant", "BLR_RET_TEMP", 1),
          _p(212, "BLR-1", "plant", "BLR_CMD", 1, "binary-output"),
          _p(213, "BLR-1", "plant", "BLR_ALARM", 1, "binary-input"),
          _p(214, "BLR-1", "plant", "BLR_ISO_VALVE_CMD", 1, "binary-output"),
          _p(215, "BLR-1", "plant", "BLR_CALL", 0, "binary-value")]
    hb3 = [_p(220, "HB3", "plant", "BLR_SUP_TEMP", 1),
           _p(221, "HB3", "plant", "BLR_CMD", 1, "binary-output"),
           _p(222, "HB3", "plant", "BLR_STATUS", 0, "binary-input"),
           _p(223, "HB3", "plant", "HW_PUMP_CMD", 1, "binary-output"),
           _p(224, "HB3", "plant", "HW_PUMP_STATUS", 1, "binary-input")]
    rep = coverage.build(plant + b1 + hb3 + _ahu() + _building_oat())
    assert _rules(rep, "BLR")[0] == {"hw-setpoint": "ready", "dhw-temp": "ready"}
    assert _rules(rep, "BLR-1")[0] == {"boiler-rise": "ready", "boiler-alarm": "ready",
                                       "backup-boiler": "ready"}
    st, s = _rules(rep, "HB3")
    assert st == {"boiler-proof": "needs-logging", "hw-pump-proof": "ready"}
    assert [(x["name"], sorted(x["rules"])) for x in s["suggest"]] == [
        ("BLR_STATUS 222", ["boiler-proof", "hw-pump-proof"])]
    ac1 = set(_rules(rep, "AC-1")[0])
    assert not ac1 & {"hw-setpoint", "boiler-rise", "boiler-proof", "boiler-alarm",
                      "backup-boiler", "dhw-temp", "hw-pump-proof"}


def test_plant_rules_apply_by_what_the_system_holds():
    pts = [_p(50, "CT-1", "plant", "CT_FAN_CMD", 1, "binary-output"),
           _p(51, "CT-1", "plant", "CT_FAN_STATUS", 1, "binary-input"),
           _p(52, "CT-1", "plant", "CT_SUP_TEMP", 1),
           _p(53, "CT", "plant", "CT_MAKEUP_VALVE", 1)]
    rep = coverage.build(pts)
    st, s = _rules(rep, "CT-1")
    assert st == {"tower-fan-proof": "ready", "tower-approach": "ready"}
    # the loop's fill valve is read by the tower-basin rule, which applies to the towers,
    # so on the loop it is still "used" and must not be listed as unused
    loop = next(x for x in rep["systems"] if x["code"] == "CT")
    assert loop["rules"] == [] and loop["unused"] == []


def test_logged_but_used_by_no_rule_lists_only_the_truly_unused():
    rep = coverage.build(_ahu() + _building_oat()
                         + [_p(91, None, None, "OTHER", 1, name="Outdoor Dewpoint")])
    _, s = _rules(rep, "AC-1")
    assert [u["name"] for u in s["unused"]] == ["Mystery Tag"]    # not HUMIDITY: context
    assert [u["name"] for u in rep["unassigned_unused"]] == ["Outdoor Dewpoint"]


def test_units_sort_naturally():
    rep = coverage.build(_ahu("AC-10") + _ahu("AC-2") + _building_oat())
    assert [s["code"] for s in rep["systems"]] == ["AC-2", "AC-10"]


def test_a_damper_stroke_counter_is_not_a_damper():
    """'Damper Position Counter' (one per AC unit) was classified OA_DAMPER, and the coverage
    review offered it as a damper position feedback."""
    assert roles.classify("Damper Position Counter|dac-3_AV~_004") == "INTERNAL"
    assert roles.classify("Damper Position Counter") == "INTERNAL"
    assert roles.classify("Outdoor Air Damper Position|dac-3") == "OA_DAMPER"
    assert roles.classify("OA/RA Damper Mod|dac-3_AOP~_05") == "OA_DAMPER"


def test_plant_rules_never_attach_to_an_air_handler():
    """HV1 holds condenser bypass-valve points of its own; the loop's bypass rule attached
    itself to it and reported a 'missing' gap on an air handler."""
    hv = _ahu("HV1") + [_p(40, "HV1", "hv", "CW_BYPASS_VALVE", 0)]
    for s in hv:
        s["system_kind"] = "hv"
    st, _ = _rules(coverage.build(hv + _building_oat()), "HV1")
    assert "bypass-when-warm" not in st and "sat-setpoint" in st


_KNOW = {
    "ignore_points": [
        {"match": "Outdoor Air Damper Position*", "systems": "AC-*", "why": "not a real sensor"},
        {"match": "filter status*", "systems": "AC-*", "why": "not accurate"},
    ],
    "rules_not_possible": [
        {"rule": "damper-feedback", "systems": "AC-*", "why": "no real damper feedback"},
        {"rule": "filter", "systems": ["AC-*"], "why": "not accurate"},
    ],
}


def test_site_knowledge_takes_points_out_of_the_review_and_says_why():
    """2026-09-29: the AC filter switches are not accurate, and the Trane 'Outdoor Air
    Damper Position' has no real I/O behind it. Neither may be offered again, and a rule
    that cannot work on a unit is not a gap on it."""
    rep = coverage.build(_ahu() + _building_oat(), _KNOW)
    st, s = _rules(rep, "AC-1")
    assert st["damper-feedback"] == "not-possible" and st["filter"] == "not-possible"
    assert next(r for r in s["rules"] if r["id"] == "filter")["why"] == "not accurate"
    assert {x["name"] for x in s["suggest"]} & {"Filter Status",
                                                 "Outdoor Air Damper Position"} == set()
    assert {x["name"]: x["why"] for x in s["ignored"]} == {
        "Outdoor Air Damper Position": "not a real sensor", "Filter Status": "not accurate"}
    assert s["applicable"] == sum(v not in ("n/a", "not-possible") for v in st.values()) == 7
    assert s["ready"] == 7
    assert (s["points"], s["logged"]) == (16, 14)       # the inventory still counts them


def test_ruling_out_a_rule_does_not_rule_out_its_points():
    """The filter switch is also evidence for the static rule. Ruling out the filter RULE
    leaves it offered there; only the point fact removes it. Each fact does one job."""
    only_rule = {"rules_not_possible": _KNOW["rules_not_possible"]}
    _, s = _rules(coverage.build(_ahu() + _building_oat(), only_rule), "AC-1")
    assert "Filter Status" in {x["name"] for x in s["suggest"]}


def test_a_logged_point_the_site_rules_out_cannot_satisfy_a_rule():
    """And it is listed, even when no rule reads its role: logging a point the site says is
    not real deserves a second look (and it is not ALSO reported as merely unused)."""
    know = {"ignore_points": [{"match": "DAT 1", "why": "reads the wrong duct"},
                              {"match": "Mystery*", "why": "a BAS scratch value"}]}
    st, s = _rules(coverage.build(_ahu() + _building_oat(), know), "AC-1")
    assert st["sat-setpoint"] == "missing" and st["full-cooling"] == "missing"
    assert s["ignored"] == [
        {"id": 1, "name": "DAT 1", "object_type": "analog-input", "role": "DAT",
         "enabled": True, "why": "reads the wrong duct"},
        {"id": 15, "name": "Mystery Tag", "object_type": "analog-input", "role": "OTHER",
         "enabled": True, "why": "a BAS scratch value"}]
    assert s["unused"] == []


def test_a_fact_scoped_to_some_units_leaves_the_others_alone():
    hv = _ahu("HV1")
    for p in hv:
        p["id"] += 100
        p["system_kind"] = "hv"
    rep = coverage.build(_ahu() + hv + _building_oat(), _KNOW)
    assert _rules(rep, "AC-1")[0]["filter"] == "not-possible"
    st, s = _rules(rep, "HV1")
    assert st["filter"] == "needs-logging" and s["ignored"] == []


def test_a_point_its_controller_reports_unreliable_is_not_offered():
    """What the operator sees under 'read visible points': a Trane profile point the unit's program
    does not drive reads reliability 'unreliable-other' (AC-1's Outdoor Air Damper Position
    read 0.0 that way, 2026-09-29). A live read sets it aside; a healthy one is still offered."""
    live = {12: {"quality": "fault", "reliability": "unreliable-other"},
            13: {"quality": "good", "reliability": "no-fault-detected"}}
    _, s = _rules(coverage.build(_ahu() + _building_oat(), None, live), "AC-1")
    offered = {x["name"] for x in s["suggest"]}
    assert "Outdoor Air Damper Position" not in offered and "Filter Status" in offered
    assert [(x["name"], x["why"]) for x in s["ignored"]] == [
        ("Outdoor Air Damper Position", "its controller reports it unreliable-other (read live)")]


@pytest.mark.parametrize("bad", [
    {"ignore_points": [{"match": "Filter*"}]},                                  # no reason
    {"ignore_points": [{"match": "Filter*", "why": "x", "systems": []}]},       # empty scope
    {"rules_not_possible": [{"rule": "filtre", "systems": "AC-*", "why": "x"}]},  # typo
    {"rules_not_possible": [{"rule": "filter", "why": "x"}]},                   # no scope
    ["not", "a", "mapping"],
])
def test_malformed_site_knowledge_is_an_error_not_a_silent_no_op(bad):
    with pytest.raises(ValueError):
        coverage.build(_ahu(), bad)


def _route_db(db_path):
    from hvactrend.storage.sqlite_dal import SQLiteDAL
    dal = SQLiteDAL(db_path)
    dal.apply_schema()
    c = dal._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'DEMO','Demo Building')")
    c.execute("INSERT INTO buildings(id,site_id,code,name) VALUES (1,1,'MAIN','Main')")
    c.execute("INSERT INTO systems(id,building_id,code,name,kind) VALUES (1,1,'AC-1','AC-1','ahu')")
    c.execute("INSERT INTO devices(id,site_id,device_instance,address,created_utc) "
              "VALUES (1,1,100100,'192.0.2.19',0)")
    for pid, ot, name, role, en in (
            (1, "analog-input", "Discharge Air Temp|dac-3_UIP~_06", "DAT", 1),
            (2, "analog-input", "Discharge Air Setpoint|dac-3_AV~_009", "DAT_ACTIVE_SP", 1),
            (3, "binary-input", "Filter Status|dac-3_UIP~_11", "FILTER", 0),
            (4, "multi-state-input", "Heat Cool Mode Status|dac-3", "MODE", 0),
            (5, "multi-state-output", "Heat Cool Mode Request|dac-3", "MODE", 0)):
        c.execute("INSERT INTO points(id,device_id,system_id,source,object_type,object_instance,"
                  "name,role,enabled,created_utc) VALUES (?,1,1,'bacnet',?,?,?,?,?,0)",
                  (pid, ot, pid, name, role, en))
    dal.close()


def _route_cfg(tmp_path, db, knowledge):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "site: {code: DEMO, name: Demo Building}\n"
        "bacnet:\n"
        "  local_address: '<COLLECTOR_IP>/24'\n"
        "  device_instance: '<LOGGER_DEVICE_INSTANCE>'\n"
        "  poll: {interval_s: 120}\n"
        "  safety: {allow_writes: false, allow_cov: false}\n"
        f"storage: {{path: '{str(db).replace(chr(92), '/')}'}}\n" + knowledge,
        encoding="utf-8")
    return cfg


def test_the_coverage_route_applies_site_knowledge_then_checks_the_controllers(
        tmp_path, monkeypatch):
    """GET /api/coverage: the config's facts set the filter switch aside; live=true then reads
    only the points still on offer, and one its controller reports unreliable (the Trane
    'Heat Cool Mode Status' reads 255 / unreliable-other) is set aside too."""
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from hvactrend.api import app as app_mod

    db = tmp_path / "c.db"
    _route_db(db)
    cfg = _route_cfg(tmp_path, db,
                     "site_knowledge:\n  ignore_points:\n"
                     "    - {match: 'Filter Status*', systems: 'AC-*', why: 'not accurate'}\n")
    read_ids = []

    async def fake_read(bacnet, targets):
        read_ids.extend(t["id"] for t in targets)
        return {4: {"id": 4, "quality": "fault", "reliability": "unreliable-other"},
                5: {"id": 5, "quality": "good", "reliability": "no-fault-detected"}}

    monkeypatch.setattr(app_mod, "_read_points_live", fake_read)
    app = app_mod.create_app(str(cfg), enable_bacnet=False)
    with TestClient(app) as client:
        ac1 = client.get("/api/coverage").json()["systems"][0]
        assert {x["name"] for x in ac1["suggest"]} == {"Heat Cool Mode Status|dac-3",
                                                      "Heat Cool Mode Request|dac-3"}
        assert [(x["name"], x["why"]) for x in ac1["ignored"]] == [
            ("Filter Status|dac-3_UIP~_11", "not accurate")]

        r = client.get("/api/coverage", params={"live": "true"}).json()   # no stack
        assert r["live_checked"] is None and "not available" in r["live_error"]

        app.state.bacnet = object()                   # a stack is up; the read is faked
        r = client.get("/api/coverage", params={"live": "true"}).json()
        assert sorted(read_ids) == [4, 5] and r["live_checked"] == 2
        ac1 = r["systems"][0]
        assert [x["name"] for x in ac1["suggest"]] == ["Heat Cool Mode Request|dac-3"]
        assert {x["name"] for x in ac1["ignored"]} == {"Filter Status|dac-3_UIP~_11",
                                                      "Heat Cool Mode Status|dac-3"}

        # bounded, however many points are on offer (audit L-13)
        read_ids.clear()
        monkeypatch.setattr(app_mod, "_COVERAGE_LIVE_MAX", 1)
        r = client.get("/api/coverage", params={"live": "true"}).json()
        assert read_ids == [4] and (r["live_checked"], r["live_skipped"]) == (1, 1)


def test_a_malformed_site_fact_makes_the_route_say_which(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from hvactrend.api.app import create_app

    db = tmp_path / "c.db"
    _route_db(db)
    cfg = _route_cfg(tmp_path, db,
                     "site_knowledge:\n  rules_not_possible:\n"
                     "    - {rule: filtre, systems: 'AC-*', why: 'x'}\n")
    with TestClient(create_app(str(cfg), enable_bacnet=False)) as client:
        r = client.get("/api/coverage")
        assert r.status_code == 500 and "unknown rule 'filtre'" in r.json()["detail"]
