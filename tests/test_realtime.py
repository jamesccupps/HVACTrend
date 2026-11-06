"""Realtime trends (collector/realtime.py, api /realtime): chosen points read every few
seconds for a set time, each for its own time if wanted, under caps, kept apart from the
2-minute record, started and stopped from the VLAN viewer too (the operator, 2026-11-06)."""

import asyncio
import csv
import io

import pytest

from hvactrend.collector import realtime
from hvactrend.collector.service import Collector
from hvactrend.storage.sqlite_dal import SQLiteDAL

T = 1_790_000_003            # 3 s past a 10-s mark: the first read is off the grid
LIM = dict(realtime.DEFAULTS)


def _plant(path):
    dal = SQLiteDAL(path)
    dal.apply_schema()
    c = dal._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'DEMO','Demo')")
    c.execute("INSERT INTO buildings(id,site_id,code,name) VALUES (1,1,'MAIN','Main')")
    c.execute("INSERT INTO systems(id,building_id,code,name,kind) VALUES (1,1,'AHU-1','AHU-1','ahu')")
    for did, addr in ((1, "192.0.2.10"), (2, "192.0.2.20")):
        c.execute("INSERT INTO devices(id,site_id,device_instance,address,name,rpm_supported,"
                  "created_utc) VALUES (?,1,?,?,?,1,0)", (did, 100 * did, addr, f"PXC{did}"))
    pts = [  # id, device, type, instance, name, display, units, logged
        (1, 1, "analog-input", 1, "AHU1.DAT", "Discharge air", "degrees-fahrenheit", 1),
        (2, 1, "analog-input", 2, "AHU1.DUCT.SP", None, "inches-of-water", 1),
        (3, 2, "binary-value", 3, "AHU1.SF.STS", "Supply fan", None, 0),
        (4, 1, "trend-log", 9, "AHU1.DAT.TL", None, None, 0),
        (5, 1, "analog-value", 5, "=HYPERLINK(x)", None, "percent", 1),
    ]
    for pid, did, ot, inst, name, shown, units, logged in pts:
        c.execute("INSERT INTO points(id,device_id,system_id,source,object_type,object_instance,"
                  "name,display_name,units_text,enabled,created_utc) "
                  "VALUES (?,?,1,'bacnet',?,?,?,?,?,?,0)",
                  (pid, did, ot, inst, name, shown, units, logged))
    c.commit()
    return dal


# ── the caps ──────────────────────────────────────────────────────────────────────────────
def test_the_caps_are_checked_in_plain_words(tmp_path):
    dal = _plant(tmp_path / "c.db")
    readable = dal.fast_points([1, 2, 3, 4, 5])
    assert set(readable) == {1, 2, 3, 5}                    # a trend log has no present value
    ok, err = realtime.plan_start([], [{"id": 1, "minutes": 60, "interval_s": 5},
                                       {"id": 3, "minutes": 5, "interval_s": 10}],
                                  readable, LIM, T)
    assert err is None and ok == [(1, 5, T + 3600), (3, 10, T + 300)]   # each its own time
    _, err = realtime.plan_start([], [{"id": 4, "minutes": 5, "interval_s": 5}], readable, LIM, T)
    assert "can't be read live" in err
    _, err = realtime.plan_start([], [{"id": 1, "minutes": 5, "interval_s": 2}], readable, LIM, T)
    assert "5, 10, 15, 30, 60" in err
    _, err = realtime.plan_start([], [{"id": 1, "minutes": 24 * 60 + 1, "interval_s": 5}],
                                 readable, LIM, T)
    assert "24 hours" in err
    small = {**LIM, "max_points": 2, "max_per_controller": 1}
    running = [{"point_id": 1, "device_id": 1}]
    _, err = realtime.plan_start(running, [{"id": 3, "minutes": 5, "interval_s": 5},
                                           {"id": 5, "minutes": 5, "interval_s": 5}],
                                 readable, small, T)
    assert "3 points running; the limit is 2" in err
    _, err = realtime.plan_start(running, [{"id": 2, "minutes": 5, "interval_s": 5}],
                                 readable, small, T)
    assert "2 points on one controller (PXC1)" in err
    # a running point started again is not counted twice
    ok, err = realtime.plan_start(running, [{"id": 1, "minutes": 5, "interval_s": 10}],
                                  readable, small, T)
    assert err is None and ok == [(1, 10, T + 300)]
    dal.close()


def test_limits_come_from_config_over_the_defaults():
    lim = realtime.limits({"realtime": {"max_points": 8, "intervals_s": [30, 5, 5]}})
    assert lim["max_points"] == 8 and lim["intervals_s"] == [5, 30]
    assert lim["max_per_controller"] == 10 and lim["max_hours"] == 24
    assert realtime.limits(None) == realtime.DEFAULTS


# ── the runs in the database ──────────────────────────────────────────────────────────────
def test_runs_start_change_stop_and_finish(tmp_path):
    dal = _plant(tmp_path / "r.db")
    g1 = dal.start_fast_runs([(1, 5, T + 3600), (3, 10, T + 300)], T)
    assert {r["point_id"]: r["group_id"] for r in dal.fast_runs_open()} == {1: g1, 3: g1}
    g2 = dal.start_fast_runs([(1, 15, T + 60), (2, 5, T + 600)], T + 10)  # 1 again: new end
    runs = {r["point_id"]: r for r in dal.fast_runs_open()}
    assert len(runs) == 3 and runs[1]["interval_s"] == 15 and runs[1]["ends_utc"] == T + 60
    assert runs[1]["group_id"] == g1 and runs[2]["group_id"] == g2
    assert dal.finish_fast_runs(T + 60) == 1                 # point 1's time is up
    assert dal.stop_fast_runs([runs[3]["run_id"]], T + 70) == 1
    assert [r["point_id"] for r in dal.fast_runs_open()] == [2]
    assert dal.stop_fast_runs(None, T + 80) == 1             # stop all
    assert dal.fast_runs_open() == []
    hist = dal.fast_history()
    assert [g["group_id"] for g in hist] == [g2, g1]
    reasons = {r["point_id"]: (r["stop_reason"], r["stopped_utc"]) for r in hist[1]["runs"]}
    assert reasons == {1: ("finished", T + 60), 3: ("stopped", T + 70)}
    dal.close()


def test_search_finds_readable_points_logged_first(tmp_path):
    dal = _plant(tmp_path / "s.db")
    got = dal.search_fast_points("ahu1")
    assert [p["point_id"] for p in got][:2] == [1, 2] and 4 not in [p["point_id"] for p in got]
    assert [p["point_id"] for p in dal.search_fast_points("supply fan")] == [3]
    dal.close()


# ── the reader ────────────────────────────────────────────────────────────────────────────
def _silence():
    from bacpypes3.apdu import AbortPDU, AbortReason
    return AbortPDU(reason=AbortReason.noResponse)


class _App:
    def __init__(self, silent=(), gate=None):
        self.calls, self.silent, self.gate = [], set(silent), gate

    async def read_property_multiple(self, address, params):
        self.calls.append(address)
        if self.gate is not None:
            await self.gate.wait()
        if address in self.silent:
            return _silence()
        return [(oid, prop, None, 1.0 if prop == "present-value" else None)
                for oid, props in zip(params[0::2], params[1::2]) for prop in props]

    async def read_property(self, address, oid, prop):
        return _silence() if address in self.silent else 1.0


def _drive(col, seconds, start=T):
    async def go():
        for now in range(start, start + seconds):
            await col.realtime.tick(now)
            await asyncio.gather(*list(col.realtime._tasks))
    asyncio.run(go())


def _stamps(dal, pid):
    return [r[0] for r in dal._conn.execute(
        "SELECT ts_utc FROM fast_samples WHERE point_id = ? ORDER BY ts_utc", (pid,))]


def test_each_point_is_read_on_its_own_grid_until_its_own_end(tmp_path):
    dal = _plant(tmp_path / "g.db")
    col = Collector(_App(), dal, {"bacnet": {"poll": {"interval_s": 120}}})
    dal.start_fast_runs([(1, 5, T + 3600), (2, 10, T + 60), (3, 5, T + 3600)], T)
    _drive(col, 71)
    s1 = _stamps(dal, 1)                     # at once, then on the :05 grid
    assert s1[0] == T and s1[1] == T + 2 and all(b - a == 5 for a, b in zip(s1[1:], s1[2:]))
    assert s1[-1] == T + 67 and all(t % 5 == 0 for t in s1[1:])
    s2 = _stamps(dal, 2)                                     # 10 s, and ends at T+60
    assert s2 == [T] + list(range(T + 7, T + 60, 10))
    assert {r["point_id"] for r in dal.fast_runs_open()} == {1, 3}
    # points 1 and 2 share a controller: one grouped read per slot, not one per point
    assert len(col._app.calls) == len(set(s1) | set(s2)) + len(_stamps(dal, 3))
    # the 2-minute record is untouched
    assert dal._conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 0
    dal.close()


def test_a_silent_controller_ends_its_runs_and_spares_the_others(tmp_path):
    pytest.importorskip("bacpypes3")
    dal = _plant(tmp_path / "x.db")
    col = Collector(_App(silent={"192.0.2.20"}), dal, {})
    dal.start_fast_runs([(1, 5, T + 3600), (3, 5, T + 3600)], T)
    _drive(col, 16)
    hist = {r["point_id"]: r for r in dal.fast_history()[0]["runs"]}
    # tries at T, then on the grid at T+2 and T+7 (the :05 and :10 slots)
    assert hist[3]["stop_reason"] == "no answer" and hist[3]["stopped_utc"] == T + 7
    assert hist[1]["stopped_utc"] is None
    assert col._app.calls.count("192.0.2.20") == 3            # three tries, then left alone
    q = dal._conn.execute("SELECT DISTINCT quality FROM fast_samples WHERE point_id = 3")
    assert [r[0] for r in q] == ["comm-loss"]
    dal.close()


def test_a_controller_the_collector_found_dark_is_not_asked(tmp_path):
    dal = _plant(tmp_path / "d.db")
    col = Collector(_App(), dal, {})
    col._dark["192.0.2.20"] = [1, 4]
    dal.start_fast_runs([(3, 5, T + 3600)], T)
    _drive(col, 16)
    assert col._app.calls == []
    assert dal.fast_history()[0]["runs"][0]["stop_reason"] == "no answer"
    dal.close()


def test_a_slow_controller_is_not_asked_again_until_it_answers(tmp_path):
    dal = _plant(tmp_path / "b.db")
    gate = asyncio.Event()
    col = Collector(_App(gate=gate), dal, {})
    dal.start_fast_runs([(1, 5, T + 3600)], T)

    async def go():
        await col.realtime.tick(T)
        await asyncio.sleep(0)
        await col.realtime.tick(T + 2)                       # its next slot: still reading
        await asyncio.sleep(0)
        assert col._app.calls == ["192.0.2.10"]
        gate.set()
        await asyncio.gather(*list(col.realtime._tasks))
        await col.realtime.tick(T + 7)
        await asyncio.gather(*list(col.realtime._tasks))
    asyncio.run(go())
    assert col._app.calls == ["192.0.2.10"] * 2 and _stamps(dal, 1) == [T, T + 7]
    dal.close()


def test_thinning_keeps_the_spike_and_the_gaps():
    rows = [(t, 50.0, "good") for t in range(0, 50000, 5)]
    rows[4000] = (rows[4000][0], 99.0, "good")
    rows[6000] = (rows[6000][0], None, "comm-loss")
    out = realtime.thin(rows, 1000)
    assert len(out) <= 1100 and any(r[1] == 99.0 for r in out)
    assert any(r[1] is None for r in out)
    assert [r[0] for r in out] == sorted(r[0] for r in out)
    assert realtime.thin(rows[:10]) == rows[:10]


# ── the API, here and from elsewhere on the VLAN ──────────────────────────────────────────
def _app(tmp_path, extra=""):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    db = tmp_path / "a.db"
    _plant(db).close()
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "site: {code: S, name: S}\n"
        "bacnet:\n  local_address: '<COLLECTOR_IP>/24'\n"
        "  device_instance: '<LOGGER_DEVICE_INSTANCE>'\n  poll: {interval_s: 120}\n"
        "  safety: {allow_writes: false, allow_cov: false}\n"
        f"storage: {{path: '{db.as_posix()}'}}\n" + extra, encoding="utf-8")
    from hvactrend.api.app import create_app
    return create_app(str(cfg), enable_bacnet=False)


def _sql(tmp_path, sql, args=()):
    """On a connection of the test's own: the app's belongs to its server thread."""
    dal = SQLiteDAL(tmp_path / "a.db")
    dal._conn.execute(sql, args)
    dal.close()


def _insert(tmp_path, values, args):
    """Readings written as the reader would."""
    _sql(tmp_path, "INSERT INTO fast_samples VALUES " + values, args)


class _Col:
    def __init__(self):
        self.pokes = 0
        self.realtime = self

    def poke(self):
        self.pokes += 1

    def stop(self):
        pass


def test_start_stop_and_read_through_the_api(tmp_path):
    from fastapi.testclient import TestClient
    app = _app(tmp_path, "realtime: {max_points: 3, max_per_controller: 2}\n")
    with TestClient(app) as c:
        r = c.post("/api/realtime/start", json={"points": [{"id": 1, "minutes": 5,
                                                            "interval_s": 5}]})
        assert r.status_code == 503 and "BACnet" in r.json()["detail"]
        assert c.get("/api/realtime").json()["reading"] is False
        app.state.collector = col = _Col()
        r = c.post("/api/realtime/start", json={"points": [
            {"id": 1, "minutes": 60, "interval_s": 5}, {"id": 3, "minutes": 5, "interval_s": 10}]})
        assert r.status_code == 200, r.text
        st = r.json()
        assert col.pokes == 1 and st["limits"]["max_points"] == 3
        run = {p["id"]: p for p in st["running"]}
        assert run[1]["label"] == "AHU-1 Discharge air" and run[3]["interval_s"] == 10
        assert run[3]["ends_utc"] - run[3]["started_utc"] == 300 and run[3]["logged"] is False
        r = c.post("/api/realtime/start", json={"points": [{"id": 2, "minutes": 5, "interval_s": 5},
                                                           {"id": 5, "minutes": 5, "interval_s": 5}]})
        assert r.status_code == 409 and "limit is 3" in r.json()["detail"]
        assert c.post("/api/realtime/start", json={"points": [{"id": 4, "minutes": 5,
                                                               "interval_s": 5}]}).status_code == 409
        assert c.post("/api/realtime/start", json={"points": [{"id": "x"}]}).status_code == 400
        assert c.post("/api/realtime/start", json={"points": []}).status_code == 400

        now = st["now"]
        _insert(tmp_path, "(1, ?, 55.5, NULL, 'good'), (1, ?, NULL, NULL, 'comm-loss'), "
                          "(1, ?, 56.0, NULL, 'good')", (now - 10, now - 5, now))
        d = c.get(f"/api/realtime/data?ids=1,3&start={now - 60}&end={now}").json()
        assert d["series"]["1"] == [[now - 10, 55.5, "good"], [now - 5, None, "comm-loss"],
                                    [now, 56.0, "good"]]
        assert d["series"]["3"] == [] and d["meta"]["1"]["units"] == "degrees-fahrenheit"
        d = c.get(f"/api/realtime/data?ids=1&since={now - 6}").json()
        assert [r[0] for r in d["series"]["1"]] == [now - 5, now]
        assert c.get("/api/realtime/data?ids=1").status_code == 400
        assert c.get("/api/realtime").json()["running"][0]["last"]["v"] == 56.0

        r = c.post("/api/realtime/stop", json={"run_ids": [run[3]["run_id"]]})
        assert r.json()["stopped"] == 1 and [p["id"] for p in r.json()["running"]] == [1]
        r = c.post("/api/realtime/stop", json={"all": True})
        assert r.json()["stopped"] == 1 and r.json()["running"] == []
        (g,) = c.get("/api/realtime/history").json()["sessions"]
        assert [x["label"] for x in g["runs"]] == ["AHU-1 Discharge air", "AHU-1 Supply fan"]


def test_a_session_exports_as_csv_like_the_other_data(tmp_path):
    from fastapi.testclient import TestClient
    app = _app(tmp_path)
    with TestClient(app) as c:
        app.state.collector = _Col()
        st = c.post("/api/realtime/start", json={"points": [
            {"id": 1, "minutes": 5, "interval_s": 5}, {"id": 5, "minutes": 5, "interval_s": 10}]}).json()
        _sql(tmp_path, "UPDATE fast_runs SET started_utc = started_utc - 60")   # a minute ago
        t0 = st["running"][0]["started_utc"] - 60
        _insert(tmp_path, "(1, ?, 55.5, NULL, 'good'), (1, ?, 55.7, NULL, 'good'), "
                          "(5, ?, 40.0, NULL, 'good')", (t0, t0 + 5, t0))
        c.post("/api/realtime/stop", json={"all": True})
        g = c.get("/api/realtime/history").json()["sessions"][0]["group_id"]
        r = c.get(f"/api/realtime/export?group={g}")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
        assert "attachment" in r.headers["content-disposition"]
        rows = list(csv.reader(io.StringIO(r.text)))
        assert rows[0][:4] == ["timestamp_iso", "timestamp_utc", "timestamp_local", "resolution"]
        assert rows[0][4:] == ["Discharge air [AHU-1 AHU1.DAT]",
                               "Discharge air [AHU-1 AHU1.DAT] quality",
                               "AHU-1 =HYPERLINK(x)", "AHU-1 =HYPERLINK(x) quality"]
        assert [r[1] for r in rows[1:]] == [str(t0), str(t0 + 5)]
        assert rows[1][3:] == ["realtime", "55.5", "good", "40.0", "good"]
        assert rows[2][6:] == ["", ""]                       # the 10-s point between its reads
        assert c.get("/api/realtime/export?group=999").status_code == 404


def test_the_vlan_viewer_can_open_start_and_stop_realtime(tmp_path):
    from fastapi.testclient import TestClient
    app = _app(tmp_path)
    with TestClient(app, client=("192.0.2.77", 51234)) as rc:
        r = rc.get("/realtime")
        assert r.status_code == 200 and 'class="viewer-only"' in r.text
        for url in ("/api/realtime", "/api/realtime/points?q=ahu", "/api/realtime/history",
                    "/api/realtime/data?ids=1&since=0"):
            assert rc.get(url).status_code == 200, url
        # it reaches the endpoint (no BACnet here, so 503), not the 404 of a page not offered
        assert rc.post("/api/realtime/start", json={"points": [{"id": 1, "minutes": 5,
                                                                "interval_s": 5}]}).status_code == 503
        assert rc.post("/api/realtime/stop", json={"all": True}).status_code == 200
        assert rc.get("/dashboard").status_code == 404
        trends = rc.get("/trends").text
        assert 'href="/realtime" class="vo"' in trends and ":not(.vo)" in trends
