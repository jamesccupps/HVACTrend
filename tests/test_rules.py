"""Rule-based alarms: the incident machine, the operating-state gate, the
thresholds as settings, grouping, and the evaluation end to end on a scratch database.

What these pin, each learned from the old AI layer or the 2026-09-30 replay:
  * a breach must HOLD for a tier's minutes before anything opens (debounce);
  * an incident closes only after the value has been clearly back for a while (hysteresis),
    never because it dipped under the threshold for one sample (64 % used to flap shut);
  * nothing is judged while a unit is off, unoccupied or settling;
  * a failed sensor neither raises nor hides a boiler's no-rise alarm, and is itself caught
    even when its controller flags the garbage "good";
  * one cause on many units is one message.
"""

import math
import sqlite3

import pytest

from hvactrend import coverage
from hvactrend.rules import catalog, state
from hvactrend.rules.engine import Episode, Tier, episodes
from hvactrend.rules.group import group

M = 60


def _tiers():
    return [Tier("action", 30, lambda v: v >= 5), Tier("critical", 10, lambda v: v >= 10)]


def _run(values, *, judged=None, step=120, clear_s=30 * M, **kw):
    s = [(i * step, v, True if judged is None else judged[i]) for i, v in enumerate(values)]
    return episodes(s, _tiers(), clear=lambda v: v < 2.5, clear_s=clear_s, **kw)


# ── the incident machine ─────────────────────────────────────────────────────────────────
def test_a_breach_shorter_than_its_minutes_never_opens():
    assert _run([0] * 5 + [6] * 14 + [0] * 30) == []        # 26 min of +6: under 30


def test_a_breach_that_holds_opens_when_its_minutes_are_up():
    (e,) = _run([0] * 5 + [6] * 20 + [0] * 30)
    assert (e.start, e.opened, e.tier) == (5 * 120, 20 * 120, "action")  # 30 min after start
    assert e.end == 25 * 120 and e.reason == "cleared"    # back at once, confirmed 30 min on


def test_a_worse_breach_escalates_the_open_incident():
    (e,) = _run([6] * 16 + [12] * 6 + [0] * 20)
    assert e.tier == "critical" and e.escalations == [("critical", 21 * 120)]
    assert e.peak == 12


def test_hovering_at_the_threshold_does_not_close_it():
    """4 °F is under the 5 °F threshold but not clearly back (< 2.5): the incident stays."""
    (e,) = _run([6] * 16 + [4] * 40 + [6] * 2 + [1] * 10)
    assert e.end is None                                  # 20 min of clear: not yet 30


def test_one_bad_sample_restarts_the_clear_clock():
    (e,) = _run([6] * 16 + [1] * 14 + [6] + [1] * 20)
    assert e.end == 31 * 120                              # the clear run began after the blip
    (e,) = _run([6] * 16 + [1] * 14 + [4] + [1] * 20)     # so does a dip back into the band
    assert e.end == 31 * 120


def test_a_stop_breaks_the_breach_run():
    """18 min of breach, one poll unjudged (too short to count as a data gap), 18 more: two
    short spells, not one 40-minute one."""
    vals = [6] * 10 + [6] + [6] * 10
    judged = [True] * 10 + [False] + [True] * 10
    assert _run(vals, judged=judged) == []


def test_a_stopped_unit_is_neither_a_breach_nor_a_recovery():
    vals = [6] * 16 + [0] * 30 + [6] * 5
    judged = [True] * 16 + [False] * 30 + [True] * 5
    (e,) = _run(vals, judged=judged)
    assert e.end is None and e.last_breach == 50 * 120    # still open across the stop


def test_a_data_gap_breaks_the_breach_run():
    s = [(t, 6, True) for t in range(0, 20 * M, 120)] + \
        [(t, 6, True) for t in range(40 * M, 55 * M, 120)]
    assert episodes(s, _tiers(), clear=lambda v: v < 2.5, clear_s=30 * M) == []


def test_an_incident_nobody_can_judge_for_days_closes_itself():
    s = [(t, 6, True) for t in range(0, 40 * M, 120)] + [(5 * 86400, 0, True)]
    (e,) = episodes(s, _tiers(), clear=lambda v: v < 2.5, clear_s=30 * M,
                    idle_close_s=3 * 86400)
    assert e.reason == "not judged" and e.end == 38 * M    # its last judged sample
    seeded = Episode(start=0, opened=30 * M, tier="action", last_breach=40 * M)
    (e,) = episodes([], _tiers(), clear=lambda v: v < 2.5, clear_s=30 * M,
                    initial=seeded, until=4 * 86400)
    assert e.reason == "not judged"                       # measured from its last breach


def test_a_seeded_incident_continues_instead_of_opening_twice():
    seeded = Episode(start=0, opened=30 * M, tier="action", last_breach=60 * M)
    out = episodes([(70 * M + i * 120, 6, True) for i in range(20)], _tiers(),
                   clear=lambda v: v < 2.5, clear_s=30 * M, initial=seeded)
    assert out == [seeded] and seeded.end is None and seeded.last_breach == 70 * M + 19 * 120


def test_the_quiet_hours_before_a_seeded_incident_do_not_close_it():
    """Live, 2026-10-01: an alarm opened at 14:05; the next run re-read its 4-hour window,
    found the quiet hours before 14:03 'clearly back', and closed it at 10:07, hours before
    it began. History before the incident's last breach can never end it."""
    seeded = Episode(start=14 * 3600, opened=14 * 3600 + 120, tier="action",
                     last_breach=14 * 3600 + 240)
    s = ([(t, 0.0, True) for t in range(10 * 3600, 14 * 3600, 120)]
         + [(t, 6.0, True) for t in range(14 * 3600, 14 * 3600 + 600, 120)])
    out = episodes(s, _tiers(), clear=lambda v: v < 2.5, clear_s=30 * M, initial=seeded)
    assert out == [seeded] and seeded.end is None
    assert seeded.last_breach == 14 * 3600 + 480                # extended by the NEW samples only


def test_a_critical_breach_builds_up_across_live_runs():
    """The history a seeded incident holds still times the tiers: an ACTION incident whose
    breach crosses the CRITICAL line escalates once the critical run reaches 10 minutes, even
    though each live run brings only 5 new minutes."""
    seeded = Episode(start=0, opened=30 * M, tier="action", last_breach=40 * M)
    s = ([(t, 6.0, True) for t in range(0, 34 * M, 120)]
         + [(t, 12.0, True) for t in range(34 * M, 45 * M, 120)])
    (e,) = episodes(s, _tiers(), clear=lambda v: v < 2.5, clear_s=30 * M, initial=seeded)
    assert e.tier == "critical" and e.escalations == [("critical", 44 * M)]


def test_separate_spells_are_counted_on_one_incident():
    (e,) = _run([6] * 16 + [4] * 5 + [6] * 16 + [1] * 20)
    assert e.occurrences == 2


def test_a_boilers_worst_rise_is_the_smallest():
    t = [Tier("action", 50, lambda v: v < 5)]
    s = [(i * 120, v, True) for i, v in enumerate([3.0] * 10 + [-0.8] + [2.0] * 20)]
    (e,) = episodes(s, t, clear=lambda v: v >= 7, clear_s=20 * M, worse=lambda a, b: a < b)
    assert e.peak == -0.8


# ── the operating-state gate ─────────────────────────────────────────────────────────────
def test_spans_end_on_off_samples_and_on_gaps():
    s = [(0, 50), (120, 50), (240, 0), (360, 50), (2000, 50), (2120, None), (2240, 50)]
    assert state.on_spans(s, threshold=10) == [(0, 120), (360, 360), (2000, 2240)]


def test_the_gate_needs_every_signal_and_each_one_settled():
    fan = [(0, 1000)]
    occ = [(300, 1000)]
    g = state.make_gate([fan, occ], settle_s=200)
    assert not g(250) and not g(450) and g(500) and not g(1100)   # occupancy settles too
    assert state.make_gate([fan, None], settle_s=200)(250)         # no occupancy signal
    assert not state.make_gate([[]], settle_s=0)(10)               # signal, never on


def test_two_fans_either_running_is_running():
    assert state.union([(0, 100)], [(50, 300)], [(500, 600)]) == [(0, 300), (500, 600)]


def test_trane_occupancy_two_is_unoccupied():
    assert state.occupied_series([(0, 1.0), (1, 2.0), (2, 3.0), (3, None)]) == \
        [(0, 1.0), (1, 0.0), (2, 1.0), (3, None)]


# ── thresholds are settings ──────────────────────────────────────────────────────────────
def test_thresholds_come_from_the_config_and_a_unit_can_override_them():
    cfg = {"sat-setpoint": {"tiers": {"action": {"delta": 7}}},
           "overrides": {"AC-2": {"sat-setpoint": {"tiers": {"critical": None}}}}}
    s = catalog.settings("sat-setpoint", "AC-5", cfg)
    assert s["tiers"]["action"] == {"delta": 7, "minutes": 30}
    assert s["tiers"]["critical"] == {"delta": 10.0, "minutes": 10}
    assert "critical" not in catalog.settings("sat-setpoint", "AC-2", cfg)["tiers"]
    assert catalog.DEFAULTS["sat-setpoint"]["tiers"]["action"]["delta"] == 5.0   # untouched


def test_each_rules_tests_mean_what_they_say():
    s = catalog.settings("boiler-rise", None, {})
    (t,) = catalog.tiers_for("boiler-rise", s)
    assert t.breach(4.9) and not t.breach(5.0) and t.minutes == 50
    assert catalog.clear_for("boiler-rise", s)(7.0) and not catalog.clear_for("boiler-rise", s)(6.9)
    s = catalog.settings("sensor-sanity", None, {})
    (t,) = catalog.tiers_for("sensor-sanity", s, sane=(32, 250))
    assert t.breach(-60) and t.breach(math.nan) and not t.breach(140)
    s = catalog.settings("bas-alarm", None, {})
    (t,) = catalog.tiers_for("bas-alarm", s, critical=True)
    assert (t.name, t.minutes) == ("critical", 2.0)
    (t,) = catalog.tiers_for("bas-alarm", s)
    assert (t.name, t.minutes) == ("action", 15.0)
    assert catalog.alarm_is_critical("Freezestat|dac-3_UIP~_07")
    assert catalog.alarm_is_critical("HB3 Flame Failure") and not catalog.alarm_is_critical("SF~-A Failure")


def test_a_finding_without_a_peak_still_reads():
    """An incident continued from the database can carry no peak (NaN is stored as NULL):
    its message must not crash (2026-10-13), for any rule."""
    from hvactrend.rules.evaluate import _finding
    e = Episode(start=1_790_000_000, opened=1_790_000_600, tier="action")
    for rule, want in (("sat-setpoint", "discharge air ? °F"), ("static-setpoint", "static ? in."),
                       ("boiler-rise", "only ? °F"), ("sensor-sanity", "flagged unreliable")):
        assert want in _finding(rule, "AC-6", "", {"name": "X"}, e).detail, rule


def test_the_alarms_read_what_the_coverage_review_says_they_read():
    """One table (coverage.RULES) for what a rule needs, so the review and the alarms cannot
    disagree about which points matter."""
    reads = {"sat-setpoint": {"DAT", "DAT_ACTIVE_SP", "FAN_SPEED", "FAN_STATUS", "OCCUPANCY"},
             "static-setpoint": {"DUCT_PRESS", "DUCT_PRESS_SP", "FAN_SPEED", "FAN_STATUS",
                                 "OCCUPANCY"},
             "boiler-rise": {"BLR_CMD", "BLR_SUP_TEMP", "BLR_RET_TEMP"}}
    table = {r["id"]: r for r in coverage.RULES}
    op = table["operating-state"]
    for rule in catalog.UNIT_RULES:
        r = table[rule]
        declared = {x for req in r["needs"] + r["gate"] + r["evidence"] + op["needs"]
                    for x in coverage._roles_of(req)}
        assert reads[rule] <= declared, (rule, reads[rule] - declared)


# ── grouping ─────────────────────────────────────────────────────────────────────────────
class _F:
    def __init__(self, unit, opened, tier="action", title="Discharge air not meeting its setpoint"):
        self.unit, self.tier, self.title, self.detail = unit, tier, title, f"{unit} detail"
        self.episode = Episode(start=opened, opened=opened, tier=tier)


def test_one_cause_on_many_units_is_one_message():
    fs = [_F("AC-3", 0), _F("AC-5", 600, "critical"), _F("AC-10", 1500),
          _F("AC-7", 9000), _F("AC-7", 9060, title="Controller alarm")]
    g = group(fs)
    assert [(x.kind, x.tier, x.units) for x in g] == [
        ("building", "critical", ["AC-3", "AC-5", "AC-10"]), ("unit", "action", ["AC-7"])]
    assert len(g[1].findings) == 2                         # two findings, one unit, one message


def test_two_units_are_still_two_messages():
    assert [x.kind for x in group([_F("AC-3", 0), _F("AC-5", 60)])] == ["unit", "unit"]


# ── end to end, on a scratch database ────────────────────────────────────────────────────
def _db(tmp_path):
    from hvactrend.models import Quality, Sample
    from hvactrend.storage.sqlite_dal import SQLiteDAL
    dal = SQLiteDAL(tmp_path / "r.db")
    dal.apply_schema()
    c = dal._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'DEMO','Demo Building')")
    c.execute("INSERT INTO buildings(id,site_id,code,name) VALUES (1,1,'MAIN','Main')")
    for sid, code, kind, mode in ((1, "AC-1", "ahu", None), (2, "AC-9", "ahu", "vacant"),
                                  (3, "BLR-2", "plant", "always")):
        c.execute("INSERT INTO systems(id,building_id,code,name,kind,occupancy_mode) "
                  "VALUES (?,1,?,?,?,?)", (sid, code, code, kind, mode))
    c.execute("INSERT INTO devices(id,site_id,device_instance,address,created_utc) "
              "VALUES (1,1,100100,'192.0.2.19',0)")
    pts = [  # id, system, name, role, canonical, type, units
        (1, 1, "Discharge Air Temp|dac-3_UIP~_06", "DAT", 1, "analog-input", "degrees-fahrenheit"),
        (2, 1, "Discharge Air Setpoint|dac-3_AV~_009", "DAT_ACTIVE_SP", 1, "analog-input", "degrees-fahrenheit"),
        (3, 1, "SF~-A VFD Mod|dac-3_AOP~_01", "FAN_SPEED", 1, "analog-input", "percent"),
        (4, 1, "Occupancy Status|dac-3", "OCCUPANCY", 1, "multi-state-input", None),
        (11, 2, "Discharge Air Temp|dac-1_UIP~_06", "DAT", 1, "analog-input", "degrees-fahrenheit"),
        (12, 2, "Discharge Air Setpoint|dac-1_AV~_009", "DAT_ACTIVE_SP", 1, "analog-input", "degrees-fahrenheit"),
        (13, 2, "SF~-A VFD Mod|dac-1_AOP~_01", "FAN_SPEED", 1, "analog-input", "percent"),
        (21, 3, "Boiler 2 Supply Temp", "BLR_SUP_TEMP", 1, "analog-input", "degrees-fahrenheit"),
        (22, 3, "Boiler 2 Return Temp", "BLR_RET_TEMP", 1, "analog-input", "degrees-fahrenheit"),
        (23, 3, "Boiler 2 Enable", "BLR_CMD", 1, "binary-value", None),
    ]
    for pid, sid, name, role, canon, ot, units in pts:
        c.execute("INSERT INTO points(id,device_id,system_id,source,object_type,object_instance,"
                  "name,role,role_canonical,units_text,enabled,created_utc) "
                  "VALUES (?,1,?,'bacnet',?,?,?,?,?,?,1,0)", (pid, sid, ot, pid, name, role, canon, units))
    T0, samples = 1_000_000, []
    for i in range(150):                        # 5 hours at 2-minute polls
        t = T0 + i * 120
        m = i * 2                               # minutes since the fan started
        hot = 70 <= m < 110                     # +12 °F from minute 70 to 110
        early = m < 40                          # +12 °F while still settling: not judged
        dat = 67.0 if (hot or early) else 55.5
        for pid, v in ((1, dat), (2, 55.0), (3, 80.0), (4, 1.0),
                       (11, 70.0), (12, 55.0), (13, 80.0),                 # vacant: ignored
                       (21, 141.0), (23, 1.0)):
            samples.append(Sample(point_id=pid, ts_utc=t, value_num=v, quality=Quality.GOOD))
        # boiler 2's return: normal for 90 min, then a failed sensor reading -60 "good"
        samples.append(Sample(point_id=22, ts_utc=t, value_num=140.5 if m < 90 else -60.0,
                              quality=Quality.GOOD))
    dal.write_samples(samples)
    dal.close()
    return tmp_path / "r.db", T0


def test_the_rules_end_to_end(tmp_path):
    from hvactrend.rules import evaluate as ev
    from hvactrend.storage.sqlite_dal import SQLiteDAL
    path, T0 = _db(tmp_path)
    dal = SQLiteDAL(path, read_only=True)
    found = ev.evaluate(dal, {}, T0, T0 + 150 * 120)
    got = sorted((f.rule, f.unit, f.tier) for f in found)
    assert got == [("boiler-rise", "BLR-2", "action"),
                   ("sat-setpoint", "AC-1", "critical"),
                   ("sensor-sanity", "BLR-2", "action")]
    sat = next(f for f in found if f.rule == "sat-setpoint")
    assert sat.episode.start == T0 + 70 * 60              # the settling breach is not judged
    rise = next(f for f in found if f.rule == "boiler-rise")
    assert rise.episode.end is None and rise.episode.peak == pytest.approx(0.5)
    assert rise.episode.last_breach < T0 + 90 * 60        # the dead sensor does not feed it


def test_a_point_the_site_rules_out_is_never_judged(tmp_path):
    from hvactrend.rules import evaluate as ev
    from hvactrend.storage.sqlite_dal import SQLiteDAL
    path, T0 = _db(tmp_path)
    know = {"ignore_points": [{"match": "Boiler 2 Return*", "why": "sensor being replaced"}]}
    found = ev.evaluate(SQLiteDAL(path, read_only=True), {}, T0, T0 + 150 * 120, knowledge=know)
    assert {f.rule for f in found if f.unit == "BLR-2"} == set()


def test_the_replay_cannot_write(tmp_path):
    from hvactrend.storage.sqlite_dal import SQLiteDAL
    path, _ = _db(tmp_path)
    dal = SQLiteDAL(path, read_only=True)
    with pytest.raises(sqlite3.OperationalError):
        dal._conn.execute("UPDATE points SET enabled = 0")
