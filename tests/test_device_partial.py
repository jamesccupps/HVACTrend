"""A controller that answers, but not for all of its points (2026-10-13/14).

The tower panel reset and reloaded an old database. 45 of its 84 logged points did not exist
for 27 hours, and nothing said so: the panel itself kept answering. Then, while it was being
reloaded, it refused grouped reads, and reading it one point at a time took 28 minutes, during
which no other controller was polled. These pin:
  * the watchdog raises "points not answering" after three short cycles, names the points
    (readable names first) and says since when, and clears when they answer again;
  * a device gets a time budget per cycle: the one-at-a-time fallback asks a point that does
    not give its present value for nothing more, and stops when the budget runs out.
"""

import asyncio
import importlib.util
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from hvactrend.collector import service
from hvactrend.models import PollHealth, Quality, Sample
from hvactrend.storage.sqlite_dal import SQLiteDAL

REPO = Path(__file__).resolve().parents[1]
T0 = 1_790_000_000


def _watchdog():
    spec = importlib.util.spec_from_file_location("wd_partial", REPO / "scripts" / "watchdog.py")
    wd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wd)
    return wd


def _panel(tmp_path):
    dal = SQLiteDAL(tmp_path / "p.db")
    dal.apply_schema()
    c = dal._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'DEMO','Demo Building')")
    c.execute("INSERT INTO buildings(id,site_id,code,name) VALUES (1,1,'MAIN','Main')")
    c.execute("INSERT INTO systems(id,building_id,code,name,kind) VALUES (1,1,'CT','CT','plant')")
    c.execute("INSERT INTO devices(id,site_id,device_instance,address,name,created_utc) "
              "VALUES (1,1,100300,'192.0.2.125','PXCM100300',0)")
    for pid, name, shown in ((1, "SITE.CHW.SEQ", None), (2, "TWR1.LVL.SP", "Tower 1 Level SP"),
                             (3, "PMP1.STS", "Pump 1 Status"), (4, "SITE.CHW.HDR.SUP.TEMP", None),
                             (5, "SITE.CHW.HDR.RET.TEMP", None), (6, "SITE.CT1.SPD", None)):
        c.execute("INSERT INTO points(id,device_id,system_id,source,object_type,object_instance,"
                  "name,display_name,enabled,created_utc) VALUES (?,1,1,'bacnet','analog-value',?,"
                  "?,?,1,0)", (pid, pid, name, shown))
    return dal


def _cycle(dal, ts, missing=(), answered=True):
    dal.write_samples([Sample(point_id=p, ts_utc=ts,
                              value_num=None if (p in missing or not answered) else 70.0,
                              quality=Quality.COMM_LOSS if (p in missing or not answered)
                              else Quality.GOOD) for p in range(1, 7)])
    ok = 0 if not answered else 6 - len(missing)
    dal.record_poll_health(PollHealth(device_id=1, ts_utc=ts, ok=ok > 0, reads_attempted=6,
                                      reads_ok=ok, method="rpm"))


def _open(dal):
    return {a["kind"]: a for a in dal.open_alerts() if a.get("source") == "watchdog"}


def test_points_that_stop_answering_on_a_live_controller_raise_an_alert(tmp_path):
    wd, dal = _watchdog(), _panel(tmp_path)
    _cycle(dal, T0)
    _cycle(dal, T0 + 120, answered=False)                 # the reset: nothing at all
    for k in (2, 3):
        _cycle(dal, T0 + 120 * k, missing=(1, 2, 3))      # back, without three points
    wd.check(dal, None, T0 + 370)
    assert "device-points-missing" not in _open(dal)      # two short cycles: not yet
    _cycle(dal, T0 + 480, missing=(1, 2, 3))
    wd.check(dal, None, T0 + 490)
    a = _open(dal)["device-points-missing"]
    since = time.strftime("%a %m-%d %H:%M", time.localtime(T0 + 240))
    assert a["message"].startswith(f"PXCM100300: 3 of 6 logged points not answering since {since}")
    assert a["message"].endswith(": CT Pump 1 Status, CT Tower 1 Level SP, CT SITE.CHW.SEQ.")
    _cycle(dal, T0 + 600)                                 # the right database is back
    wd.check(dal, None, T0 + 610)
    assert "device-points-missing" not in _open(dal)


def test_two_missing_points_are_not_an_alert(tmp_path):
    wd, dal = _watchdog(), _panel(tmp_path)
    for k in range(5):
        _cycle(dal, T0 + 120 * k, missing=(1, 2))
    wd.check(dal, None, T0 + 610)
    assert "device-points-missing" not in _open(dal)
    (d,) = dal.poll_health_latest()           # and the history test on its own agrees
    assert wd._points_missing(dal, d, "panel", T0 + 610, 3, 3) is None
    assert wd._points_missing(dal, d, "panel", T0 + 610, 2, 3).startswith("panel: 2 of 6")


# ── the viewer is told (api/app.py _outages) ─────────────────────────────────────────────
def test_the_series_api_says_when_a_point_gave_no_answer():
    """The viewer showed the 27 hours only as a 3-px ribbon: each series now carries its
    stretches with no good reading, and what kind (no answer, or a flagged reading)."""
    from hvactrend.api.app import _outages
    raw = [{"t": T0 + 120 * k, "v": None if 3 <= k < 10 else 17.0,
            "q": "comm-loss" if 3 <= k < 10 else "good"} for k in range(15)]
    assert _outages(None, 1, raw, None, 120) == [{"a": T0 + 360, "b": T0 + 1200, "q": "comm-loss"}]

    class _Mix:                                    # a rollup window asks the raw samples
        def bad_quality_mix(self, pid, a, b):
            return {"fault": 3, "comm-loss": 9}
    buckets = [{"t": T0 + 300 * k, "v": None if k in (2, 3) else 17.0,
                "n": 0 if k in (2, 3) else 5, "nt": 5} for k in range(6)]
    assert _outages(_Mix(), 1, buckets, "5m") == [{"a": T0 + 600, "b": T0 + 1200, "q": "comm-loss"}]
    # nothing recorded at all (nt unknown) is a gap in the record, not the point's silence
    assert _outages(_Mix(), 1, [{"t": T0, "v": None, "n": 0, "nt": None}], "5m") == []


# ── the collector's time budget ──────────────────────────────────────────────────────────
class _App:
    """Refuses grouped reads; every single read costs 10 s on the fake clock."""
    def __init__(self, clock, answer):
        self.clock, self.answer, self.reads = clock, answer, []

    async def read_property_multiple(self, address, params):
        self.clock[0] += 1
        return TimeoutError("RPM refused")

    async def read_property(self, address, oid, prop):
        self.clock[0] += 10
        self.reads.append((oid, prop))
        return self.answer(prop)


def _collector(monkeypatch, app, clock):
    fake_time = SimpleNamespace(monotonic=lambda: clock[0], time=time.time,
                                localtime=time.localtime, strftime=time.strftime)
    monkeypatch.setattr(service, "time", fake_time)
    return service.Collector(app, dal=None, config={"bacnet": {"poll": {
        "device_budget_s": 45, "rpm_batch": 10}}})


def test_a_panel_that_does_not_answer_cannot_stall_the_cycle(monkeypatch):
    clock = [0.0]
    app = _App(clock, lambda prop: TimeoutError("no answer"))
    col = _collector(monkeypatch, app, clock)
    pts = [{"point_id": i, "object_type": "analog-value", "object_instance": i} for i in range(20)]
    samples, health = asyncio.run(col._poll_device("192.0.2.125", 1, pts, T0))
    assert len(samples) == 20 and all(s.quality == Quality.COMM_LOSS for s in samples)
    assert {p for _, p in app.reads} == {"present-value"}  # nothing more from a silent point
    assert len(app.reads) <= 5                            # 45 s of 10-s reads, not 80 reads
    assert "budget ran out" in health.error and health.reads_attempted == 20


def test_a_point_that_answers_one_at_a_time_is_still_read_in_full(monkeypatch):
    clock = [0.0]
    app = _App(clock, lambda prop: {"present-value": 55.0}.get(prop, None))
    col = _collector(monkeypatch, app, clock)
    pts = [{"point_id": 1, "object_type": "analog-value", "object_instance": 1}]
    (s,), health = asyncio.run(col._poll_device("192.0.2.125", 1, pts, T0))
    assert s.quality == Quality.GOOD and s.value_num == 55.0
    assert [p for _, p in app.reads] == list(service._SAMPLE_PROPS)
    assert health.error is None and health.method == "single"
