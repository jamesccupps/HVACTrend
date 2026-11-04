"""Rule-based alarms, live (rules/live.py, notify.run_incidents, the check-in, the watchdog).

What these pin:
  * a run records each episode once: an ongoing condition continues its incident on every
    5-minute run instead of opening another, and the overlapping windows never duplicate one;
  * a recovery closes it, and three units at once share one group;
  * nothing is pushed until a tier is switched on (rules.push_critical / push_action); then
    one message per group, again when one gets worse, and one all-clear. ACTION incidents
    already open at the switch-on are not replayed (the operator chose both tiers on 2026-10-02);
  * an alarm is called by its own point ("Multistack Alarm"), not "controller alarm", and
    one reminder a day lists every pushed tier still on after a day (2026-10-26);
  * the check-in asks for a look at every important incident still on, by name, and says so
    when the rules are failing or their pushes are;
  * the watchdog survives a rules crash with its liveness result untouched.
"""

import importlib.util
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from hvactrend import checkin, notify
from hvactrend.models import Quality, Sample
from hvactrend.rules import catalog, live
from hvactrend.storage.sqlite_dal import SQLiteDAL

REPO = Path(__file__).resolve().parents[1]
T0 = 1_700_000_000
TGT = {"server": "https://ntfy.sh", "topic": "occ-7kq2-m9xw-4tfh-p3vd-8ncr"}


class _Cfg(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


def _world(tmp_path, units=("BLR-2",)):
    """Boilers that are enabled and NOT heating (supply == return) from T0 on."""
    dal = SQLiteDAL(tmp_path / "l.db")
    dal.apply_schema()
    c = dal._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'DEMO','Demo Building')")
    c.execute("INSERT INTO buildings(id,site_id,code,name) VALUES (1,1,'MAIN','Main')")
    c.execute("INSERT INTO devices(id,site_id,device_instance,address,created_utc) "
              "VALUES (1,1,101000,'192.0.2.176',0)")
    pid = 0
    for sid, code in enumerate(units, start=1):
        c.execute("INSERT INTO systems(id,building_id,code,name,kind,occupancy_mode) "
                  "VALUES (?,1,?,?,'plant','always')", (sid, code, code))
        for role, units_text in (("BLR_SUP_TEMP", "degrees-fahrenheit"),
                                 ("BLR_RET_TEMP", "degrees-fahrenheit"), ("BLR_CMD", None)):
            pid += 1
            c.execute("INSERT INTO points(id,device_id,system_id,source,object_type,"
                      "object_instance,name,role,role_canonical,units_text,enabled,created_utc) "
                      "VALUES (?,1,?,'bacnet','analog-value',?,?,?,1,?,1,0)",
                      (pid, sid, pid, f"{code} {role}", role, units_text))
    return dal


def _feed(dal, t_from, t_to, *, rise=0.0):
    rows = []
    for (pid, role) in dal._conn.execute("SELECT id, role FROM points"):
        for t in range(t_from, t_to, 120):
            v = {"BLR_SUP_TEMP": 140.0 + rise, "BLR_RET_TEMP": 140.0, "BLR_CMD": 1.0}[role]
            rows.append(Sample(point_id=pid, ts_utc=t, value_num=v, quality=Quality.GOOD))
    dal.write_samples(rows)


def _rows(dal):
    return dal._conn.execute("SELECT rule, system_code, status, tier, group_key, started_utc "
                             "FROM incidents ORDER BY id").fetchall()


def test_an_ongoing_condition_is_one_incident_across_runs(tmp_path):
    dal = _world(tmp_path)
    _feed(dal, T0, T0 + 3 * 3600)
    now = T0 + 3 * 3600
    out = live.run(dal, _Cfg(), now)
    assert len(out["new"]) == 1 and dal.get_meta("rules.last_run_utc") == str(now)
    first = _rows(dal)
    for k in range(1, 4):                     # three more 5-minute runs, still not heating
        _feed(dal, now + (k - 1) * 300, now + k * 300)
        out = live.run(dal, _Cfg(), now + k * 300)
        assert out["new"] == [] and out["updated"] == 1
    assert _rows(dal) == first                # the same one incident, still open
    (r,) = dal.open_incidents()
    assert r["rule"] == "boiler-rise" and "separate spells" not in r["detail"]


def test_a_recovery_closes_the_incident(tmp_path):
    dal = _world(tmp_path)
    _feed(dal, T0, T0 + 3 * 3600)
    live.run(dal, _Cfg(), T0 + 3 * 3600)
    _feed(dal, T0 + 3 * 3600, T0 + 4 * 3600, rise=15.0)       # it fires: 15 °F of rise
    out = live.run(dal, _Cfg(), T0 + 4 * 3600)
    assert out["resolved"] == 1 and dal.open_incidents() == []
    (row,) = _rows(dal)
    assert row[2] == "resolved"
    out = live.run(dal, _Cfg(), T0 + 4 * 3600 + 300)           # the next window re-reads it
    assert out["new"] == [] and len(_rows(dal)) == 1           # recorded once


def test_a_long_incident_is_recorded_once_even_after_it_closes(tmp_path):
    """Every run re-reads the last 4 h. After an incident longer than that closed, each run
    found its tail again from wherever its window began, and the dedupe (exact start) let
    every one in: 263 of the first 313 incidents were such copies (found 2026-10-14)."""
    dal = _world(tmp_path)
    _feed(dal, T0, T0 + 6 * 3600)                                  # 6 h not heating
    for k in range(7):
        live.run(dal, _Cfg(), T0 + 3 * 3600 + k * 1800)
    _feed(dal, T0 + 6 * 3600, T0 + 12 * 3600, rise=15.0)           # then it fires
    for k in range(13):
        live.run(dal, _Cfg(), T0 + 6 * 3600 + 1800 + k * 1800)     # six more hours of runs
    (row,) = _rows(dal)
    assert row[2] == "resolved" and row[5] == T0 + 600             # after the 10-min settle


def test_a_stored_nan_peak_does_not_crash_the_run(tmp_path):
    """2026-10-13 22:12: boiler 3's return sensor, flagged unreliable, peaks at NaN, which the
    database stores as NULL. Continued from the database with no new breach, the incident's
    message formatted None, and that crashed every rule in the run."""
    dal = _world(tmp_path)
    ret = dal._conn.execute("SELECT id FROM points WHERE role = 'BLR_RET_TEMP'").fetchone()[0]
    dal.insert_incident(dict(
        rule="sensor-sanity", system_code="BLR-2", point_key=str(ret),
        fingerprint=f"sensor-sanity:BLR-2:{ret}", tier="action",
        title="Sensor reading impossible values", started_utc=T0 - 3600,
        opened_utc=T0 - 3000, last_breach_utc=T0 - 60, peak=float("nan"), updated_utc=T0))
    assert dal.open_incidents()[0]["peak"] is None             # NaN went in as NULL
    _feed(dal, T0, T0 + 3600, rise=15.0)                        # sane again
    out = live.run(dal, _Cfg(), T0 + 3600)
    assert "errors" not in out
    (r,) = [r for r in dal._conn.execute("SELECT status, detail FROM incidents WHERE rule = "
                                         "'sensor-sanity'")]
    assert r[0] == "resolved" and "flagged unreliable" in r[1]


def test_one_rule_failing_does_not_blind_the_others(tmp_path, monkeypatch):
    from hvactrend.rules import evaluate as ev
    dal = _world(tmp_path)
    _feed(dal, T0, T0 + 3 * 3600)                               # not heating

    def boom(*a, **k):
        raise TypeError("unsupported format string passed to NoneType.__format__")
    monkeypatch.setattr(ev, "_point_rule", boom)                # every per-point rule fails
    out = live.run(dal, _Cfg(), T0 + 3 * 3600)
    assert len(out["new"]) == 1                                 # the boiler-rise still opened
    assert out["errors"][0].startswith("sensor-sanity on BLR-2 point")
    err = dal.get_meta("rules.last_error")
    assert err.startswith(f"{T0 + 3 * 3600} sensor-sanity on BLR-2") and "more)" in err
    now = T0 + 3 * 3600 + 120
    s = checkin.build(dal, _Cfg(), now)
    assert any(a.startswith("The equipment alarm rules are failing: sensor-sanity")
               for a in s["attention"])


def test_an_incident_its_rule_can_no_longer_judge_closes_after_the_idle_time(tmp_path):
    """Its point switched off: nothing continues it, so it must not stay open forever."""
    dal = _world(tmp_path)
    _feed(dal, T0, T0 + 3 * 3600)
    live.run(dal, _Cfg(), T0 + 3 * 3600)
    dal._conn.execute("UPDATE points SET enabled = 0")
    live.run(dal, _Cfg(), T0 + 3 * 3600 + 86400)
    assert len(dal.open_incidents()) == 1                     # a day: still open
    out = live.run(dal, _Cfg(), T0 + 3 * 3600 + 4 * 86400)
    assert out["resolved"] == 1 and dal.open_incidents() == []
    assert dal._conn.execute("SELECT resolve_reason FROM incidents").fetchone()[0] == "not judged"


def test_three_units_at_once_share_a_group(tmp_path):
    dal = _world(tmp_path, units=("BLR-1", "BLR-2", "BLR-3"))
    _feed(dal, T0, T0 + 3 * 3600)
    live.run(dal, _Cfg(), T0 + 3 * 3600)
    keys = {r[4] for r in _rows(dal)}
    assert len(_rows(dal)) == 3 and len(keys) == 1 and next(iter(keys)).startswith("building:")


def test_the_rules_can_be_switched_off(tmp_path):
    dal = _world(tmp_path)
    _feed(dal, T0, T0 + 3 * 3600)
    out = live.run(dal, _Cfg(rules={"enabled": False}), T0 + 3 * 3600)
    assert out["skipped"] and _rows(dal) == []


# ── delivery ─────────────────────────────────────────────────────────────────────────────
class _Wire:
    def __init__(self):
        self.sent = []

    def __call__(self, tgt, msg, *, timeout=10.0):
        self.sent.append(msg)
        return True, "HTTP 200"


def _critical_world(tmp_path, monkeypatch):
    dal = _world(tmp_path, units=("BLR-1", "BLR-2", "BLR-3"))
    _feed(dal, T0, T0 + 3 * 3600)
    live.run(dal, _Cfg(), T0 + 3 * 3600)
    dal._conn.execute("UPDATE incidents SET tier = 'critical'")   # as if a critical tier fired
    wire = _Wire()
    monkeypatch.setattr(notify, "post", wire)
    return dal, wire


def test_nothing_is_pushed_until_the_operator_switches_it_on(tmp_path, monkeypatch):
    dal, wire = _critical_world(tmp_path, monkeypatch)
    out = notify.run_incidents(dal, _Cfg(), T0 + 3 * 3600, tgt=TGT)
    assert out["enabled"] is False and wire.sent == []


def test_one_cause_is_one_push_and_one_all_clear(tmp_path, monkeypatch):
    dal, wire = _critical_world(tmp_path, monkeypatch)
    on = _Cfg(rules={"push_critical": True})
    out = notify.run_incidents(dal, on, T0 + 3 * 3600, tgt=TGT)
    assert out["pushed"] == 3 and len(wire.sent) == 1          # three units, one message
    assert wire.sent[0].priority == 5 and "3 units at once" in wire.sent[0].title
    notify.run_incidents(dal, on, T0 + 3 * 3600 + 300, tgt=TGT)
    assert len(wire.sent) == 1                                 # not pushed twice
    dal._conn.execute("UPDATE incidents SET status = 'resolved', resolved_utc = ?",
                      (T0 + 4 * 3600,))
    notify.run_incidents(dal, on, T0 + 4 * 3600, tgt=TGT)
    notify.run_incidents(dal, on, T0 + 4 * 3600 + 300, tgt=TGT)
    assert len(wire.sent) == 2 and wire.sent[1].priority == 3  # one all-clear, once


CRIT = _Cfg(rules={"push_critical": True})
BOTH = _Cfg(rules={"push_critical": True, "push_action": True})


def _inc(dal, unit, tier, opened):
    """An incident as rules/live.py records it."""
    return dal.insert_incident(dict(
        rule="sat-setpoint", system_code=unit, point_key="", fingerprint=f"sat-setpoint:{unit}:",
        tier=tier, title="Discharge air not meeting its setpoint",
        detail=f"{unit}: discharge air +7.5 °F against its setpoint at worst",
        started_utc=opened - 1800, opened_utc=opened, updated_utc=opened))


def _wired(tmp_path, monkeypatch):
    dal = _world(tmp_path)
    wire = _Wire()
    monkeypatch.setattr(notify, "post", wire)
    return dal, wire


def test_action_incidents_push_as_normal_notifications_once_switched_on(tmp_path, monkeypatch):
    """2026-10-02: critical AND action to the phone. ACTION is a normal notification,
    and what was already open at the switch-on stays in the morning check-in."""
    dal, wire = _wired(tmp_path, monkeypatch)
    notify.run_incidents(dal, CRIT, T0, tgt=TGT)    # critical only, so far
    _inc(dal, "AC-5", "action", T0 + 300)
    notify.run_incidents(dal, CRIT, T0 + 300, tgt=TGT)
    assert wire.sent == []                          # push_critical alone never sends ACTION
    notify.run_incidents(dal, BOTH, T0 + 600, tgt=TGT)
    assert wire.sent == []                          # open before the switch-on: not replayed
    _inc(dal, "AC-7", "action", T0 + 900)
    out = notify.run_incidents(dal, BOTH, T0 + 900, tgt=TGT)
    (m,) = wire.sent
    assert out["pushed"] == 1 and m.priority == 3 and "AC-7" in m.title
    assert m.title.startswith(notify._INCIDENT_ICON["action"])
    notify.run_incidents(dal, BOTH, T0 + 1200, tgt=TGT)
    assert len(wire.sent) == 1                      # once
    dal._conn.execute("UPDATE incidents SET status = 'resolved', resolved_utc = ? "
                      "WHERE system_code = 'AC-7'", (T0 + 3000,))
    notify.run_incidents(dal, BOTH, T0 + 3000, tgt=TGT)
    assert len(wire.sent) == 2 and wire.sent[1].priority == 2      # a quiet all-clear


def test_an_action_incident_that_turns_critical_is_pushed_again_as_urgent(tmp_path, monkeypatch):
    dal, wire = _wired(tmp_path, monkeypatch)
    notify.run_incidents(dal, BOTH, T0, tgt=TGT)                     # switched on
    iid = _inc(dal, "AC-6", "action", T0 + 300)
    notify.run_incidents(dal, BOTH, T0 + 300, tgt=TGT)
    dal.update_incident(iid, tier="critical")                      # worse (rules/live.py)
    notify.run_incidents(dal, BOTH, T0 + 600, tgt=TGT)
    notify.run_incidents(dal, BOTH, T0 + 900, tgt=TGT)
    assert [m.priority for m in wire.sent] == [3, 5]               # once each
    assert "Now critical: AC-6" in wire.sent[1].text()


def test_switching_action_pushes_off_and_on_does_not_replay_the_gap(tmp_path, monkeypatch):
    dal, wire = _wired(tmp_path, monkeypatch)
    notify.run_incidents(dal, BOTH, T0, tgt=TGT)
    notify.run_incidents(dal, CRIT, T0 + 300, tgt=TGT)              # ACTION off ...
    _inc(dal, "AC-5", "action", T0 + 600)                         # ... while this opened
    notify.run_incidents(dal, BOTH, T0 + 900, tgt=TGT)              # ... and on again
    assert wire.sent == []


def test_an_incident_pushed_by_the_first_version_is_not_pushed_again(tmp_path, monkeypatch):
    """AC-6's of 2026-10-02 was pushed before the tier it went out at was recorded."""
    dal, wire = _wired(tmp_path, monkeypatch)
    iid = _inc(dal, "AC-6", "critical", T0)
    dal.update_incident(iid, notified_utc=T0 + 60)
    notify.run_incidents(dal, BOTH, T0 + 300, tgt=TGT)
    assert wire.sent == []


def _alarm(dal, unit, label, tier, started, *, notified=None):
    """A controller's own alarm point, as rules/live.py records it."""
    iid = dal.insert_incident(dict(
        rule="bas-alarm", system_code=unit, point_key="77", point_label=label,
        fingerprint=f"bas-alarm:{unit}:77", tier=tier, title="Controller alarm",
        detail=f"{unit}: {label} on", started_utc=started, opened_utc=started + 900,
        updated_utc=started + 900))
    if notified:
        dal.update_incident(iid, notified_utc=notified, notified_tier=tier)
    return iid


def test_an_alarm_is_called_by_its_own_point():
    """2026-10-26: AC-2's and AC-8's Multistack alarms were "controller alarm" everywhere."""
    assert catalog.name({"rule": "bas-alarm", "point_label": "Multistack Alarm",
                         "title": "Controller alarm"}) == "Multistack Alarm"
    assert catalog.name({"rule": "sensor-sanity", "point_label": "Boiler 3 Return Temp",
                         "title": "Sensor reading impossible values"}) \
        == "Boiler 3 Return Temp reading impossible values"
    assert catalog.name({"rule": "sat-setpoint", "point_label": "Discharge Air Temp",
                         "title": "Discharge air not meeting its setpoint"}) \
        == "Discharge air not meeting its setpoint"
    assert catalog.name({"rule": "bas-alarm", "point_label": None,
                         "title": "Controller alarm"}) == "Controller alarm"


def test_the_push_and_its_all_clear_name_the_alarm(tmp_path, monkeypatch):
    dal, wire = _wired(tmp_path, monkeypatch)
    notify.run_incidents(dal, BOTH, T0, tgt=TGT)                     # switched on
    _alarm(dal, "AC-8", "Multistack Alarm", "action", T0)
    notify.run_incidents(dal, BOTH, T0 + 1200, tgt=TGT)
    dal._conn.execute("UPDATE incidents SET status = 'resolved', resolved_utc = ?", (T0 + 3000,))
    notify.run_incidents(dal, BOTH, T0 + 3000, tgt=TGT)
    assert [m.title[2:] for m in wire.sent] == [
        "HVACTrend: AC-8: Multistack Alarm", "HVACTrend: AC-8: Multistack Alarm cleared"]


def _at(day, h, m=0):
    """Local time on a day of October 2026."""
    return int(time.mktime((2026, 10, day, h, m, 0, 0, 0, -1)))


def test_an_alarm_still_on_is_reminded_once_a_day_from_noon(tmp_path, monkeypatch):
    """2026-10-26: "important alarms should have reminders about it"."""
    dal, wire = _wired(tmp_path, monkeypatch)
    _alarm(dal, "AC-8", "Multistack Alarm", "action", _at(14, 13, 46), notified=_at(14, 14, 4))
    for t in (_at(26, 11, 55), _at(26, 12, 0), _at(26, 12, 5), _at(26, 23, 55)):
        notify.run_reminders(dal, BOTH, t, tgt=TGT)
    (m,) = wire.sent                                    # noon, once
    assert m.title == "\U0001f7e0 HVACTrend: AC-8: Multistack Alarm still on, 12 days"
    assert m.priority == 3 and m.lines == ["\u2022 AC-8: Multistack Alarm, 12 days (since Wed 14 Oct 13:46)"]
    assert "once a day" in m.footer
    notify.run_reminders(dal, BOTH, _at(27, 12, 0), tgt=TGT)
    assert len(wire.sent) == 2                          # and the next day again
    assert notify.status(dal)["reminder_count"] == 1


def test_the_reminder_waits_a_day_after_an_alarm_opens_or_is_pushed(tmp_path, monkeypatch):
    dal, wire = _wired(tmp_path, monkeypatch)
    _alarm(dal, "AC-2", "Multistack Alarm", "action", _at(25, 13, 0))   # opened 23 h ago
    old = _alarm(dal, "AC-8", "Multistack Alarm", "action", _at(20, 9, 0))
    dal._conn.execute("UPDATE incidents SET fingerprint = 'x' WHERE id = ?", (old,))
    dal.update_incident(old, tier="critical", notified_utc=_at(26, 1, 0),   # pushed again,
                        notified_tier="critical")                          # worse, at 01:00
    notify.run_reminders(dal, BOTH, _at(26, 12, 0), tgt=TGT)
    assert wire.sent == [] and notify.status(dal)["reminder_count"] == 0
    assert notify.status(dal)["reminder_date"] == "2026-10-26"            # done for today


def test_the_reminder_puts_critical_first_and_is_louder_for_it(tmp_path, monkeypatch):
    dal, wire = _wired(tmp_path, monkeypatch)
    _alarm(dal, "AC-2", "Multistack Alarm", "action", _at(14, 13, 46))
    c = _alarm(dal, "AC-3", "Freeze Condition", "critical", _at(24, 6, 0))
    dal._conn.execute("UPDATE incidents SET fingerprint = 'f' WHERE id = ?", (c,))
    _inc(dal, "AC-5", "action", _at(25, 9, 0))
    notify.run_reminders(dal, BOTH, _at(26, 12, 0), tgt=TGT)
    (m,) = wire.sent
    assert m.title == "\U0001f534 HVACTrend: 3 alarms still on" and m.priority == 4
    assert [line.split(",")[0] for line in m.lines] == [
        "\u2022 CRITICAL: AC-3: Freeze Condition", "\u2022 AC-2: Multistack Alarm",
        "\u2022 AC-5: Discharge air not meeting its setpoint"]


def test_only_the_tiers_that_are_pushed_are_reminded(tmp_path, monkeypatch):
    dal, wire = _wired(tmp_path, monkeypatch)
    _alarm(dal, "AC-8", "Multistack Alarm", "action", _at(14, 13, 46))
    notify.run_reminders(dal, _Cfg(), _at(26, 12, 0), tgt=TGT)            # pushes off
    notify.run_reminders(dal, CRIT, _at(26, 12, 5), tgt=TGT)              # ACTION not pushed
    assert wire.sent == []
    off = _Cfg(rules=BOTH["rules"], alerts={"reminders": False})
    notify.run_reminders(dal, off, _at(27, 12, 0), tgt=TGT)               # switched off
    assert wire.sent == []
    notify.run_reminders(dal, BOTH, _at(28, 12, 0), tgt=TGT)
    assert len(wire.sent) == 1


def test_the_reminder_time_is_a_setting(tmp_path, monkeypatch):
    dal, wire = _wired(tmp_path, monkeypatch)
    _alarm(dal, "AC-8", "Multistack Alarm", "action", _at(14, 13, 46))
    at8 = _Cfg(rules=BOTH["rules"], alerts={"reminder_time": "08:00"})
    notify.run_reminders(dal, at8, _at(26, 7, 55), tgt=TGT)
    assert wire.sent == []
    notify.run_reminders(dal, at8, _at(26, 8, 0), tgt=TGT)
    assert len(wire.sent) == 1


def test_a_failed_reminder_is_retried_on_the_next_run(tmp_path, monkeypatch):
    dal, wire = _wired(tmp_path, monkeypatch)
    _alarm(dal, "AC-8", "Multistack Alarm", "action", _at(14, 13, 46))
    monkeypatch.setattr(notify, "post", lambda *a, **k: (False, "HTTP 429"))
    out = notify.run_reminders(dal, BOTH, _at(26, 12, 0), tgt=TGT)
    assert out["errors"] == ["HTTP 429"] and notify.status(dal)["reminder_date"] is None
    monkeypatch.setattr(notify, "post", wire)
    notify.run_reminders(dal, BOTH, _at(26, 12, 5), tgt=TGT)
    assert len(wire.sent) == 1


def test_an_existing_database_gains_the_notified_tier_column(tmp_path):
    schema = (REPO / "db" / "schema.sql").read_text(encoding="utf-8")
    ddl = schema[schema.index("CREATE TABLE IF NOT EXISTS incidents"):]
    ddl = ddl[:ddl.index("\n);") + 3]
    c = sqlite3.connect(tmp_path / "old.db")
    c.execute("\n".join(line for line in ddl.splitlines() if "notified_tier" not in line))
    c.close()
    dal = SQLiteDAL(tmp_path / "old.db")
    dal.apply_schema()
    assert "notified_tier" in {r["name"] for r in dal._conn.execute("PRAGMA table_info(incidents)")}


# ── the check-in ─────────────────────────────────────────────────────────────────────────
def _checkin_world(tmp_path):
    dal = _world(tmp_path)
    now = T0 + 86400 + 7 * 3600
    t0, _ = checkin.yesterday(now)
    old = dict(rule="static-setpoint", system_code="AC-2", point_key="", fingerprint="s:AC-2:",
               tier="action", title="Duct static not meeting its setpoint", started_utc=t0 - 9 * 86400,
               opened_utc=t0 - 9 * 86400, updated_utc=now)
    new = dict(rule="boiler-rise", system_code="BLR-2", point_key="", fingerprint="b:BLR-2:",
               tier="action", title="Boiler enabled but not heating (no temperature rise)",
               started_utc=t0 + 3600, opened_utc=t0 + 6600, updated_utc=now)
    dal.insert_incident(old)
    dal.insert_incident(new)
    dal.set_meta("rules.last_run_utc", now - 120)
    return dal, now


def _equip(s):
    """The check-in's equipment lines (the empty test database fills the rest)."""
    return [a for a in s["attention"] if a.startswith(("AC-", "BLR-", "And "))]


def test_the_checkin_asks_about_every_important_alarm_still_on_by_name(tmp_path):
    """Until 2026-10-26 only NEW incidents asked for a look: AC-2's and AC-8's Multistack
    alarms sat 12 days in the quiet "Still open" line as "controller alarm"."""
    dal, now = _checkin_world(tmp_path)
    t0, _ = checkin.yesterday(now)
    _alarm(dal, "AC-8", "Multistack Alarm", "action", t0 - 12 * 86400)
    s = checkin.build(dal, _Cfg(), now)
    facts = dict(s["facts"])
    assert [a.split(" for ")[0] for a in _equip(s)] == [            # oldest first
        "AC-8: Multistack Alarm", "AC-2: Duct static not meeting its setpoint",
        "BLR-2: Boiler enabled but not heating (no temperature rise)"]
    assert _equip(s)[0].startswith("AC-8: Multistack Alarm for 13 days (since ")
    assert "Still open" not in facts
    assert facts["Equipment"] == "3 open incidents, 1 new since yesterday"


def test_a_critical_alarm_comes_first_and_a_watch_is_listed_quietly(tmp_path):
    dal, now = _checkin_world(tmp_path)
    dal._conn.execute("UPDATE incidents SET tier = 'critical' WHERE system_code = 'BLR-2'")
    dal._conn.execute("UPDATE incidents SET tier = 'watch' WHERE system_code = 'AC-2'")
    s = checkin.build(dal, _Cfg(), now)
    assert [a.split(":")[0] for a in _equip(s)] == ["BLR-2"]
    assert dict(s["facts"])["Still open"] == "AC-2: Duct static not meeting its setpoint"


def test_a_long_list_of_alarms_is_cut_short(tmp_path):
    dal, now = _checkin_world(tmp_path)
    t0, _ = checkin.yesterday(now)
    for k in range(9):
        i = _alarm(dal, f"AC-{k + 3}", "Multistack Alarm", "action", t0 - k * 3600)
        dal._conn.execute("UPDATE incidents SET fingerprint = ? WHERE id = ?", (f"m{k}", i))
    s = checkin.build(dal, _Cfg(), now)
    assert len(_equip(s)) == 9 and _equip(s)[-1] == "And 3 more equipment alarms still on."


def test_an_incident_that_came_and_went_is_listed_quietly(tmp_path):
    dal, now = _checkin_world(tmp_path)
    t0, _ = checkin.yesterday(now)
    dal._conn.execute("UPDATE incidents SET status = 'resolved', resolved_utc = ? "
                      "WHERE system_code = 'BLR-2'", (t0 + 9000,))
    s = checkin.build(dal, _Cfg(), now)
    assert not any(a.startswith("BLR-2") for a in s["attention"])
    assert "BLR-2: Boiler enabled" in dict(s["facts"])["Came and went"]


def test_the_checkin_says_when_the_rules_are_failing(tmp_path):
    dal, now = _checkin_world(tmp_path)
    dal.set_meta("rules.last_error", f"{now - 60} KeyError: 'DAT'")
    s = checkin.build(dal, _Cfg(), now)
    assert any("alarm rules are failing: KeyError" in a for a in s["attention"])


def test_the_checkin_says_when_the_alarms_are_not_reaching_the_phone(tmp_path):
    dal, now = _checkin_world(tmp_path)
    dal.set_meta("notify.incident_error", f"{now - 60} OperationalError")
    s = checkin.build(dal, _Cfg(), now)
    assert any("not reaching your phone" in a and "OperationalError" in a for a in s["attention"])


# ── the watchdog ─────────────────────────────────────────────────────────────────────────
def _watchdog(tmp_path, monkeypatch):
    """The real watchdog script against a database whose data is flowing (liveness healthy)."""
    dal = _world(tmp_path)
    now = int(time.time())
    _feed(dal, now - 3600, now)
    dal.close()
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("site: {code: DEMO, name: Demo Building}\n"
                   "bacnet:\n  local_address: '<COLLECTOR_IP>/24'\n"
                   "  device_instance: '<LOGGER_DEVICE_INSTANCE>'\n"
                   "  poll: {interval_s: 120}\n"
                   "  safety: {allow_writes: false, allow_cov: false}\n"
                   f"storage: {{path: '{(tmp_path / 'l.db').as_posix()}'}}\n"
                   "alerts: {checkin: false}\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("wd_t", REPO / "scripts" / "watchdog.py")
    wd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wd)
    monkeypatch.setattr(sys, "argv", ["watchdog.py", "--config", str(cfg), "--quiet"])
    return wd


def test_the_watchdog_survives_a_rules_crash(tmp_path, monkeypatch):
    wd = _watchdog(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise KeyError("DAT")
    monkeypatch.setattr(live, "run", boom)
    assert wd.main() == 0                      # the liveness result stands
    d = SQLiteDAL(tmp_path / "l.db")
    assert d.get_meta("rules.last_error").split(" ", 1)[1].startswith("KeyError")


def test_a_broken_rules_package_cannot_stop_the_watchdog(tmp_path, monkeypatch):
    """2026-10-27 review: notify and checkin imported rules.catalog at the top (for the alarm
    names, 10-26), so a syntax error in catalog.py or engine.py stopped the watchdog loading
    at all: no "data has stopped" alert, no pushes, no check-in. The rules load only inside
    the fence; without them an incident is called by its stored title."""
    import hvactrend
    for m in [k for k in sys.modules
              if k == "hvactrend.rules" or k.startswith("hvactrend.rules.")]:
        monkeypatch.setitem(sys.modules, m, None)          # importing it now raises
    monkeypatch.delattr(hvactrend, "rules", raising=False)
    for m in ("notify", "checkin"):                        # so their top-level imports re-run
        monkeypatch.delitem(sys.modules, f"hvactrend.{m}", raising=False)
        monkeypatch.delattr(hvactrend, m, raising=False)
    wd = _watchdog(tmp_path, monkeypatch)                  # loads at all
    assert wd.main() == 0                                  # the liveness result stands
    d = SQLiteDAL(tmp_path / "l.db")
    assert "Error" in d.get_meta("rules.last_error")       # and the fence says why
    d.close()
    from hvactrend import checkin as fresh_checkin      # the check-in loads too
    assert fresh_checkin.incident_name({"rule": "bas-alarm", "point_label": "Multistack Alarm",
                                        "title": "Controller alarm"}) == "Controller alarm"


def test_a_push_step_that_raises_is_recorded_until_it_works_again(tmp_path, monkeypatch):
    """Otherwise the equipment alarms could stop reaching the phone with no sign at all."""
    wd = _watchdog(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise sqlite3.OperationalError("no such column: notified_tier")
    monkeypatch.setattr(notify, "run_incidents", boom)
    assert wd.main() == 0                      # the liveness result stands
    d = SQLiteDAL(tmp_path / "l.db")
    assert d.get_meta("notify.incident_error").split(" ", 1)[1] == "OperationalError"
    d.close()
    monkeypatch.setattr(notify, "run_incidents", lambda *a, **k: {"errors": []})
    assert wd.main() == 0
    d = SQLiteDAL(tmp_path / "l.db")
    assert d.get_meta("notify.incident_error") is None


def test_the_watchdog_sends_the_reminder_and_records_it_failing(tmp_path, monkeypatch):
    wd = _watchdog(tmp_path, monkeypatch)
    calls = []

    def boom(dal, cfg, now, **k):
        calls.append(now)
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(notify, "run_reminders", boom)
    assert wd.main() == 0 and len(calls) == 1
    d = SQLiteDAL(tmp_path / "l.db")
    assert d.get_meta("notify.incident_error").split(" ", 1)[1] == "OperationalError"

# ── 2026-10-27 review ────────────────────────────────────────────────────────────────────
def test_an_episode_inside_a_gap_in_the_runs_is_recorded_and_pushed_once(tmp_path, monkeypatch):
    """The watchdog did not run for 8 h; a boiler failed to heat for 3 of them and recovered.
    A fixed 4 h window never saw it, and an incident first recorded closed was never pushed."""
    dal, wire = _wired(tmp_path, monkeypatch)
    _feed(dal, T0 - 8 * 3600, T0, rise=20.0)
    live.run(dal, _Cfg(), T0)                                   # healthy; the last run
    notify.run_incidents(dal, BOTH, T0, tgt=TGT)                # pushes on
    _feed(dal, T0, T0 + 3 * 3600)                               # not heating
    _feed(dal, T0 + 3 * 3600, T0 + 8 * 3600, rise=20.0)         # heating again
    live.run(dal, _Cfg(), T0 + 8 * 3600)
    assert [r[2] for r in _rows(dal)] == ["resolved"]
    notify.run_incidents(dal, BOTH, T0 + 8 * 3600, tgt=TGT)
    notify.run_incidents(dal, BOTH, T0 + 8 * 3600 + 300, tgt=TGT)
    (m,) = wire.sent                                            # once, and no all-clear
    assert m.title.endswith("BLR-2: Boiler enabled but not heating (no temperature rise) "
                            "came and went")
    assert "over before the alarm rules next ran" in m.footer and m.priority == 3


def test_came_and_went_does_not_replay_history(tmp_path, monkeypatch):
    dal, wire = _wired(tmp_path, monkeypatch)
    iid = _inc(dal, "AC-5", "critical", T0)
    dal.update_incident(iid, status="resolved", resolved_utc=T0 + 600)
    notify.run_incidents(dal, BOTH, T0 + 3600, tgt=TGT)         # first run after the change
    assert wire.sent == []


def test_an_incident_closed_unjudged_is_not_announced_as_cleared(tmp_path, monkeypatch):
    dal, wire = _wired(tmp_path, monkeypatch)
    notify.run_incidents(dal, BOTH, T0, tgt=TGT)
    iid = _inc(dal, "BLR-2", "action", T0 + 600)
    notify.run_incidents(dal, BOTH, T0 + 900, tgt=TGT)          # pushed
    dal.update_incident(iid, status="resolved", resolved_utc=T0 + 3600,
                        resolve_reason="not judged")
    notify.run_incidents(dal, BOTH, T0 + 4 * 86400, tgt=TGT)
    m = wire.sent[-1]
    assert "NOT cleared" in m.title and "\u2705" not in m.title
    assert "nothing to judge it by" in m.lines[0]
    notify.run_incidents(dal, BOTH, T0 + 4 * 86400 + 300, tgt=TGT)
    assert len(wire.sent) == 2                                  # once


def test_the_checkin_and_the_summary_say_not_cleared_too(tmp_path):
    from hvactrend.ai import summary
    dal, now = _checkin_world(tmp_path)
    t0, _ = checkin.yesterday(now)
    dal._conn.execute("UPDATE incidents SET status = 'resolved', resolved_utc = ?, "
                      "resolve_reason = 'not judged' WHERE system_code = 'BLR-2'", (t0 + 9000,))
    s = checkin.build(dal, _Cfg(), now)
    assert "NOT cleared" in dict(s["facts"])["Came and went"]
    text = summary.render(summary.build_inputs(dal, _Cfg(), "daily", t0, now))
    assert "WITHOUT clearing" in text


def test_one_rejected_push_does_not_hold_back_the_others(tmp_path, monkeypatch):
    dal, wire = _wired(tmp_path, monkeypatch)
    notify.run_incidents(dal, BOTH, T0, tgt=TGT)
    _inc(dal, "AC-1", "action", T0 + 600)
    _inc(dal, "AC-2", "action", T0 + 600)
    calls = []

    def picky(tgt, msg, *, timeout=10.0):
        calls.append(msg.title)
        return (False, "HTTP 413") if "AC-1" in msg.title else (True, "HTTP 200")
    monkeypatch.setattr(notify, "post", picky)
    out = notify.run_incidents(dal, BOTH, T0 + 900, tgt=TGT)
    assert out["pushed"] == 1 and out["errors"] == ["HTTP 413"]
    pushed = {r["system_code"] for r in dal._conn.execute(
        "SELECT system_code FROM incidents WHERE notified_utc IS NOT NULL")}
    assert pushed == {"AC-2"}


def test_the_checkin_says_when_a_push_was_rejected(tmp_path):
    dal, now = _checkin_world(tmp_path)
    dal.set_meta("notify.last_ok_utc", now - 7200)
    dal.set_meta("notify.last_error_utc", now - 60)
    dal.set_meta("notify.last_error", "incident: HTTP 413")
    s = checkin.build(dal, _Cfg(), now)
    assert any("was not delivered: incident: HTTP 413" in a for a in s["attention"])
    dal.set_meta("notify.last_ok_utc", now - 30)               # delivered since: not news
    s = checkin.build(dal, _Cfg(), now)
    assert not any("not delivered" in a for a in s["attention"])


def test_with_the_rules_off_nothing_is_pushed_or_reminded(tmp_path, monkeypatch):
    dal, wire = _wired(tmp_path, monkeypatch)
    _alarm(dal, "AC-8", "Multistack Alarm", "action", _at(14, 13, 46), notified=_at(14, 14, 4))
    off = _Cfg(rules={"enabled": False, "push_critical": True, "push_action": True})
    notify.run_reminders(dal, off, _at(26, 12, 0), tgt=TGT)
    notify.run_incidents(dal, off, _at(26, 12, 0), tgt=TGT)
    assert wire.sent == []


def test_a_second_spell_is_counted_and_kept(tmp_path):
    dal = _world(tmp_path)
    _feed(dal, T0, T0 + 3 * 3600)
    live.run(dal, _Cfg(), T0 + 3 * 3600)
    _feed(dal, T0 + 3 * 3600, T0 + 3 * 3600 + 600, rise=20.0)  # a short recovery, not a clear
    _feed(dal, T0 + 3 * 3600 + 600, T0 + 6 * 3600)
    live.run(dal, _Cfg(), T0 + 6 * 3600)
    _feed(dal, T0 + 6 * 3600, T0 + 6 * 3600 + 300)
    live.run(dal, _Cfg(), T0 + 6 * 3600 + 300)
    assert dal._conn.execute("SELECT occurrences FROM incidents").fetchone()[0] == 2


def _device_world(tmp_path, monkeypatch):
    from hvactrend.models import Alert
    wd = _watchdog(tmp_path, monkeypatch)
    dal = SQLiteDAL(tmp_path / "l.db")
    dal.apply_schema()
    dal.upsert_alert(Alert(kind="device-stale", severity="high", message="AC panel dark",
                           fingerprint="device-stale:101000", created_utc=T0, last_seen_utc=T0,
                           system_code="101000", source="watchdog"))
    return wd, dal


def test_a_device_check_that_fails_does_not_send_all_clears(tmp_path, monkeypatch):
    wd, dal = _device_world(tmp_path, monkeypatch)

    def boom():
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(dal, "poll_health_latest", boom)
    wd.check(dal, _Cfg(), T0 + 300)
    assert [a["fingerprint"] for a in dal.open_alerts()] == ["device-stale:101000"]


def test_a_controller_with_nothing_left_to_log_is_not_called_dark(tmp_path, monkeypatch):
    wd, dal = _device_world(tmp_path, monkeypatch)
    dal._conn.execute("UPDATE alerts SET status = 'resolved'")
    monkeypatch.setattr(dal, "poll_health_latest", lambda: [dict(
        device_instance=202000, name="old panel", address="192.0.2.99", ts_utc=T0 - 86400,
        ok=1, reads_attempted=10, reads_ok=10, error=None)])
    monkeypatch.setattr(dal, "poll_targets", lambda: [{"address": "192.0.2.176"}])
    res = wd.check(dal, _Cfg(), T0)
    assert not any("old panel" in p for p in res["problems"])
    monkeypatch.setattr(dal, "poll_targets", lambda: [{"address": "192.0.2.99"}])
    res = wd.check(dal, _Cfg(), T0)
    assert any("old panel" in p for p in res["problems"])       # still logged: still watched
