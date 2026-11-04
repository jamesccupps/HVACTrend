"""Fixes from the 2026-10-27 whole-program review: storage and the stalls on the event loop.

The alarm-delivery fixes from the same review are in test_rules_live.py, and the trend
viewer's in tests/js/viewer.test.mjs. Every test here was run against the original code
and failed (scratchpad mutation runs) before the fix went in.
"""

import time

import pytest

from hvactrend.models import Quality, Sample
from hvactrend.storage.sqlite_dal import SQLiteDAL

# The rollup INSERT's SELECT as it was until 2026-10-27, kept here as the oracle the new,
# point-driven form must agree with row for row.
_OLD_ROLLUP = (
    "SELECT a.point_id, a.b, g.v_min, g.v_max, g.v_avg, l.v_last, COALESCE(g.n, 0), a.n_total "
    "FROM (SELECT point_id, (ts_utc / ?) * ? AS b, COUNT(*) AS n_total FROM samples "
    "      WHERE ts_utc >= ? AND ts_utc < ? GROUP BY point_id, b) a "
    "LEFT JOIN (SELECT point_id, (ts_utc / ?) * ? AS b, MIN(value_num) v_min, MAX(value_num) v_max, "
    "      AVG(value_num) v_avg, COUNT(*) n FROM samples WHERE quality='good' "
    "      AND value_num IS NOT NULL AND ts_utc >= ? AND ts_utc < ? GROUP BY point_id, b) g "
    "ON g.point_id = a.point_id AND g.b = a.b "
    "LEFT JOIN (SELECT point_id, (ts_utc / ?) * ? AS b, value_num AS v_last, MAX(ts_utc) AS mx "
    "      FROM samples WHERE quality='good' AND value_num IS NOT NULL "
    "      AND ts_utc >= ? AND ts_utc < ? GROUP BY point_id, b) l "
    "ON l.point_id = a.point_id AND l.b = a.b ORDER BY 1, 2")

BASE = 3_600_000


def _dal(tmp_path, name="r.db", points=3):
    dal = SQLiteDAL(tmp_path / name)
    dal.apply_schema()
    c = dal._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'DEMO','Demo Building')")
    c.execute("INSERT INTO buildings(id,site_id,code,name) VALUES (1,1,'B1','Main')")
    c.execute("INSERT INTO systems(id,building_id,code,name) VALUES (1,1,'AC-1','AC-1')")
    c.execute("INSERT INTO devices(id,site_id,device_instance,created_utc) VALUES (1,1,1001,0)")
    for pid in range(1, points + 1):
        c.execute("INSERT INTO points(id,device_id,system_id,source,object_type,object_instance,"
                  "name,enabled,created_utc) VALUES (?,1,1,'bacnet','analog-input',?,?,1,0)",
                  (pid, pid, f"P{pid}"))
    return dal


def _mixed(dal, t_from, t_to):
    """Good, faulted, comm-loss and text samples, so every branch of a bucket is exercised:
    a point whose every sample in a bucket faulted, one with none good at all, and so on."""
    rows = []
    for t in range(t_from, t_to, 120):
        i = (t - BASE) // 120
        rows.append(Sample(point_id=1, ts_utc=t, value_num=50.0 + i % 7, quality=Quality.GOOD))
        q = Quality.FAULT if i % 5 == 0 else Quality.GOOD
        rows.append(Sample(point_id=2, ts_utc=t, value_num=float(i % 11), quality=q))
        if (i // 10) % 3 == 0:                                   # stretches of all-faulted
            rows.append(Sample(point_id=3, ts_utc=t, value_num=999.0, quality=Quality.FAULT))
        else:
            rows.append(Sample(point_id=3, ts_utc=t, value_num=None, quality=Quality.COMM_LOSS))
    dal.write_samples(rows)


def _norm(rows):
    return [tuple(round(x, 6) if isinstance(x, float) else x for x in r) for r in rows]


def test_the_point_driven_rollup_matches_the_old_one_row_for_row(tmp_path):
    dal = _dal(tmp_path)
    _mixed(dal, BASE, BASE + 6 * 3600)
    dal.build_rollups(BASE + 6 * 3600)
    for tbl, secs in (("samples_5m", 300), ("samples_1h", 3600)):
        want = dal._conn.execute(_OLD_ROLLUP, (secs, secs, 0, BASE + 6 * 3600) * 3).fetchall()
        got = dal._conn.execute(
            f"SELECT point_id, bucket_utc, v_min, v_max, v_avg, v_last, n, n_total FROM {tbl} "
            "ORDER BY 1, 2").fetchall()
        assert _norm(got) == _norm(want) and len(got) > 10, tbl
    # the all-faulted point still lands, with n = 0 and no value (F-11)
    assert dal._conn.execute("SELECT COUNT(*) FROM samples_1h WHERE point_id = 3 AND n = 0 "
                             "AND n_total > 0 AND v_avg IS NULL").fetchone()[0] > 0


def test_the_pickers_counts_stay_right_as_the_hourly_watermark_moves(tmp_path):
    dal = _dal(tmp_path)
    _mixed(dal, BASE, BASE + 4 * 3600)
    dal.build_rollups(BASE + 4 * 3600)
    dal.logged_points()                                          # primes the carried sums
    _mixed(dal, BASE + 4 * 3600, BASE + 9 * 3600)
    dal.build_rollups(BASE + 9 * 3600)
    seen = []
    dal._conn.set_trace_callback(seen.append)
    got = {p["id"]: p["sample_count"] for p in dal.logged_points()}
    dal._conn.set_trace_callback(None)
    fresh = SQLiteDAL(tmp_path / "r.db")                         # no carried sums
    want = {p["id"]: p["sample_count"] for p in fresh.logged_points()}
    assert got == want and got[1] > 0
    assert not any("GROUP BY point_id" in s and "samples_1h" in s for s in seen), \
        "a later load must add the new buckets, not sum the whole table again"
    # a watermark that moves BACK (a rebuild) recounts
    dal._conn.execute("DELETE FROM samples_1h WHERE bucket_utc >= ?", (BASE + 6 * 3600,))
    fresh2 = SQLiteDAL(tmp_path / "r.db")
    assert {p["id"]: p["sample_count"] for p in dal.logged_points()} == \
        {p["id"]: p["sample_count"] for p in fresh2.logged_points()}


def test_the_extent_still_ends_at_the_newest_sample_and_falls_back_to_the_rollups(tmp_path):
    dal = _dal(tmp_path)
    _mixed(dal, BASE, BASE + 4 * 3600)
    dal.build_rollups(BASE + 4 * 3600)
    assert dal.samples_time_extent() == {"min_ts": BASE, "max_ts": BASE + 4 * 3600 - 120}
    dal._conn.execute("DELETE FROM samples")                     # raw all pruned
    assert dal.samples_time_extent()["max_ts"] == BASE + 3 * 3600 + 3300   # newest 5 m bucket


def test_the_canonical_flags_change_in_one_transaction(tmp_path):
    dal = _dal(tmp_path)
    seen = []
    dal._conn.set_trace_callback(seen.append)
    dal.set_role_canonical([1, 3])
    dal._conn.set_trace_callback(None)
    assert seen[0].upper().startswith("BEGIN")
    assert [r[0] for r in dal._conn.execute(
        "SELECT id FROM points WHERE role_canonical = 1 ORDER BY id")] == [1, 3]


def test_the_rebuild_button_leaves_the_baselines_alone_unless_asked(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from hvactrend.ai import baseline
    from hvactrend.api.app import create_app
    _dal(tmp_path).close()
    calls = []
    monkeypatch.setattr(baseline, "build_baselines",
                        lambda *a, **k: calls.append(1) or {"points": 0})

    def app_with(extra):
        cfg = tmp_path / "c.yaml"
        cfg.write_text("site: {code: DEMO, name: Demo Building}\n"
                       "bacnet:\n  local_address: '<COLLECTOR_IP>/24'\n"
                       "  device_instance: '<LOGGER_DEVICE_INSTANCE>'\n"
                       "  poll: {interval_s: 120}\n"
                       "  safety: {allow_writes: false, allow_cov: false}\n"
                       f"storage: {{path: '{(tmp_path / 'r.db').as_posix()}'}}\n" + extra,
                       encoding="utf-8")
        return TestClient(create_app(str(cfg), enable_bacnet=False))

    with app_with("") as c:
        d = c.post("/api/model/rebuild").json()
        assert d["status"] == "ok" and d["baselines"] is None and calls == []
    with app_with("ai: {baselines: true}\n") as c:
        c.post("/api/model/rebuild")
        assert calls == [1]


def test_the_priority_read_keeps_to_its_interval(monkeypatch):
    """Checked once per 120 s poll cycle, "now + 600" slipped to 720 s half the time."""
    import asyncio

    from hvactrend.collector import service
    col = service.Collector(None, None, {"bacnet": {"poll": {"priority_interval_s": 600}}})
    reads = []

    class _D:
        def command_targets(self, types):
            reads.append(clock[0])
            return []
    col._dal = _D()
    clock = [0.0]
    monkeypatch.setattr(service.time, "monotonic", lambda: clock[0])
    for k in range(61):                       # two hours of cycles, each a few s late or not
        clock[0] = 1000 + 120 * k + (3 if k % 2 == 0 else 0)
        asyncio.run(col._maybe_read_priorities())
    gaps = [b - a for a, b in zip(reads, reads[1:])]
    assert reads[0] == 1003
    assert sum(gaps) / len(gaps) < 650, gaps                     # "now + 600": all 720


def test_retention_does_not_count_the_whole_table_when_nothing_is_due(tmp_path):
    dal = _dal(tmp_path)
    _mixed(dal, BASE, BASE + 3600)
    seen = []
    dal._conn.set_trace_callback(seen.append)
    dal.enforce_retention(BASE + 3600, {"raw_days": 730})
    assert "SELECT COUNT(*) FROM samples" not in [s.strip() for s in seen]
    assert not any(s.startswith("DELETE FROM samples ") for s in seen)


def test_a_big_prune_is_still_refused(tmp_path):
    """The guard F-05 put in still sees its denominator when it matters."""
    dal = _dal(tmp_path, points=1)
    dal.MIN_PRUNE_GUARD_ROWS = 10
    dal.write_samples([Sample(point_id=1, ts_utc=BASE + i * 120, value_num=1.0,
                              quality=Quality.GOOD) for i in range(100)])
    out = dal.enforce_retention(BASE + 100 * 120 + 8 * 86400, {"raw_days": 7})
    assert "samples" not in out                                  # refused: nothing deleted
    assert dal._conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 100
    out = dal.enforce_retention(BASE + 100 * 120 + 8 * 86400,
                                {"raw_days": 7, "allow_large_prune": True})
    assert out["samples"] == 100

# ── the API (2026-10-27 review) ──────────────────────────────────────────────────────────
def _app(tmp_path, extra=""):
    from hvactrend.api.app import create_app
    db = tmp_path / "a.db"
    if not db.exists():
        d = _dal(tmp_path, "a.db")
        _mixed(d, BASE, BASE + 6 * 3600)
        d.close()
    cfg = tmp_path / "a.yaml"
    cfg.write_text("site: {code: DEMO, name: Demo Building}\n"
                   "bacnet:\n  local_address: '<COLLECTOR_IP>/24'\n"
                   "  device_instance: '<LOGGER_DEVICE_INSTANCE>'\n"
                   "  poll: {interval_s: 120}\n"
                   "  safety: {allow_writes: false, allow_cov: false}\n"
                   f"storage: {{path: '{db.as_posix()}'}}\n" + extra, encoding="utf-8")
    return create_app(str(cfg), enable_bacnet=False)


def _where(record):
    import asyncio

    def wrap(fn):
        def spy(*a, **k):
            try:
                asyncio.get_running_loop()
                record.append("on the event loop")
            except RuntimeError:
                record.append("worker thread")
            return fn(*a, **k)
        return spy
    return wrap


def test_chart_data_and_schedules_are_read_off_the_event_loop(tmp_path, monkeypatch):
    """64 points over 14 days at 5 m was 1.2 s on the loop the collector polls from, and the
    schedule detection 0.9-7 s; polling and /health stood still for them."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from hvactrend.api import app as app_module
    where: list = []
    monkeypatch.setattr(app_module, "_series_payload", _where(where)(app_module._series_payload))
    monkeypatch.setattr(app_module, "_schedule_detect_ro",
                        _where(where)(app_module._schedule_detect_ro))
    with TestClient(_app(tmp_path)) as c:
        r = c.post("/api/series", json={"ids": [1, 2], "start": BASE, "end": BASE + 6 * 3600})
        assert r.status_code == 200 and len(r.json()["series"]) == 2
        assert c.get("/api/points/1/series",
                     params={"start": BASE, "end": BASE + 3600}).status_code == 200
        assert c.get("/api/systems/schedule-detect", params={"days": 14}).status_code == 200
    assert where == ["worker thread"] * 3


def test_a_window_across_the_5m_retention_edge_has_no_false_hole(tmp_path):
    """rollup_5m_days (365) is shorter than raw_days (730): the part of a window before the
    5-minute table's edge was blank, drawn as a collector outage, while raw still held it."""
    dal = _dal(tmp_path)
    _mixed(dal, BASE, BASE + 6 * 3600)
    dal.build_rollups(BASE + 6 * 3600)
    whole = dal.rollup_series(1, BASE, BASE + 6 * 3600, "5m")
    dal._conn.execute("DELETE FROM samples_5m WHERE bucket_utc < ?", (BASE + 3 * 3600,))
    after = dal.rollup_series(1, BASE, BASE + 6 * 3600, "5m")
    assert _norm([tuple(r[k] for k in ("bucket_utc", "v_min", "v_max", "v_avg", "v_last", "n"))
                  for r in after]) == \
        _norm([tuple(r[k] for k in ("bucket_utc", "v_min", "v_max", "v_avg", "v_last", "n"))
               for r in whole])


def test_numbers_out_of_any_range_are_answered_not_500(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    with TestClient(_app(tmp_path), raise_server_exceptions=False) as c:
        assert c.get("/api/weather", params={"start": 0, "end": 2 ** 70}).status_code == 200
        assert c.get("/api/annotations", params={"start": 0, "end": 2 ** 70}).status_code == 200
        r = c.post("/api/series", content=b'{"ids": [1], "start": 0, "end": Infinity}',
                   headers={"content-type": "application/json"})
        assert r.status_code == 400
        assert c.post("/api/series", json={"ids": [1], "start": 0, "end": 2 ** 70}).status_code == 200
        r = c.post("/api/export", json={"ids": [], "weather": True, "start": -10 ** 12,
                                        "end": 10 ** 13, "resolution": "1h", "layout": "one"})
        assert r.status_code in (200, 400), r.text


def test_a_setting_must_be_of_the_kind_it_replaces(tmp_path):
    """A string where a number was meant was stored, then stopped the collector at the next
    restart (reported as a BACnet failure). A key that names a file is not settable here."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    extra = ("weather: {refresh_interval_s: 3600, enabled: false}\n"
             "ai: {prompts: {daily: config/prompts/daily.md}}\n")
    with TestClient(_app(tmp_path, extra)) as c:
        r = c.put("/api/settings", json={"settings": {
            "weather.refresh_interval_s": "abc", "weather.enabled": "yes",
            "ai.prompts.daily": "config/secrets.env"}}).json()
        assert r["applied"] == [] and set(r["rejected"]) == {
            "weather.refresh_interval_s", "weather.enabled", "ai.prompts.daily"}
        assert r["why"]["weather.refresh_interval_s"] == "must be a number"
        r = c.put("/api/settings", json={"settings": {"weather.refresh_interval_s": 1800,
                                                      "weather.enabled": True}}).json()
        assert sorted(r["applied"]) == ["weather.enabled", "weather.refresh_interval_s"]
        assert "ai.prompts.daily" not in c.get("/api/settings").json()["effective"]


def test_a_bad_value_stored_before_the_check_is_not_applied(tmp_path):
    from hvactrend.config import apply_overrides, load_config
    cfg = tmp_path / "c.yaml"
    cfg.write_text("site: {code: DEMO, name: Demo Building}\n"
                   "bacnet:\n  local_address: '<COLLECTOR_IP>/24'\n"
                   "  device_instance: '<LOGGER_DEVICE_INSTANCE>'\n"
                   "  safety: {allow_writes: false, allow_cov: false}\n"
                   f"storage: {{path: '{(tmp_path / 'c.db').as_posix()}'}}\n"
                   "weather: {refresh_interval_s: 3600}\n", encoding="utf-8")
    c = load_config(str(cfg))
    assert apply_overrides(c, {"weather.refresh_interval_s": "abc"}) == []
    assert apply_overrides(c, {"weather.refresh_interval_s": 900}) == ["weather.refresh_interval_s"]


@pytest.mark.parametrize("value", ["abc\u2028HVACTREND_NTFY_TOPIC=evil", "a\x0bb", "a\x85b", "x" * 5000])
def test_a_secret_is_one_short_printable_line(tmp_path, value):
    """Reading the file back splits on every character str.splitlines() breaks on, and a
    value past Windows' environment limit was saved and then broke every watchdog run."""
    from hvactrend import secrets_store
    f = tmp_path / "s.env"
    secrets_store.set_secret("ntfy_topic", "occ-good-topic", path=f)
    before = f.read_bytes()
    with pytest.raises(ValueError):
        secrets_store.set_secret("ntfy_topic", value, path=f)
    assert f.read_bytes() == before


def test_an_over_long_secret_already_in_the_file_is_skipped_not_fatal(tmp_path, monkeypatch):
    from hvactrend import secrets_store
    f = tmp_path / "s.env"
    f.write_text("HVACTREND_NTFY_TOPIC=" + "x" * 40000 + "\n", encoding="utf-8")
    monkeypatch.delenv("HVACTREND_NTFY_TOPIC", raising=False)
    assert secrets_store.load_into_env(f) == []

# ── the collector (2026-10-27 review) ────────────────────────────────────────────────────
class _App:
    """A read-only application stub: the second RPM chunk gets a reply bacpypes3 returns as
    None (not an RPM-ACK); single reads answer."""
    def __init__(self, raise_instead=False):
        self.calls = 0
        self.raise_instead = raise_instead

    async def read_property_multiple(self, address, params):
        self.calls += 1
        if self.calls == 2:
            if self.raise_instead:
                raise ValueError("cast_out: could not decode")
            return None
        oids = params[0::2]
        return [(oid, "present-value", None, 55.0) for oid in oids]

    async def read_property(self, address, oid, prop):
        return 56.0 if prop == "present-value" else None


def _pts(n):
    return [{"point_id": i, "object_type": "analog-input", "object_instance": i}
            for i in range(1, n + 1)]


@pytest.mark.parametrize("raise_instead", [False, True])
def test_one_bad_rpm_reply_does_not_cost_the_whole_device(raise_instead):
    import asyncio

    from hvactrend.collector import service
    col = service.Collector(_App(raise_instead), None,
                            {"bacnet": {"poll": {"rpm_batch": 10, "device_budget_s": 60}}})
    samples, health = asyncio.run(col._poll_device("192.0.2.9", 7, _pts(25), BASE))
    assert len(samples) == 25 and health.reads_ok == 25
    assert [s.value_num for s in samples[:10]] == [55.0] * 10           # chunk 1 kept
    assert [s.value_num for s in samples[10:20]] == [56.0] * 10         # chunk 2 per object
    assert health.method == "single"


def test_samples_a_locked_database_refused_wait_for_the_next_cycle(tmp_path):
    import sqlite3

    from hvactrend.collector import service
    dal = _dal(tmp_path, points=2)
    col = service.Collector(None, dal, {})
    real = dal.write_samples
    fail = [True]

    def flaky(rows):
        if fail[0]:
            raise sqlite3.OperationalError("database is locked")
        return real(rows)
    dal.write_samples = flaky
    first = [Sample(point_id=1, ts_utc=BASE, value_num=1.0, quality=Quality.GOOD)]
    assert col._store(first, 1) == 0
    fail[0] = False
    second = [Sample(point_id=2, ts_utc=BASE + 120, value_num=2.0, quality=Quality.GOOD)]
    assert col._store(second, 2) == 2
    assert dal._conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 2


def test_a_report_attempt_that_raises_is_not_retried_every_cycle(tmp_path, monkeypatch):
    import asyncio

    from hvactrend.collector import service
    dal = _dal(tmp_path)
    col = service.Collector(None, dal, {"ai": {"enabled": True,
                                               "schedule": {"daily_time": "06:00"}}})
    tries = []

    async def boom(kind):
        tries.append(kind)
        raise RuntimeError("insert_report: database is locked")
    monkeypatch.setattr(col, "_run_report", boom)
    day = int(time.mktime((2026, 10, 27, 6, 0, 0, 0, 0, -1)))

    async def cycles():
        for k in range(120):                                     # four hours of cycles
            monkeypatch.setattr(service.time, "time", lambda k=k: day + 60 + 120 * k)
            col._maybe_generate_report()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
    asyncio.run(cycles())
    assert tries == ["daily"] * 3                                # 3 tries, 30 min apart
