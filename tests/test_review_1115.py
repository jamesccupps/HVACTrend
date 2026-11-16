"""The 2026-11-15 full-read review of this repository. Each test failed on the code before its
fix."""

import asyncio
import time
from types import SimpleNamespace

import pytest

T0 = 1_790_000_000


# ── finding 2: one timed-out request no longer turns grouped reads off for good ──────────
def _reject_unrecognized():
    from bacpypes3.apdu import RejectPDU, RejectReason
    return RejectPDU(reason=RejectReason.unrecognizedService)


def _no_reply():
    from bacpypes3.apdu import AbortPDU, AbortReason
    return AbortPDU(reason=AbortReason.noResponse)


class _OneDevice:
    """read_property_multiple answers with whatever `rpm` returns next; plain reads answer."""
    def __init__(self, *rpm):
        self.rpm, self.calls = list(rpm), []

    async def read_property_multiple(self, address, params):
        self.calls.append("rpm")
        r = self.rpm.pop(0) if len(self.rpm) > 1 else self.rpm[0]
        if r == "ok":
            return [(oid, prop, None, 55.0 if prop == "present-value" else None)
                    for oid, props in zip(params[0::2], params[1::2]) for prop in props]
        if isinstance(r, Exception):
            raise r
        return r()

    async def read_property(self, address, oid, prop, property_array_index=None):
        self.calls.append(prop)
        return 56.0 if prop == "present-value" else None


def test_discovery_marks_a_device_as_refusing_grouped_reads_only_when_it_says_so():
    pytest.importorskip("bacpypes3")
    from hvactrend.bacnet.discovery import DiscoveredDevice, _read_props

    def scan(*answers):
        dev = DiscoveredDevice(device_instance=1, address="192.0.2.10")
        app = _OneDevice(*answers)
        for _ in answers:
            asyncio.run(_read_props(app, dev.address, "analog-value,1", ["object-name"],
                                    use_rpm=True, device=dev))
        return dev.rpm_supported

    assert scan(_no_reply) is None                 # a timeout says nothing about RPM
    assert scan(TimeoutError("raised")) is None    # nor does a raised one
    assert scan("ok", _no_reply) is True           # it was True and stays True
    assert scan(_no_reply, "ok") is True           # was: False, set by the first timeout
    assert scan(_reject_unrecognized) is False     # "I don't do that": marked
    assert scan(_reject_unrecognized, "ok") is True   # and an answer after it proves it does


def _collector(monkeypatch, clock, app, **poll):
    from hvactrend.collector import service
    monkeypatch.setattr(service, "time", SimpleNamespace(
        monotonic=lambda: clock[0], time=lambda: T0 + clock[0], localtime=time.localtime,
        strftime=time.strftime))
    return service.Collector(app, None, {"bacnet": {"poll": {
        "device_budget_s": 45, "rpm_batch": 10, "interval_s": 60, **poll}}})


def _pts(n, rpm):
    return [{"point_id": i, "object_type": "analog-value", "object_instance": i,
             "rpm_supported": rpm} for i in range(1, n + 1)]


def test_a_device_marked_as_refusing_grouped_reads_is_asked_again(monkeypatch):
    pytest.importorskip("bacpypes3")
    clock = [0.0]
    # it does answer them (the mark came from a timeout): read grouped from the first cycle
    app = _OneDevice("ok")
    col = _collector(monkeypatch, clock, app)
    _s, health = asyncio.run(col._poll_device("192.0.2.10", 8, _pts(25, rpm=0), T0))
    assert app.calls == ["rpm"] * 3 and health.method == "rpm" and health.reads_ok == 25
    clock[0] += 60
    app.calls.clear()
    asyncio.run(col._poll_device("192.0.2.10", 8, _pts(25, rpm=0), T0 + 60))
    assert app.calls == ["rpm"] * 3                # and for the rest of the run

    # it really refuses them: one grouped request, then one at a time, re-asked hourly
    clock[0] = 0.0
    app = _OneDevice(_reject_unrecognized)
    col = _collector(monkeypatch, clock, app)
    _s, health = asyncio.run(col._poll_device("192.0.2.11", 9, _pts(25, rpm=0), T0))
    assert app.calls.count("rpm") == 1 and health.method == "single" and health.reads_ok == 25
    for minutes in (1, 30, 59):
        clock[0] = minutes * 60.0
        app.calls.clear()
        asyncio.run(col._poll_device("192.0.2.11", 9, _pts(25, rpm=0), T0))
        assert "rpm" not in app.calls
    clock[0] = 3600.0
    app.calls.clear()
    asyncio.run(col._poll_device("192.0.2.11", 9, _pts(25, rpm=0), T0))
    assert app.calls.count("rpm") == 1


# ── finding 3: a controller's declared object count is capped ────────────────────────────
def test_an_absurd_object_count_is_refused_not_read_for_ever():
    pytest.importorskip("bacpypes3")
    from bacpypes3.apdu import AbortPDU
    from hvactrend.bacnet.discovery import MAX_OBJECTS, _read_object_list

    class Huge:
        reads = 0

        async def read_property(self, address, oid, prop, property_array_index=None):
            if property_array_index == 0:
                return 4_000_000_000
            if property_array_index is None:
                return AbortPDU()                 # the whole list will not fit: by index
            Huge.reads += 1
            return f"analog-value,{property_array_index}"

    stats = {}
    oids = asyncio.run(_read_object_list(Huge(), "192.0.2.10", "device,1", stats=stats))
    assert oids == [] and Huge.reads == 0
    assert stats == {"declared": 4_000_000_000, "listed": 0, "complete": False}
    assert 50_000 <= MAX_OBJECTS <= 1_000_000


# ── finding 4: realtime reads off the loop, a window for them, four big reads at once ─────
def test_realtime_readings_are_read_off_the_event_loop(tmp_path):
    pytest.importorskip("bacpypes3")
    from fastapi.testclient import TestClient
    from tests.test_realtime import _app, _insert
    app = _app(tmp_path)
    with TestClient(app) as c:
        def on_the_loop(*a, **k):
            raise AssertionError("realtime readings read on the event loop")
        app.state.dal.fast_series = on_the_loop
        now = int(time.time())
        _insert(tmp_path, "(1, ?, 55.5, NULL, 'good'), (1, ?, 56.0, NULL, 'good')",
                (now - 10, now - 5))
        d = c.get(f"/api/realtime/data?ids=1&start={now - 60}&end={now}")
        assert d.status_code == 200, d.text
        assert d.json()["series"]["1"] == [[now - 10, 55.5, "good"], [now - 5, 56.0, "good"]]
        r = c.get(f"/api/realtime/export?ids=1&start={now - 60}&end={now}")
        assert r.status_code == 200, r.text
        assert "55.5" in r.text and "56.0" in r.text


def test_realtime_readings_are_kept_a_year_unless_config_says(tmp_path):
    from tests.test_realtime import _plant
    dal = _plant(tmp_path / "r.db")
    now = T0
    old, new = now - 400 * 86400, now - 10 * 86400

    def fill():
        c = dal._conn
        c.execute("DELETE FROM fast_samples")
        c.execute("DELETE FROM fast_runs")
        c.executemany("INSERT INTO fast_samples VALUES (1, ?, 55.0, NULL, 'good')",
                      [(old,), (new,)])
        c.executemany("INSERT INTO fast_runs(id, group_id, point_id, interval_s, started_utc, "
                      "ends_utc, stopped_utc, stop_reason) VALUES (?, ?, 1, 5, ?, ?, ?, ?)",
                      [(1, 1, old, old + 60, old + 60, "finished"),    # long over
                       (2, 2, new, new + 60, new + 60, "finished"),
                       (3, 3, old, now + 60, None, None)])             # still running
        c.commit()

    def left():
        return ([r[0] for r in dal._conn.execute("SELECT ts_utc FROM fast_samples")],
                [r[0] for r in dal._conn.execute("SELECT id FROM fast_runs ORDER BY id")])

    fill()
    out = dal.enforce_retention(now, {})                 # absent: a year
    assert left() == ([new], [2, 3]) and out["fast_samples"] == 1 and out["fast_runs"] == 1
    fill()
    dal.enforce_retention(now, {"realtime_days": None})  # null: for ever
    assert left() == ([old, new], [1, 2, 3])
    fill()
    dal.enforce_retention(now, {"realtime_days": 5})     # under the floor: refused
    assert left() == ([old, new], [1, 2, 3])
    fill()
    dal.enforce_retention(now, {"realtime_days": 30})
    assert left() == ([new], [2, 3])
    dal.close()


def test_big_reads_run_four_at_a_time(tmp_path, monkeypatch):
    pytest.importorskip("bacpypes3")
    import threading
    from fastapi.testclient import TestClient
    from hvactrend.api import app as app_module
    from tests.test_realtime import _app
    lock, now_running, most = threading.Lock(), [0], [0]

    def slow(*a):
        with lock:
            now_running[0] += 1
            most[0] = max(most[0], now_running[0])
        time.sleep(0.3)
        with lock:
            now_running[0] -= 1
        return {"series": [], "rollup": "raw", "extent": {}}

    monkeypatch.setattr(app_module, "_series_payload", slow)
    app = _app(tmp_path)
    with TestClient(app) as c:
        def ask():
            assert c.post("/api/series", json={"ids": [1], "start": T0 - 3600,
                                               "end": T0}).status_code == 200
        threads = [threading.Thread(target=ask) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert most[0] == 4
