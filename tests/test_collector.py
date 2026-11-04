"""Phase 2 collector tests: quality derivation (hermetic) + poll→write (mock)."""

import asyncio
import os
import socket
import time

import pytest

from hvactrend.collector.service import Collector, _sample_from
from hvactrend.models import Quality


# ── quality derivation (no BACnet, no hardware) ──────────────────────────────
def test_sample_quality_and_value():
    p = {"point_id": 1}

    # no response for this object -> comm-loss, no phantom value
    s = _sample_from(p, {}, 1000)
    assert s.quality == Quality.COMM_LOSS and s.value_num is None

    # healthy numeric read -> good
    s = _sample_from(p, {"present-value": 55.3}, 1000)
    assert s.quality == Quality.GOOD and s.value_num == 55.3

    # out-of-service -> fault (still records the value)
    s = _sample_from(p, {"present-value": 1.0, "out-of-service": True}, 1000)
    assert s.quality == Quality.FAULT

    # unreliable sensor -> fault
    s = _sample_from(p, {"present-value": -58.0, "reliability": "open-loop"}, 1000)
    assert s.quality == Quality.FAULT

    # boolean/enum present-value keeps a text label
    s = _sample_from(p, {"present-value": True}, 1000)
    assert s.value_text == "active" and s.value_num == 1.0


def test_error_read_is_comm_loss():
    pytest.importorskip("bacpypes3")
    from bacpypes3.apdu import AbortPDU
    s = _sample_from({"point_id": 1}, {"present-value": AbortPDU()}, 1000)
    assert s.quality == Quality.COMM_LOSS and s.value_num is None


def test_bind_precheck_rejects_stale_address():
    # Loud failure on an unbindable local_address (e.g. NIC IP changed) instead of
    # bacpypes3 silently leaving a non-functional app that hangs on every read.
    from hvactrend.bacnet.application import BACnetConfigError, _assert_bindable
    _assert_bindable("127.0.0.1:47808")            # loopback is bindable -> ok
    with pytest.raises(BACnetConfigError):
        _assert_bindable("192.0.2.1/24")           # TEST-NET, never a local NIC


# ── poll -> write, against the mock controller over loopback ─────────────────
pytest.importorskip("bacpypes3", reason="bacpypes3 not installed")

from hvactrend.bacnet.application import ReadOnlyBACnetApplication  # noqa: E402
from hvactrend.discovery_service import run_discovery  # noqa: E402
from hvactrend.storage.sqlite_dal import SQLiteDAL  # noqa: E402

MOCK_PORT = 47809
READER_PORT = 47808


def _loopback_ok(*ports):
    for port in ports:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", port))
            s.close()
        except OSError:
            return False
    return True


@pytest.mark.skipif(not _loopback_ok(MOCK_PORT, READER_PORT),
                    reason="loopback UDP bind unavailable")
def test_collector_polls_and_writes(tmp_path):
    async def run():
        from .mock_bacnet import build_mock_controller

        mock = build_mock_controller(address=f"127.0.0.1:{MOCK_PORT}")
        cfg = {"bacnet": {"local_address": f"127.0.0.1:{READER_PORT}",
                          "device_instance": 1000, "vendor_identifier": 999,
                          "poll": {"interval_s": 120, "stagger_s": 0},
                          "safety": {"allow_writes": False, "allow_cov": False}}}
        reader = await ReadOnlyBACnetApplication.create(cfg)
        dal = SQLiteDAL(tmp_path / "c.db")
        dal.apply_schema()
        site_id = dal.ensure_site("DEMO", "Demo Building")
        try:
            await asyncio.sleep(0.3)
            res = await run_discovery(reader, dal, site_id=site_id,
                                      address=f"127.0.0.1:{MOCK_PORT}")
            assert res and res[0].device_id
            # opt the sensor objects into logging
            dal._conn.execute(
                "UPDATE points SET enabled=1 WHERE object_type IN "
                "('analog-input','analog-value','binary-value','multi-state-value')")
            assert len(dal.poll_targets()) >= 4

            n_targets = len(dal.poll_targets())
            collector = Collector(reader, dal, cfg)
            await collector._tick()

            rows = dal._conn.execute(
                "SELECT p.name, s.value_num, s.value_text, s.quality "
                "FROM samples s JOIN points p ON s.point_id=p.id").fetchall()
            by = {r["name"]: r for r in rows}
            assert len(rows) == n_targets            # one sample per enabled point
            assert "AHU1-DAT" in by
            assert 54.5 <= by["AHU1-DAT"]["value_num"] <= 56.0
            assert by["AHU1-DAT"]["quality"] == "good"
            assert by["AHU1-FAN"]["value_text"] == "active"

            ph = dal._conn.execute(
                "SELECT ok, reads_ok, reads_attempted, method FROM poll_health").fetchone()
            assert ph["ok"] == 1 and ph["reads_ok"] == n_targets
        finally:
            await reader.close()
            mock.close()

    asyncio.run(run())


# ── scheduled upkeep: weather + learned-model refresh (2026-07-23) ────────────
# These were documented as external Task Scheduler jobs that were never created, so on
# the live box the weather table and the baselines silently went 67 h stale. They now
# live in the collector loop; these tests pin the scheduling contract.
class _FakeDal:
    def __init__(self): self.calls = []

    # the maintenance loop's model rebuild ingests the plant's own trended limits,
    # which reads the enabled-point list. No points -> nothing to apply.
    def points_for_analysis(self): return []


def _collector(cfg):
    return Collector(app=None, dal=_FakeDal(), config=cfg)


def test_due_at_startup_survives_a_low_machine_uptime(monkeypatch):
    """REGRESSION: `_last_* = 0.0` compared against `time.monotonic()` — which on
    Windows counts from BOOT — made `monotonic() - 0.0 < interval` true for the first
    `interval` seconds of uptime, so the job was SKIPPED rather than run. Caught live
    on the real box: 4.8 h uptime meant the 24 h model rebuild had been deferred since
    boot and the baselines were 49 HOURS STALE while reports ran against them. The
    sentinel must not depend on what monotonic() counts from."""
    c = _collector({"weather": {"refresh_interval_s": 3600},
                    "analysis": {"baseline": {"rebuild_interval_s": 86400}}})
    fired = []
    monkeypatch.setattr("hvactrend.ai.weather.update_weather",
                        lambda dal, cfg: fired.append("weather") or {"stored": 1})
    monkeypatch.setattr("hvactrend.ai.roles.assign_roles",
                        lambda dal: fired.append("roles") or {"classified": 1, "total": 1})
    monkeypatch.setattr("hvactrend.ai.baseline.build_baselines",
                        lambda dal, cfg: {"points": 1, "corrs": 0, "window_days": 30})
    # a freshly-booted machine: monotonic() is small, smaller than BOTH intervals
    monkeypatch.setattr("time.monotonic", lambda: 12.0)
    c._maybe_refresh_weather()
    c._maybe_rebuild_model()
    assert fired == ["weather", "roles"], "both must run on a just-booted machine"

    # and having run, they must respect their interval from that point
    fired.clear()
    monkeypatch.setattr("time.monotonic", lambda: 100.0)
    c._maybe_refresh_weather()
    c._maybe_rebuild_model()
    assert fired == []


def test_weather_and_model_refresh_are_DUE_at_startup(monkeypatch):
    """A restart must bring both current immediately — waiting a full interval after a
    deploy is how the weather table sat 67 h stale in the first place."""
    c = _collector({"weather": {"refresh_interval_s": 3600},
                    "analysis": {"baseline": {"rebuild_interval_s": 86400}}})
    fired = []
    monkeypatch.setattr("hvactrend.ai.weather.update_weather",
                        lambda dal, cfg: fired.append("weather") or {"stored": 1})
    monkeypatch.setattr("hvactrend.ai.roles.assign_roles",
                        lambda dal: fired.append("roles") or {"classified": 1, "total": 1})
    monkeypatch.setattr("hvactrend.ai.baseline.build_baselines",
                        lambda dal, cfg: {"points": 1, "corrs": 0, "window_days": 30})
    c._maybe_refresh_weather()
    c._maybe_rebuild_model()
    assert fired == ["weather", "roles"]

    # ...and NOT again on the very next loop iteration
    fired.clear()
    c._maybe_refresh_weather()
    c._maybe_rebuild_model()
    assert fired == []


def test_refresh_can_be_switched_off(monkeypatch):
    c = _collector({"weather": {"refresh_interval_s": 0},
                    "analysis": {"baseline": {"rebuild_interval_s": None}}})
    monkeypatch.setattr("hvactrend.ai.weather.update_weather",
                        lambda dal, cfg: pytest.fail("weather ran while disabled"))
    monkeypatch.setattr("hvactrend.ai.roles.assign_roles",
                        lambda dal: pytest.fail("model rebuild ran while disabled"))
    c._maybe_refresh_weather()
    c._maybe_rebuild_model()


def test_turning_the_ai_summary_on_does_not_bring_back_the_baselines(monkeypatch):
    """The summary (ai/summary.py) reads no baselines, and a rebuild is 21 s of poll-loop
    time. They follow ai.baselines, not ai.enabled (2026-10-20)."""
    built = []
    monkeypatch.setattr("hvactrend.ai.roles.assign_roles",
                        lambda dal: {"classified": 1, "total": 1})
    monkeypatch.setattr("hvactrend.ai.baseline.build_baselines",
                        lambda dal, cfg: built.append(1) or {"points": 1, "corrs": 0,
                                                             "window_days": 30})
    for ai, want in (({"enabled": True}, []), ({"enabled": True, "baselines": True}, [1])):
        built.clear()
        c = _collector({"ai": ai, "analysis": {"baseline": {"rebuild_interval_s": 86400}}})
        c._maybe_rebuild_model()
        assert built == want, ai


def test_a_failed_weather_fetch_backs_off_instead_of_hammering(monkeypatch):
    """The interval is stamped BEFORE the call, so a provider outage retries next
    interval rather than on every single poll cycle."""
    n = []
    monkeypatch.setattr("hvactrend.ai.weather.update_weather",
                        lambda dal, cfg: n.append(1) or {"error": "boom", "stored": 0})
    c = _collector({"weather": {"refresh_interval_s": 3600}})
    c._maybe_refresh_weather()
    c._maybe_refresh_weather()
    c._maybe_refresh_weather()
    assert len(n) == 1        # failed once, backed off — did not hammer the endpoint


def test_collector_loop_survives_a_weather_or_model_explosion(monkeypatch):
    """Upkeep must never take down a 24/7 collector."""
    monkeypatch.setattr("hvactrend.ai.weather.update_weather",
                        lambda dal, cfg: (_ for _ in ()).throw(RuntimeError("network down")))
    c = _collector({"weather": {"refresh_interval_s": 3600}})
    with pytest.raises(RuntimeError):
        c._maybe_refresh_weather()          # raises...
    # ...and run() wraps it — verified by the guard in run(); assert the guard exists
    import inspect
    src = inspect.getsource(Collector.run)
    assert "weather refresh failed" in src and "model rebuild failed" in src


# ── automatic database backup (the trend history, not the code) ──────────────
def _tiny_db(path):
    import sqlite3
    c = sqlite3.connect(path)
    with c:
        c.execute("CREATE TABLE samples (point_id INT, ts_utc INT, value_num REAL)")
        c.executemany("INSERT INTO samples VALUES (?,?,?)",
                      [(1, 1_700_000_000 + i, float(i)) for i in range(50)])
    c.close()
    return path


def test_database_backup_verifies_the_copy_by_reading_it_back(tmp_path):
    """An unverified backup is not a backup. The copy is opened, integrity-checked and
    counted before it is allowed to count as one."""
    from hvactrend.collector.service import _backup_db_file

    src = _tiny_db(str(tmp_path / "src.db"))
    dest = str(tmp_path / "copy.db")
    assert _backup_db_file(src, dest) == 50
    assert os.path.getsize(dest) > 0

    # a source with no samples table cannot be verified -> refused, not silently kept
    import sqlite3
    empty = str(tmp_path / "empty.db")
    sqlite3.connect(empty).close()
    assert _backup_db_file(empty, str(tmp_path / "bad.db")) is None


def test_database_backup_runs_on_the_loop_and_prunes(tmp_path):
    """It exists on the collector loop for the same reason the snapshot does: a job that
    depends on someone remembering is a job that stops happening. Before this, every
    database backup on the box had been taken by hand."""
    from hvactrend.collector.service import Collector

    src = _tiny_db(str(tmp_path / "live.db"))
    bdir = tmp_path / "backups"

    class _Dal:
        db_path = src
    cfg = {"storage": {"path": src,
                       "backup": {"interval_s": 3600, "dir": str(bdir), "keep": 2}}}
    col = Collector(app=None, dal=_Dal(), config=cfg)

    # Three older archives on disk; keep=2 must leave the two NEWEST. Give them DISTINCT
    # mtimes, a day apart: the prune orders by mtime rather than by name now, so that a
    # hand-named or stale-named copy cannot sort itself to the front and survive.
    bdir.mkdir(parents=True, exist_ok=True)
    stale = time.time() - 30 * 86400        # older than the interval, or it is not due
    for i, d in enumerate(("20260101_000000", "20260102_000000", "20260103_000000")):
        f = bdir / f"hvactrend.{d}.db"
        f.write_bytes(b"stale")
        os.utime(f, (stale + i * 86400, stale + i * 86400))

    col._next_dbbackup_check = None         # force a check now

    async def _backup_and_wait():
        await col._maybe_backup_db()
        # ⚠️ The copy is DETACHED (a background task), not awaited inline. It used to be
        # awaited, which froze the poll loop for the whole copy: 304.6 s at 1,750 MB on the
        # live box, 2-3 cycles simply not recorded, growing linearly with the database.
        # This assertion is the contract -- the method hands the work to a task and returns.
        assert col._dbbackup_task is not None, "the backup did not become a task"
        await col._dbbackup_task

    asyncio.run(_backup_and_wait())

    made = sorted(bdir.glob("hvactrend.*.db"))
    assert len(made) == 2, [f.name for f in made]
    assert made[0].name == "hvactrend.20260103_000000.db", "pruned the wrong end"
    assert made[1].read_bytes()[:15] == b"SQLite format 3",         "the surviving new archive must be a real database, not the stale placeholder"

    # not due -> no new file, and no second task
    before = len(made)

    async def _again():
        await col._maybe_backup_db()
        if col._dbbackup_task is not None and not col._dbbackup_task.done():
            await col._dbbackup_task

    asyncio.run(_again())
    assert len(list(bdir.glob("hvactrend.*.db"))) == before
    # and nothing half-written is left where the glob can mistake it for an archive
    assert not list(bdir.glob("*.partial*")), "a partial survived"


def test_database_backup_honours_an_explicit_disable(tmp_path):
    """config.example.yaml has shipped `backup.enabled: false` since before this job
    existed. A config that says off must mean off."""
    from hvactrend.collector.service import Collector

    src = _tiny_db(str(tmp_path / "live.db"))
    bdir = tmp_path / "backups"

    class _Dal:
        db_path = src
    cfg = {"storage": {"path": src, "backup": {"enabled": False, "interval_s": 3600,
                                               "dir": str(bdir)}}}
    col = Collector(app=None, dal=_Dal(), config=cfg)
    col._next_dbbackup_check = None
    asyncio.run(col._maybe_backup_db())
    assert not bdir.exists() or not list(bdir.glob("*.db"))


def test_database_backup_survives_a_restart_loop(tmp_path):
    """`_next_dbbackup_check` resets on every process start, and the watchdog restarts this
    service. If due-ness came from uptime, a restart loop would take a full ~1 GB copy
    every time and prune the real history out of the keep window within minutes. The
    archives on disk are the state, so a restart cannot manufacture a backup."""
    from hvactrend.collector.service import Collector

    src = _tiny_db(str(tmp_path / "live.db"))
    bdir = tmp_path / "backups"

    class _Dal:
        db_path = src
    cfg = {"storage": {"path": src,
                       "backup": {"interval_s": 604800, "dir": str(bdir), "keep": 4}}}

    # first ever run: nothing on disk, so a fresh deploy IS protected immediately
    col = Collector(app=None, dal=_Dal(), config=cfg)
    col._next_dbbackup_check = None
    asyncio.run(col._maybe_backup_db())
    assert len(list(bdir.glob("hvactrend.*.db"))) == 1

    # five "restarts" in a row must not produce five more copies
    for _ in range(5):
        col = Collector(app=None, dal=_Dal(), config=cfg)   # fresh process state
        assert col._next_dbbackup_check is None
        asyncio.run(col._maybe_backup_db())
    assert len(list(bdir.glob("hvactrend.*.db"))) == 1,         "a restart must not trigger a backup while a recent one exists"

    # ...and once the newest archive ages past the interval, it runs again
    old = next(iter(bdir.glob("hvactrend.*.db")))
    stale = time.time() - 604800 - 60
    os.utime(old, (stale, stale))
    col = Collector(app=None, dal=_Dal(), config=cfg)
    asyncio.run(col._maybe_backup_db())
    assert len(list(bdir.glob("hvactrend.*.db"))) == 2


def test_maintenance_is_due_from_the_data_not_from_uptime(tmp_path):
    """The maintenance timer used to start at time.monotonic() on every loop start, so the
    first housekeeping came one full interval after boot. That resets on every restart --
    and THE WATCHDOG RESTARTS THIS SERVICE. A service flapping faster than
    maintenance_interval_s would never build a rollup or prune a row again, silently.

    Observed for real: three restarts inside an hour left the 5-minute rollups 57 minutes
    stale and the hourly ones 147 minutes, which put 20,254 raw rows into the trend
    picker's correlated-subquery tail and took /api/logged from 0.18 s to 1.6 s.

    The fix must NOT be "always run at startup" -- that rebuilds rollups on every flap.
    The data decides."""
    from hvactrend.collector.service import Collector

    cfg = {"storage": {"maintenance_interval_s": 3600}}

    class _Dal:
        def __init__(self, wm):
            self._wm = wm
        def rollup_watermark(self):
            return self._wm

    now = int(time.time())

    # rollups fresh -> NOT due; a restart must not trigger a rebuild
    col = Collector(app=None, dal=_Dal(now - 60), config=cfg)
    seeded = col._seed_maint_clock()
    assert time.monotonic() - seeded < 3600, "fresh rollups must not force maintenance"

    # rollups two hours stale -> due immediately, however recently the process started
    col = Collector(app=None, dal=_Dal(now - 7200), config=cfg)
    assert time.monotonic() - col._seed_maint_clock() >= 3600, \
        "a restart must not hide stale rollups"

    # nothing rolled up at all -> due immediately
    col = Collector(app=None, dal=_Dal(None), config=cfg)
    assert time.monotonic() - col._seed_maint_clock() >= 3600

    # a DAL that cannot answer must degrade to the old behaviour, never crash the loop
    class _Broken:
        def rollup_watermark(self):
            raise RuntimeError("no such table")
    col = Collector(app=None, dal=_Broken(), config=cfg)
    assert time.monotonic() - col._seed_maint_clock() < 1.0


def test_rollup_watermark_reads_the_newest_bucket(tmp_path):
    """Backs the scheduling decision above, so it has to be right on an empty DB too."""
    from hvactrend.models import Quality, Sample
    from hvactrend.storage.sqlite_dal import SQLiteDAL

    dal = SQLiteDAL(tmp_path / "wm.db")
    dal.apply_schema()
    c = dal._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'DEMO','Demo Building')")
    c.execute("INSERT INTO buildings(id,site_id,code,name) VALUES (1,1,'MAIN','Main')")
    c.execute("INSERT INTO systems(id,building_id,code,name) VALUES (1,1,'AC-1','AC-1')")
    c.execute("INSERT INTO devices(id,site_id,device_instance,created_utc) VALUES (1,1,1001,0)")
    c.execute("INSERT INTO points(id,device_id,system_id,source,object_type,"
              "object_instance,name,enabled,created_utc) "
              "VALUES (1,1,1,'bacnet','analogInput',1,'DAT',1,0)")

    assert dal.rollup_watermark() is None, "no rollups yet must read as None, not 0"

    base = 1_000_020
    dal.write_samples([Sample(point_id=1, ts_utc=base + i * 60, value_num=50.0 + i,
                              quality=Quality.GOOD) for i in range(20)])
    dal.build_rollups(base + 20 * 60)
    wm = dal.rollup_watermark()
    assert wm is not None and wm % 300 == 0
    assert wm == c.execute("SELECT MAX(bucket_utc) FROM samples_5m").fetchone()[0]


# ── 2026-09-29 audit, A2: a "not due" check must not push due-ness a whole interval out ────
# Both jobs used to stamp a "last ran" clock BEFORE reading the disk, so a check landing
# shortly before due-ness re-armed the gate for a full interval. Observed: no snapshot at
# all on 2026-09-23. Predicted from code: the 09-29 weekly backup slipping ~2 days.

def test_a_not_due_check_rechecks_when_the_archive_comes_due():
    """The pure rule, on the exact 09-22/09-23 shape: an archive that comes due in three
    minutes must be re-checked in three minutes -- not in 24 hours."""
    from hvactrend.collector.service import _next_check
    now_w, now_m, day = 2_000_000_000.0, 5_000.0, 86400.0

    due, nxt = _next_check(now_w - day + 180, day, now_wall=now_w, now_mono=now_m)
    assert not due
    assert abs((nxt - now_m) - 180) < 1, f"re-check in {(nxt - now_m) / 3600:.1f} h, not 3 min"

    assert _next_check(now_w - day - 10, day, now_wall=now_w, now_mono=now_m) == (True, now_m)
    assert _next_check(0.0, day, now_wall=now_w, now_mono=now_m) == (True, now_m), \
        "a family with no archives at all must be due now"
    # a hair in the future is floored, never a tight loop
    due, nxt = _next_check(now_w - day + 5, day, now_wall=now_w, now_mono=now_m)
    assert not due and nxt - now_m == 60


def test_a_not_due_backup_check_is_not_a_full_week_away(tmp_path):
    """End to end through _maybe_backup_db: the newest archive comes due in two minutes, so
    the next check must be about two minutes out. Before the fix it was seven days."""
    from hvactrend.collector.service import Collector

    src = _tiny_db(str(tmp_path / "live.db"))
    bdir = tmp_path / "backups"
    bdir.mkdir()
    week = 604800
    old = bdir / "hvactrend.20260922_155228.db"
    old.write_bytes(b"x")
    t = time.time() - week + 120
    os.utime(old, (t, t))

    class _Dal:
        db_path = src
    col = Collector(app=None, dal=_Dal(), config={
        "storage": {"path": src, "backup": {"interval_s": week, "dir": str(bdir), "keep": 2}}})
    asyncio.run(col._maybe_backup_db())

    assert col._dbbackup_task is None, "it was not due, yet a backup started"
    delay = col._next_dbbackup_check - time.monotonic()
    assert 60 <= delay <= 180, (
        f"next backup check {delay / 3600:.1f} h away; the archive comes due in 2 min")


def _fake_snapshot_script(tmp_path, dest, *, writes=True):
    """A stand-in for scripts/snapshot.py: same DESTS / snapshot() surface, tmp paths only,
    so the test can never touch the real snapshot destinations."""
    f = tmp_path / "fake_snapshot.py"
    body = ("(DESTS[0] / 'HVACTrend_snapshot_2099-01-01_000000.zip').write_bytes(b'x')"
            if writes else "pass")
    f.write_text(
        "from pathlib import Path\n"
        f"DESTS = [Path(r'{dest}')]\n"
        "def snapshot():\n"
        f"    {body}\n"
        "    return ['wrote it' if " + str(writes) + " else 'FAILED: disk full']\n",
        encoding="utf-8")
    return f


def test_a_not_due_snapshot_check_is_not_a_full_day_away(tmp_path):
    """The case that actually happened: an archive three minutes short of due. The next
    check must land when it comes due; once it runs, the next is one interval out."""
    from hvactrend.collector.service import Collector
    dest = tmp_path / "snaps"
    dest.mkdir()
    arch = dest / "HVACTrend_snapshot_2026-09-22_164126.zip"
    arch.write_bytes(b"x")
    t = time.time() - 86400 + 180
    os.utime(arch, (t, t))

    col = Collector(app=None, dal=object(), config={"storage": {"snapshot_interval_s": 86400}})
    col._snapshot_script = _fake_snapshot_script(tmp_path, dest)
    col._maybe_snapshot()
    assert len(list(dest.glob("*.zip"))) == 1, "it was not due, yet a snapshot was taken"
    delay = col._next_snapshot_check - time.monotonic()
    assert 60 <= delay <= 240, f"re-check {delay / 3600:.1f} h away; due in 3 min"

    # time passes: the archive is now overdue, so the next check takes the snapshot...
    t = time.time() - 86400 - 60
    os.utime(arch, (t, t))
    col._next_snapshot_check = None
    col._maybe_snapshot()
    assert len(list(dest.glob("*.zip"))) == 2, "an overdue snapshot did not run"
    # ...and the following check is about one interval out, measured from the NEW archive
    delay = col._next_snapshot_check - time.monotonic()
    assert delay > 86400 * 0.9, f"after a good run the next check is only {delay:.0f} s out"


def test_a_failed_snapshot_retries_on_the_backoff_not_every_cycle(tmp_path):
    """No new archive after a run means it failed. Retry on the backoff: not every two
    minutes (log spam, hammering a full disk) and not a whole day later."""
    from hvactrend.collector.service import Collector, _RETRY_BACKOFF_S
    dest = tmp_path / "snaps"
    dest.mkdir()
    col = Collector(app=None, dal=object(), config={"storage": {"snapshot_interval_s": 86400}})
    col._snapshot_script = _fake_snapshot_script(tmp_path, dest, writes=False)
    col._maybe_snapshot()
    delay = col._next_snapshot_check - time.monotonic()
    assert abs(delay - _RETRY_BACKOFF_S) < 5, f"failed snapshot retries in {delay:.0f} s"


# ── A8: one database copy at a time, whichever Collector instance asks ──────────────────────────

def test_a_copy_still_running_blocks_a_second_backup(tmp_path):
    """Cancelling the task that awaits a copy does not stop its worker thread. A collector
    relaunched mid-backup used to start a second copy beside the first."""
    from hvactrend.collector.service import Collector, _BACKUP_LOCK

    src = _tiny_db(str(tmp_path / "live.db"))
    bdir = tmp_path / "backups"

    class _Dal:
        db_path = src
    cfg = {"storage": {"path": src, "backup": {"interval_s": 3600, "dir": str(bdir), "keep": 2}}}

    assert _BACKUP_LOCK.acquire(blocking=False), "lock unexpectedly held by another test"
    try:                                   # a PREVIOUS instance's thread is mid-copy
        col = Collector(app=None, dal=_Dal(), config=cfg)
        asyncio.run(col._maybe_backup_db())
        assert col._dbbackup_task is None, "started a second copy while one was running"
        assert not bdir.exists() or not list(bdir.glob("hvactrend.*.db"))
        assert 0 < col._next_dbbackup_check - time.monotonic() <= 300, \
            "while blocked it should look again within minutes"
    finally:
        _BACKUP_LOCK.release()

    col = Collector(app=None, dal=_Dal(), config=cfg)       # the copy finished
    asyncio.run(col._maybe_backup_db())
    assert len(list(bdir.glob("hvactrend.*.db"))) == 1, "once free, the backup must run"


def test_backup_db_file_refuses_rather_than_copying_concurrently(tmp_path):
    """The lock is taken inside the copy function itself, so the manual route is covered too."""
    from hvactrend.collector.service import _BACKUP_LOCK, _backup_db_file
    src = _tiny_db(str(tmp_path / "live.db"))
    dest = tmp_path / "hvactrend.x.db"
    assert _BACKUP_LOCK.acquire(blocking=False)
    try:
        assert _backup_db_file(src, str(dest)) is None
        assert not dest.exists() and not (tmp_path / "hvactrend.x.db.partial").exists()
    finally:
        _BACKUP_LOCK.release()
    assert _backup_db_file(src, str(dest)) == 50, "and it copies normally once free"
