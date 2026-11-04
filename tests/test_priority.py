"""Who is in control of a commandable point (bacnet/priority.py, 2026-10-06).

The operator asked whether the program would know if they had something overridden at the operator
workstation. It did not: the status flag "overridden" never fired in 30 days, because an
operator command is a write into the point's priority array, which nothing read. These pin:
  * the priority array is parsed to (level, value), and "no priority array" is told apart
    from a read that failed;
  * a real ReadPropertyMultiple of priority-array round-trips against a mock controller;
  * the database keeps who is in control, since when, and a log of the changes that matter;
  * the morning check-in lists overrides, an equipment alarm names them, and the dashboard
    and the viewer get them (the viewer remotely too; the full list stays local).
"""

import asyncio
import socket
import time

import pytest

from hvactrend import checkin, notify
from hvactrend.bacnet import priority
from hvactrend.collector.service import Collector, _command_reading
from hvactrend.storage.sqlite_dal import SQLiteDAL

T0 = 1_790_000_000
TGT = {"server": "https://ntfy.sh", "topic": "occ-7kq2-m9xw-4tfh-p3vd-8ncr"}


class _Cfg(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


def _slots(**set_):
    from bacpypes3.basetypes import PriorityArray, PriorityValue
    from bacpypes3.primitivedata import Enumerated, Null, Real
    out = [PriorityValue(null=Null(())) for _ in range(16)]
    for k, v in set_.items():
        i = int(k[1:]) - 1
        out[i] = (PriorityValue(real=Real(v)) if isinstance(v, float)
                  else PriorityValue(enumerated=Enumerated(v)))
    return PriorityArray(out)


# ── parsing ──────────────────────────────────────────────────────────────────────────────
def test_the_first_slot_that_is_not_empty_is_in_control():
    pytest.importorskip("bacpypes3")
    from bacpypes3.basetypes import PriorityArray
    level, v = priority.active(PriorityArray.decode(_slots(s8=58.0, s16=55.0).encode()))
    assert (level, float(v)) == (8, 58.0)              # the operator at 8 beats the program
    assert priority.active(_slots(s16=1))[0] == 16
    assert priority.active(_slots()) == (None, None)   # all empty: the relinquish default
    with pytest.raises(ValueError):
        priority.active([None] * 3)
    with pytest.raises(ValueError):
        priority.active(42)


def test_levels_and_how_an_override_reads():
    assert priority.is_override(8) and priority.is_override(1)
    assert not priority.is_override(9) and not priority.is_override(16)
    assert not priority.is_override(None)
    assert priority.level_name(8) == "manual operator" and priority.level_name(12) == "level 12"
    row = dict(system_code="AC-6", name="Disable Mech Cooling|dac-5_TrBV~_03",
               object_type="binary-output", level=8, value_num=1.0, since_utc=T0,
               since_exact=1)
    assert priority.describe(row).startswith("AC-6 Disable Mech Cooling = ON (manual operator, since ")
    row.update(object_type="analog-output", value_num=58.0, units_text="degrees-fahrenheit",
               since_exact=0, display_name="DAT setpoint")
    assert "DAT setpoint = 58 °F (manual operator, since at least " in priority.describe(row)


def test_no_priority_array_is_told_apart_from_a_failed_read():
    pytest.importorskip("bacpypes3")
    from bacpypes3.apdu import AbortPDU
    from bacpypes3.basetypes import ErrorType
    p = {"point_id": 7}
    none = ErrorType(errorClass="property", errorCode="unknown-property")
    assert _command_reading(p, none) == {"point_id": 7, "has_array": False}
    assert _command_reading(p, ErrorType(errorClass="object", errorCode="unknown-object")) is None
    assert _command_reading(p, AbortPDU()) is None      # failed: the last state stands
    assert _command_reading(p, None) is None
    r = _command_reading(p, _slots(s8=58.0))
    assert r["has_array"] and r["level"] == 8 and r["value_num"] == 58.0
    assert _command_reading(p, _slots())["level"] is None


# ── the database ─────────────────────────────────────────────────────────────────────────
def _db(path):
    dal = SQLiteDAL(path)
    dal.apply_schema()
    c = dal._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'DEMO','Demo Building')")
    c.execute("INSERT INTO buildings(id,site_id,code,name) VALUES (1,1,'MAIN','Main')")
    c.execute("INSERT INTO systems(id,building_id,code,name,kind) VALUES (1,1,'AC-1','AC-1','ahu')")
    c.execute("INSERT INTO devices(id,site_id,device_instance,address,created_utc) "
              "VALUES (1,1,100100,'192.0.2.19',0)")
    for pid, ot, oi, nm, units in (
            (1, "analog-output", 9, "Discharge Air Setpoint|dac-3_TrAV~_09", "degrees-fahrenheit"),
            (2, "binary-output", 3, "Disable Mech Cooling|dac-3_TrBV~_03", None),
            (3, "analog-value", 4, "Econ Setpoint", "degrees-fahrenheit"),
            (4, "analog-input", 6, "Discharge Air Temp|dac-3_UIP~_06", "degrees-fahrenheit")):
        c.execute("INSERT INTO points(id,device_id,system_id,source,object_type,object_instance,"
                  "name,units_text,enabled,created_utc) VALUES (?,1,1,'bacnet',?,?,?,?,1,0)",
                  (pid, ot, oi, nm, units))
    return dal


def _reading(pid, level, num, has_array=True):
    return {"point_id": pid, "has_array": has_array, "level": level, "value_num": num,
            "value_text": None}


def _log(dal):
    return dal._conn.execute("SELECT point_id, ts_utc, level, value_num FROM point_command_log "
                             "ORDER BY ts_utc, point_id").fetchall()


def test_the_database_keeps_who_is_in_control_and_what_changed(tmp_path):
    dal = _db(tmp_path / "p.db")
    kinds = [t["object_type"] for t in dal.command_targets(priority.COMMANDABLE_TYPES)]
    assert sorted(kinds) == ["analog-output", "analog-value", "binary-output"]   # not the input
    out = dal.record_commands(T0, [_reading(1, 16, 55.0), _reading(2, 8, 1.0),
                                   {"point_id": 3, "has_array": False}])
    assert out == {"read": 2, "changed": 0, "no_array": 1}
    assert [t["point_id"] for t in dal.command_targets(priority.COMMANDABLE_TYPES)] == [1, 2]
    (o,) = dal.overrides()                       # only the operator's command is an override
    assert (o["point_id"], o["level"], o["since_utc"], o["since_exact"]) == (2, 8, T0, 0)
    assert len(_log(dal)) == 2                   # the first read is the baseline

    dal.record_commands(T0 + 600, [_reading(1, 16, 55.0), _reading(2, 8, 1.0)])
    assert len(_log(dal)) == 2                   # nothing changed: nothing logged
    assert dal.overrides()[0]["checked_utc"] == T0 + 600

    dal.record_commands(T0 + 1200, [_reading(1, 16, 57.5)])      # the program moved it
    assert len(_log(dal)) == 2                   # not news: that is the trend already
    dal.record_commands(T0 + 1800, [_reading(1, 8, 60.0)])       # an operator took it over
    dal.record_commands(T0 + 2400, [_reading(1, 8, 62.0)])       # and changed their mind
    o = {r["point_id"]: r for r in dal.overrides()}[1]
    assert (o["level"], o["value_num"], o["since_utc"], o["since_exact"]) == (8, 62.0, T0 + 1800, 1)
    dal.record_commands(T0 + 3000, [_reading(2, 16, 0.0)])       # released to the program
    assert [r["point_id"] for r in dal.overrides()] == [1]
    assert [(r[0], r[1], r[2]) for r in _log(dal)][2:] == [
        (1, T0 + 1800, 8), (1, T0 + 2400, 8), (2, T0 + 3000, 16)]
    dal._conn.execute("UPDATE points SET enabled = 0 WHERE id = 1")
    assert dal.overrides() == []                 # no longer read, so no longer claimed


def _state(pid, unit, name, level, role=None, kind="ahu", value=1.0):
    return dict(point_id=pid, system_code=unit, system_kind=kind, name=name, role=role,
                role_canonical=1 if role else 0, level=level, value_num=value,
                since_utc=T0 + pid)


def test_a_level_every_unit_shares_is_configuration_not_an_override():
    """The first read at the first building found 86 points at levels 1-8, and 78 were the Trane units' own
    setpoints, held at 8 on every unit alike. Only a point more important than the same
    point's usual level elsewhere is an override; a point with no peers counts from 8."""
    rows = [_state(i, f"AC-{i}", f"Econ Enable Enthalpy|dac-{i}", 8, "ECON_ENTH_SP")
            for i in range(1, 4)]
    rows += [_state(4, "AC-4", "Economizer Enable Enthalpy", 8, "ECON_ENTH_SP"),  # named apart
             _state(5, "AC-5", "Static Pressure Setpoint|dac-7", 8, "DUCT_PRESS_SP")]
    rows += [_state(10 + i, f"AC-{i}", f"Static Pressure Setpoint|dac-{i}", 9, "DUCT_PRESS_SP")
             for i in range(1, 4)]
    rows += [_state(20, "HB3", "HB3 Enable (command)", 8, kind="plant"),
             _state(21, "CT", "Supply SP", 16, kind="plant")]
    got = priority.overrides(rows)
    assert [o["point_id"] for o in got] == [5, 20]
    assert [o["usual_level"] for o in got] == [9, None]


def test_a_value_cycling_at_one_level_is_a_schedule_not_a_person(tmp_path):
    """AC-6's and AC-12's occupancy requests sit at 8 (the others at 10-12) and still go
    occupied by day and unoccupied by night: a schedule writes them there."""
    rows = [_state(1, "AC-6", "Occupancy Request|dac-5", 8)]
    assert priority.overrides(rows, {1: [(1.0, None), (2.0, None)]})            # changed once
    assert not priority.overrides(rows, {1: [(1.0, None), (2.0, None), (1.0, None)]})
    assert priority.overrides(rows, {1: [(58.0, None), (60.0, None), (62.0, None)]})
    dal = _db(tmp_path / "s.db")
    for k, v in enumerate((1.0, 0.0, 1.0)):
        dal.record_commands(T0 + k * 600, [_reading(2, 8, v)])
    assert dal.overrides() == []


# ── after-hours use: the thermostat's override button is not an override ─────────────────
def _occ_db(path):
    """AC-12 with its occupancy request (what the button writes) and occupancy status."""
    dal = SQLiteDAL(path)
    dal.apply_schema()
    c = dal._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'DEMO','Demo Building')")
    c.execute("INSERT INTO buildings(id,site_id,code,name) VALUES (1,1,'MAIN','Main')")
    c.execute("INSERT INTO systems(id,building_id,code,name,kind) VALUES (1,1,'AC-12','AC-12','ahu')")
    c.execute("INSERT INTO devices(id,site_id,device_instance,address,created_utc) "
              "VALUES (1,1,100200,'192.0.2.18',0)")
    for pid, ot, nm in ((1, "multi-state-output", "Occupancy Request|dac-2"),
                        (2, "multi-state-input", "Occupancy Status|dac-2")):
        c.execute("INSERT INTO points(id,device_id,system_id,source,object_type,object_instance,"
                  "name,role,enabled,created_utc) VALUES (?,1,1,'bacnet',?,?,?,'OCCUPANCY',1,0)",
                  (pid, ot, pid, nm))
    return dal


def _status(dal, t_from, t_to, v):
    from hvactrend.models import Quality, Sample
    dal.write_samples([Sample(point_id=2, ts_utc=t, value_num=v, quality=Quality.GOOD)
                       for t in range(t_from, t_to, 120)])


def test_an_override_button_is_after_hours_use_not_an_override(tmp_path):
    """2026-10-15: list the thermostat's override button separately, as after-hours
    use. Held occupied longer than a button press lasts, it is an override again."""
    dal = _occ_db(tmp_path / "o.db")
    dal.record_commands(T0, [_reading(1, 12, 2.0)])                   # the schedule: off
    dal.record_commands(T0 + 600, [_reading(1, 8, 1.0)])              # a press at night
    assert dal.overrides(now_utc=T0 + 3600) == []                     # not an override
    (o,) = dal.overrides(now_utc=T0 + 600 + 9 * 3600)                 # held 9 h: it is now
    assert o["point_id"] == 1
    row = {"role": "OCCUPANCY", "object_type": "multi-state-output", "value_num": 2.0,
           "since_exact": 1, "since_utc": T0}
    assert not priority.is_occupancy_button(row, T0 + 60)             # forced OFF: an override


def test_after_hours_sessions_are_the_ones_that_ran_the_unit_off_schedule(tmp_path):
    dal = _occ_db(tmp_path / "s.db")
    H = 3600
    _status(dal, T0, T0 + 2 * H, 2.0)                                 # unoccupied
    dal.record_commands(T0, [_reading(1, 12, 2.0)])
    dal.record_commands(T0 + 2 * H, [_reading(1, 8, 1.0)])           # pressed: unit comes on
    dal.record_commands(T0 + 4 * H, [_reading(1, 12, 2.0)])          # timed out
    _status(dal, T0 + 10 * H, T0 + 12 * H, 1.0)                       # scheduled hours
    dal.record_commands(T0 + 10 * H, [_reading(1, 12, 1.0)])
    dal.record_commands(T0 + 11 * H, [_reading(1, 8, 1.0)])          # pressed while on: no use
    dal.record_commands(T0 + 13 * H, [_reading(1, 12, 1.0)])
    dal.record_commands(T0 + 20 * H, [_reading(1, 8, 1.0)])          # pressed near the end of
    dal.record_commands(T0 + 22 * H, [_reading(1, 12, 2.0)])          # the day: ran past it
    got = dal.after_hours_sessions(T0, T0 + 30 * H)
    assert [(s["start"] - T0, s["end"] - T0) for s in got] == [(2 * H, 4 * H), (20 * H, 22 * H)]
    assert got[0]["started_off"] and got[1]["ran_past_schedule"]
    assert priority.session_text(got[0]).endswith("(2.0 h)")
    assert priority.session_text(got[1]).endswith("(ran past its schedule)")
    assert dal.after_hours_sessions(T0 + 5 * H, T0 + 19 * H) == []    # none in that window
    dal.set_meta("priority.last_read_utc", T0 + 30 * H)
    facts = dict(checkin.build(dal, _Cfg(), T0 + 30 * H)["facts"])    # the morning check-in
    assert "After-hours use" in facts


def test_the_after_hours_list_is_served_locally(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from hvactrend.api.app import create_app
    db = tmp_path / "a.db"
    dal = _occ_db(db)
    now = int(time.time())
    _status(dal, now - 7200, now - 3600, 2.0)
    dal.record_commands(now - 7200, [_reading(1, 12, 2.0)])
    dal.record_commands(now - 3600, [_reading(1, 8, 1.0)])
    dal.set_meta("priority.last_read_utc", now - 60)
    dal.close()
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("site: {code: DEMO, name: Demo Building}\n"
                   "bacnet:\n  local_address: '<COLLECTOR_IP>/24'\n"
                   "  device_instance: '<LOGGER_DEVICE_INSTANCE>'\n"
                   "  poll: {interval_s: 120}\n"
                   "  safety: {allow_writes: false, allow_cov: false}\n"
                   f"storage: {{path: '{db.as_posix()}'}}\n", encoding="utf-8")
    app = create_app(str(cfg), enable_bacnet=False)
    with TestClient(app) as c:
        (s,) = c.get("/api/after-hours").json()["sessions"]
        assert s["system_code"] == "AC-12" and s["end"] is None and "still on" in s["text"]
        assert c.get("/api/systems/1/overview").json()["after_hours"][0]["started_off"]
        assert c.get("/api/overrides").json()["overrides"] == []
    with TestClient(app, client=("192.0.2.77", 51234)) as rc:
        assert rc.get("/api/after-hours").status_code == 404


# ── the read itself, against a mock controller over loopback ─────────────────────────────
MOCK_PORT, READER_PORT = 47809, 47808


def _loopback_ok(*ports):
    for port in ports:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", port))
            s.close()
        except OSError:
            return False
    return True


@pytest.mark.skipif(not _loopback_ok(MOCK_PORT, READER_PORT), reason="loopback UDP unavailable")
def test_the_collector_reads_priority_arrays_from_a_controller(tmp_path):
    pytest.importorskip("bacpypes3")

    async def run():
        from bacpypes3.app import Application
        from bacpypes3.local.analog import AnalogInputObject, AnalogOutputObject, AnalogValueObject
        from bacpypes3.local.binary import BinaryOutputObject

        from hvactrend.bacnet.application import ReadOnlyBACnetApplication
        from hvactrend.discovery_service import run_discovery
        from .mock_bacnet import _args

        mock = Application.from_args(_args(999002, "MockCmd", f"127.0.0.1:{MOCK_PORT}"))
        mock.add_object(AnalogOutputObject(
            objectIdentifier=("analog-output", 1), objectName="AHU1-DAT-SP-CMD",
            presentValue=58.0, units="degreesFahrenheit", relinquishDefault=55.0,
            priorityArray=list(_slots(s8=58.0, s16=55.0))))
        mock.add_object(BinaryOutputObject(
            objectIdentifier=("binary-output", 1), objectName="AHU1-MECH-DISABLE",
            presentValue="inactive", relinquishDefault="inactive",
            priorityArray=list(_slots(s16=0))))
        mock.add_object(AnalogValueObject(
            objectIdentifier=("analog-value", 1), objectName="AHU1-ECON-SP",
            presentValue=60.0, units="degreesFahrenheit"))
        mock.add_object(AnalogInputObject(
            objectIdentifier=("analog-input", 1), objectName="AHU1-DAT",
            presentValue=55.3, units="degreesFahrenheit"))
        cfg = {"bacnet": {"local_address": f"127.0.0.1:{READER_PORT}",
                          "device_instance": 1000, "vendor_identifier": 999,
                          "poll": {"interval_s": 120, "stagger_s": 0},
                          "safety": {"allow_writes": False, "allow_cov": False}}}
        reader = await ReadOnlyBACnetApplication.create(cfg)
        dal = SQLiteDAL(tmp_path / "c.db")
        dal.apply_schema()
        try:
            await asyncio.sleep(0.3)
            await run_discovery(reader, dal, site_id=dal.ensure_site("DEMO", "Demo Building"),
                                address=f"127.0.0.1:{MOCK_PORT}")
            dal._conn.execute("UPDATE points SET enabled = 1")
            col = Collector(reader, dal, cfg)
            await col._maybe_read_priorities()
            rows = {r["name"]: dict(r) for r in dal._conn.execute(
                "SELECT p.name, c.* FROM point_command c JOIN points p ON p.id = c.point_id")}
            assert (rows["AHU1-DAT-SP-CMD"]["level"], rows["AHU1-DAT-SP-CMD"]["value_num"]) == (8, 58.0)
            assert (rows["AHU1-MECH-DISABLE"]["level"], rows["AHU1-MECH-DISABLE"]["value_num"]) == (16, 0.0)
            assert rows["AHU1-ECON-SP"]["has_array"] == 0     # not commandable: not asked again
            assert "AHU1-DAT" not in rows                     # an input has no priority array
            assert [o["name"] for o in dal.overrides()] == ["AHU1-DAT-SP-CMD"]
            assert dal.get_meta("priority.last_read_utc") is not None
            assert dal._conn.execute("SELECT count(*) FROM point_command_log").fetchone()[0] == 2
            col._dal = None                                   # any read now would raise:
            await col._maybe_read_priorities()                # not due again for 10 minutes
        finally:
            await reader.close()
            mock.close()

    asyncio.run(run())


# ── where the operator sees them ────────────────────────────────────────────────────────────────
def _overridden(tmp_path):
    dal = _db(tmp_path / "w.db")
    dal.record_commands(T0, [_reading(1, 16, 55.0), _reading(2, 8, 1.0),
                             {"point_id": 3, "has_array": False}])
    dal.set_meta("priority.last_read_utc", T0)
    return dal


def test_the_checkin_lists_the_overrides(tmp_path):
    dal = _db(tmp_path / "n.db")
    facts = dict(checkin.build(dal, _Cfg(), T0)["facts"])
    assert facts["Overrides"] == "not read yet"
    dal.set_meta("priority.last_read_utc", T0)
    assert dict(checkin.build(dal, _Cfg(), T0 + 60)["facts"])["Overrides"] == "none"
    dal = _overridden(tmp_path)
    facts = dict(checkin.build(dal, _Cfg(), T0 + 60)["facts"])
    assert facts["Overrides (1)"].startswith("AC-1 Disable Mech Cooling = ON (manual operator")
    facts = dict(checkin.build(dal, _Cfg(), T0 + 2 * 3600)["facts"])
    assert "(last read " in facts["Overrides (1)"]          # an old read says how old


class _Wire:
    def __init__(self):
        self.sent = []

    def __call__(self, tgt, msg, *, timeout=10.0):
        self.sent.append(msg)
        return True, "HTTP 200"


def test_an_equipment_alarm_names_the_override_on_its_unit(tmp_path, monkeypatch):
    """2026-10-06: alert anyway, and say what is overridden."""
    dal = _overridden(tmp_path)
    dal.insert_incident(dict(
        rule="sat-setpoint", system_code="AC-1", point_key="", fingerprint="sat-setpoint:AC-1:",
        tier="critical", title="Discharge air not meeting its setpoint",
        detail="AC-1: discharge air +22.0 °F against its setpoint at worst",
        started_utc=T0, opened_utc=T0 + 600, updated_utc=T0 + 600))
    wire = _Wire()
    monkeypatch.setattr(notify, "post", wire)
    notify.run_incidents(dal, _Cfg(rules={"push_critical": True}), T0 + 600, tgt=TGT)
    (m,) = wire.sent
    assert "Overridden now: AC-1 Disable Mech Cooling = ON (manual operator" in m.text()


def test_site_knowledge_can_say_an_override_is_held_on_purpose():
    """2026-10-06: HB3's DHW call is held OFF at operator level on purpose (its DHW
    side is no longer connected). Held at anything else, it is news again."""
    from hvactrend import coverage
    facts = coverage.site_facts({"expected_overrides": [
        {"match": "HB3DHW*", "systems": "HB3", "value": False, "why": "DHW not connected"}]})
    assert facts["expected_overrides"][0]["value"] == "OFF"     # YAML reads a bare OFF as false
    with pytest.raises(ValueError):
        coverage.site_facts({"expected_overrides": [{"match": "HB3DHWCALL"}]})   # no reason

    def row(unit, v):
        return dict(name="HB3DHWCALL", system_code=unit, object_type="binary-output", value_num=v)
    held, other_unit, other_value = priority.mark_expected(
        [row("HB3", 0.0), row("BLR-1", 0.0), row("HB3", 1.0)], facts)
    assert held["expected"] == "DHW not connected"
    assert other_unit["expected"] is None and other_value["expected"] is None


def test_an_override_held_on_purpose_is_shown_but_not_counted_or_named(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from hvactrend.api.app import create_app
    dal = _overridden(tmp_path)                  # AC-1 Disable Mech Cooling = ON at 8
    know = {"expected_overrides": [{"match": "Disable Mech Cooling*", "systems": "AC-1",
                                    "value": "ON", "why": "on purpose"}]}
    facts = dict(checkin.build(dal, _Cfg(site_knowledge=know), T0 + 60)["facts"])
    assert facts["Overrides"] == "none"
    assert facts["Held on purpose (1)"] == "AC-1 Disable Mech Cooling = ON"
    dal.insert_incident(dict(
        rule="sat-setpoint", system_code="AC-1", point_key="", fingerprint="sat-setpoint:AC-1:",
        tier="critical", title="Discharge air not meeting its setpoint", detail="AC-1: +22 °F",
        started_utc=T0, opened_utc=T0 + 600, updated_utc=T0 + 600))
    wire = _Wire()
    monkeypatch.setattr(notify, "post", wire)
    notify.run_incidents(dal, _Cfg(rules={"push_critical": True}, site_knowledge=know),
                         T0 + 600, tgt=TGT)
    (m,) = wire.sent
    assert "Overridden now" not in m.text()
    dal.close()
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("site: {code: DEMO, name: Demo Building}\n"
                   "bacnet:\n  local_address: '<COLLECTOR_IP>/24'\n"
                   "  device_instance: '<LOGGER_DEVICE_INSTANCE>'\n"
                   "  poll: {interval_s: 120}\n"
                   "  safety: {allow_writes: false, allow_cov: false}\n"
                   f"storage: {{path: '{(tmp_path / 'w.db').as_posix()}'}}\n"
                   "site_knowledge:\n  expected_overrides:\n"
                   "    - {match: 'Disable Mech Cooling*', systems: AC-1, value: 'ON', why: on purpose}\n",
                   encoding="utf-8")
    with TestClient(create_app(str(cfg), enable_bacnet=False)) as c:
        (o,) = c.get("/api/overrides").json()["overrides"]
        assert o["expected"] == "on purpose"
        assert c.get("/api/overview").json()["units"][0]["overrides"] == 0


def test_the_dashboard_and_viewer_get_the_overrides_and_remote_only_the_marks(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from hvactrend.api.app import create_app
    db = tmp_path / "w.db"
    _overridden(tmp_path).close()
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("site: {code: DEMO, name: Demo Building}\n"
                   "bacnet:\n  local_address: '<COLLECTOR_IP>/24'\n"
                   "  device_instance: '<LOGGER_DEVICE_INSTANCE>'\n"
                   "  poll: {interval_s: 120}\n"
                   "  safety: {allow_writes: false, allow_cov: false}\n"
                   f"storage: {{path: '{db.as_posix()}'}}\n", encoding="utf-8")
    app = create_app(str(cfg), enable_bacnet=False)
    with TestClient(app) as c:
        d = c.get("/api/overrides").json()
        assert d["last_read_utc"] == T0 and [o["point_id"] for o in d["overrides"]] == [2]
        assert d["overrides"][0]["level_name"] == "manual operator"
        assert d["overrides"][0]["value"] == "ON"
        marks = {p["id"]: p.get("override") for p in c.get("/api/logged").json()["points"]}
        assert marks[2]["level"] == 8 and marks[1] is None and marks[4] is None
        u = c.get("/api/systems/1/overview").json()
        assert [o["point_id"] for o in u["overrides"]] == [2]
        assert {p["id"]: "override" in p for p in u["points"]} == {1: False, 2: True, 3: False,
                                                                  4: False}
        assert c.get("/api/overview").json()["units"][0]["overrides"] == 1
    with TestClient(app, client=("192.0.2.77", 51234)) as rc:
        # the viewer polls the list to keep its marks current (2026-10-27); the dashboard's
        # unit overview stays local
        assert [o["point_id"] for o in rc.get("/api/overrides").json()["overrides"]] == [2]
        assert rc.get("/api/systems/1/overview").status_code == 404
        marks = {p["id"]: p.get("override") for p in rc.get("/api/logged").json()["points"]}
        assert marks[2]["level"] == 8                            # the viewer's marks do not
