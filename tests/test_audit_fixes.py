"""Acceptance tests for the 2026-09-15 external audit findings.

Each test pins one finding. They are grouped here rather than scattered into the existing
files because what they have in common is the reason they exist: an outside reviewer proved
these were wrong, and two of them contradicted claims the project then made about itself.
Keeping them together makes it obvious if the set ever stops running.
"""

import sqlite3
import time
from pathlib import Path

import pytest

from hvactrend.models import Quality, Sample
from hvactrend.storage.sqlite_dal import SQLiteDAL


def _dal_with_point(tmp_path, name="s.db"):
    dal = SQLiteDAL(tmp_path / name)
    dal.apply_schema()
    c = dal._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'DEMO','Demo Building')")
    c.execute("INSERT INTO buildings(id,site_id,code,name) VALUES (1,1,'B1','Main')")
    c.execute("INSERT INTO systems(id,building_id,code,name) VALUES (1,1,'AC-1','AC-1')")
    c.execute("INSERT INTO devices(id,site_id,device_instance,created_utc) VALUES (1,1,1001,0)")
    c.execute("INSERT INTO points(id,device_id,system_id,source,object_type,"
              "object_instance,name,enabled,created_utc) VALUES "
              "(1,1,1,'bacnet','analogInput',1,'DAT',1,0)")
    return dal


# ── F-01 · the snapshot schedule reads the files, not process uptime ─────────────────────

def test_snapshot_dueness_reads_the_newest_archive_on_disk(tmp_path):
    """The bug: `_last_snapshot` starts as None and is re-initialised by every restart, so
    a restart loop snapshotted on every process start and walked the keep window forward
    until no genuine archive was left. Due-ness must come from the files."""
    from hvactrend.collector.service import newest_archive_mtime

    dest = tmp_path / "snaps"
    dest.mkdir()
    assert newest_archive_mtime([dest]) == 0.0          # nothing yet -> always due
    assert newest_archive_mtime([tmp_path / "nope"]) == 0.0   # missing dir is not an error

    f = dest / "HVACTrend_snapshot_2026-09-15_010000.zip"
    f.write_bytes(b"x")
    os_time = time.time() - 3600
    import os
    os.utime(f, (os_time, os_time))
    got = newest_archive_mtime([dest])
    assert abs(got - os_time) < 2, "must report the archive's own mtime"
    # an hour-old archive against a daily interval is NOT due
    assert time.time() - got < 86400


def test_snapshot_prune_keeps_days_not_files(tmp_path, monkeypatch):
    """A burst of same-day snapshots must not evict days it did not replace. Before the fix
    this was a pure file count, so seven runs in an hour cost seven days of history."""
    import importlib.util
    sp = Path(__file__).resolve().parents[1] / "scripts" / "snapshot.py"
    spec = importlib.util.spec_from_file_location("snap_under_test", sp)
    snap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(snap)
    monkeypatch.setattr(snap, "KEEP", 5)

    import os
    dest = tmp_path / "snaps"
    dest.mkdir()
    now = time.time()

    def put(when):
        stamp = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(when))
        f = dest / f"HVACTrend_snapshot_{stamp}.zip"
        f.write_bytes(b"x")
        os.utime(f, (when, when))
        return f

    # five genuine daily archives, 2..6 days back
    daily = [put(now - d * 86400) for d in range(6, 1, -1)]
    # then a burst of eight snapshots today
    burst = [put(now - i * 60) for i in range(8, 0, -1)]

    snap._prune(dest)
    left = {f.name for f in dest.glob("*.zip")}

    survivors = [f for f in daily if f.name in left]
    assert len(survivors) >= 4, (
        "a same-day burst evicted genuine daily history: only %d of 5 days survived"
        % len(survivors))
    # everything from the last 24 h is kept, so a deliberate same-day snapshot is not lost
    assert all(f.name in left for f in burst)


# ── F-05 · retention cannot be reached from the API, and has a floor ─────────────────────

def test_retention_keys_are_not_settable_from_the_api():
    """A retention window is the only setting whose new value DELETES data on the next
    maintenance cycle. It does not belong behind a dropdown."""
    from hvactrend.config import is_settable
    for key in ("storage.retention.raw_days", "storage.retention.rollup_1h_days",
                "storage.path", "storage.backup.dir"):
        assert not is_settable(key), f"{key} must not be settable"
    # ⚠️ This test used to assert `is_settable("storage.backup.keep")` is TRUE, on the
    # grounds that F-05 was "a scalpel not a hammer". The 2026-09-22 audit (L-10) showed
    # that was the wrong intent, pinned as a requirement: keep:1 or enabled:false removes
    # the backup rotation, and rollups.enabled:false stops the long memory growing while
    # raw still expires at 730 days. Applying F-05's own argument to the rest of the domain
    # left nothing that should be web-editable, so `storage` is out of SETTABLE_DOMAINS.
    for key in ("storage.backup.keep", "storage.backup.enabled",
                "storage.backup.min_free_bytes", "storage.rollups.enabled",
                "storage.maintenance_interval_s", "storage.snapshot_interval_s",
                "storage.engine"):
        assert not is_settable(key), f"{key} is a durability knob, not a setting"
    # ... and the domains that ARE settable still are, so this is not a blanket lock.
    assert is_settable("appearance.theme") or is_settable("logging.level")


def test_retention_refuses_a_value_below_the_floor(tmp_path, caplog):
    dal = _dal_with_point(tmp_path)
    now = 10_000_000
    dal.write_samples([
        Sample(point_id=1, ts_utc=now - 100 * 86400, value_num=1.0, quality=Quality.GOOD),
        Sample(point_id=1, ts_utc=now - 1 * 86400, value_num=2.0, quality=Quality.GOOD),
    ])
    deleted = dal.enforce_retention(now, {"raw_days": 1})       # below the 7-day floor
    assert not deleted.get("samples"), "a sub-floor window must delete NOTHING"
    assert len(dal.query_series(1, 0, now + 1)) == 2
    dal.close()


def test_retention_refuses_a_prune_that_would_take_most_of_the_record(tmp_path):
    """The floor stops an absurd value; this stops a merely wrong one. Both conditions have
    to hold -- a large fraction AND a large absolute count -- or a small/fresh database
    could never prune at all."""
    dal = _dal_with_point(tmp_path)
    monkey_rows = 1000
    dal.MIN_PRUNE_GUARD_ROWS = 10          # scale the guard down for a small fixture
    now = 10_000_000
    old = [Sample(point_id=1, ts_utc=now - 100 * 86400 + i, value_num=1.0,
                  quality=Quality.GOOD) for i in range(monkey_rows)]
    new = [Sample(point_id=1, ts_utc=now - 3600 + i, value_num=2.0,
                  quality=Quality.GOOD) for i in range(10)]
    dal.write_samples(old + new)
    total_before = dal._conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]

    deleted = dal.enforce_retention(now, {"raw_days": 30})
    assert not deleted.get("samples"), "a prune of 99% of the record must be refused"
    assert dal._conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == total_before

    # ...and the operator can still force it deliberately
    deleted = dal.enforce_retention(now, {"raw_days": 30, "allow_large_prune": True})
    assert deleted.get("samples") == monkey_rows
    dal.close()


# ── F-06 · a poll cycle is ONE transaction ───────────────────────────────────────────────

def test_write_samples_is_a_single_transaction(tmp_path):
    """Autocommit made executemany commit per row: 657 transactions and 657 WAL fsyncs per
    poll cycle, and a crash mid-cycle left half an observation in the record."""
    dal = _dal_with_point(tmp_path)
    seen: list[str] = []
    # sqlite3.Connection.execute is read-only on the instance, so watch the statements the
    # driver actually runs instead of wrapping the method.
    dal._conn.set_trace_callback(lambda sql: seen.append(str(sql).strip().split()[0].upper()))
    dal.write_samples([Sample(point_id=1, ts_utc=1000 + i, value_num=float(i),
                              quality=Quality.GOOD) for i in range(50)])
    dal._conn.set_trace_callback(None)
    assert seen.count("BEGIN") == 1, f"expected one BEGIN, saw {seen.count('BEGIN')}"
    assert seen.count("COMMIT") == 1, f"expected one COMMIT, saw {seen.count('COMMIT')}"
    assert dal._conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 50
    dal.close()


def test_a_failed_write_leaves_no_partial_cycle(tmp_path):
    """Atomicity is the half that matters for a recorder: either the whole poll cycle is in
    the record or none of it is."""
    dal = _dal_with_point(tmp_path)
    good = [Sample(point_id=1, ts_utc=2000 + i, value_num=1.0, quality=Quality.GOOD)
            for i in range(10)]
    bad = [Sample(point_id=999_999, ts_utc=3000, value_num=1.0, quality=Quality.GOOD)]
    with pytest.raises(sqlite3.IntegrityError):
        dal.write_samples(good + bad)             # FK violation on the last row
    assert dal._conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 0, \
        "a partial poll cycle survived a failed write"
    dal.close()


# ── F-11 · a faulted bucket leaves a row saying so ───────────────────────────────────────

def test_a_fully_faulted_bucket_is_recorded_not_omitted(tmp_path):
    """Before the fix, a bucket in which EVERY sample was faulted produced no rollup row at
    all. After raw retention expires that makes a failed sensor indistinguishable from a
    period the collector never ran — and AC-1's mixed-air sensor is faulted right now."""
    dal = _dal_with_point(tmp_path)
    base = 3_600_000                            # an exact hour boundary
    dal.write_samples(
        # hour 0: ordinary
        [Sample(point_id=1, ts_utc=base + i * 120, value_num=55.0 + i, quality=Quality.GOOD)
         for i in range(10)]
        # hour 1: recorded, but faulted from end to end
        + [Sample(point_id=1, ts_utc=base + 3600 + i * 120, value_num=185.0,
                  quality=Quality.FAULT) for i in range(10)]
    )
    dal.build_rollups(base + 3 * 3600)
    rows = {r["bucket_utc"]: r for r in dal._conn.execute(
        "SELECT bucket_utc, n, n_total, v_avg FROM samples_1h WHERE point_id=1")}

    assert base in rows and rows[base]["n"] == 10 and rows[base]["v_avg"] is not None

    faulted = rows.get(base + 3600)
    assert faulted is not None, "the faulted hour vanished — it must leave a row"
    assert faulted["n"] == 0, "no GOOD samples, so n must be 0"
    assert faulted["n_total"] == 10, "but ten readings were recorded, and that is the point"
    assert faulted["v_avg"] is None, "there is no meaningful average of ten faults"
    dal.close()


# ── F-09 · saved views are durable, and reach a remote tech ──────────────────────────────

def _views_app(tmp_path):
    import pytest as _pytest
    _pytest.importorskip("fastapi")
    from tests.test_browser import _viewer_app
    return _viewer_app(tmp_path)


def test_saved_views_round_trip_through_the_database(tmp_path):
    """The one piece of operator configuration that used to live only in localStorage, so a
    view built at the desk did not exist on the phone or for a visiting tech."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from tests.test_browser import _viewer_app
    app = _viewer_app(tmp_path)
    with TestClient(app) as c:
        assert c.get("/api/views").json()["views"] == {}
        spec = {"sel": [1, 2], "axisMode": "shared", "start": 100, "end": 200}
        assert c.put("/api/views/morning check", json={"spec": spec}).status_code == 200
        got = c.get("/api/views").json()["views"]
        assert "morning check" in got
        assert got["morning check"]["sel"] == [1, 2]
        # saving again under the same name updates rather than duplicating
        c.put("/api/views/morning check", json={"spec": {**spec, "axisMode": "each"}})
        got = c.get("/api/views").json()["views"]
        assert len(got) == 1 and got["morning check"]["axisMode"] == "each"
        assert c.delete("/api/views/morning check").status_code == 200
        assert c.get("/api/views").json()["views"] == {}
        assert c.delete("/api/views/nope").status_code == 404


def test_a_remote_tech_can_read_views_but_not_write_them(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from tests.test_browser import _viewer_app, _remote_client
    app = _viewer_app(tmp_path)
    with TestClient(app) as local:
        local.put("/api/views/plant start-up", json={"spec": {"sel": [1]}})
    with _remote_client(app) as rc:
        got = rc.get("/api/views")
        assert got.status_code == 200, "a view built at the desk must reach the phone"
        assert "plant start-up" in got.json()["views"]
        # ...but writing stays local, exactly like renaming a point
        assert rc.put("/api/views/mine", json={"spec": {}}).status_code == 404
        assert rc.delete("/api/views/plant start-up").status_code == 404


# ── F-04 · the manual backup verifies, and never prunes behind a bad copy ────────────────

def test_manual_backup_verifies_and_does_not_prune_on_failure(tmp_path, monkeypatch):
    """This was the only backup path that wrote without checking — and the only one that
    deleted a verified archive in the same breath. It is also the one a human reaches for
    when they are already worried."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from tests.test_browser import _viewer_app
    import hvactrend.collector.service as svc

    app = _viewer_app(tmp_path)
    with TestClient(app) as c:
        bdir = Path("data/backups")
        monkeypatch.setattr(svc, "_backup_db_file", lambda s, d: None)   # verification fails
        before = set(bdir.glob("hvactrend.*.db")) if bdir.exists() else set()
        r = c.post("/api/backup")
        assert r.status_code == 500, "an unverified backup must not be reported as success"
        after = set(bdir.glob("hvactrend.*.db")) if bdir.exists() else set()
        assert after == before, "a verified archive was pruned behind a failed copy"


# ── F-10 · the fail-closed default can actually fire ─────────────────────────────────────

def test_remote_viewer_fails_closed_before_the_lifespan_runs(tmp_path):
    """`_cfg(..., default=True)` in the remote-viewer middleware is a deliberate fail-closed
    choice, but it could only apply once app.state.config existed — otherwise the request
    500ed instead. A TestClient used without its context manager reproduces it."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from tests.test_browser import _viewer_app
    app = _viewer_app(tmp_path)
    rc = TestClient(app, client=("192.0.2.77", 51234))       # no `with` -> no lifespan
    r = rc.get("/api/systems")
    assert r.status_code != 500, "missing config must fail CLOSED, not explode"
    assert r.status_code == 404, f"expected the viewer-only 404, got {r.status_code}"


# ── Q4 · the extent must not hide history the rollups still hold ─────────────────────────

def test_extent_includes_rollup_only_history(tmp_path):
    """`samples_time_extent` read the raw table alone. Retention keeps hourly buckets ten
    times longer than raw, so on the first day retention ran the viewer would have started
    reporting a shorter record than the program holds — silently. Compounds with F-03: the
    strip would draw a confident line over a span it was also refusing to admit existed."""
    dal = _dal_with_point(tmp_path)
    base = 3_600_000
    dal.write_samples([Sample(point_id=1, ts_utc=base + i * 120, value_num=1.0,
                              quality=Quality.GOOD) for i in range(60)])
    dal.build_rollups(base + 4 * 3600)
    # now simulate retention having pruned the raw tail while the rollups survive
    dal._conn.execute("DELETE FROM samples WHERE ts_utc < ?", (base + 3600,))
    ext = dal.samples_time_extent()
    assert ext["min_ts"] <= base, (
        f"extent starts at {ext['min_ts']} but hourly rollups reach back to {base} — "
        "the viewer would hide history that is still on disk")
    dal.close()


def test_disabled_points_keep_their_history_visible(tmp_path):
    """M-6: unticking a point stops the logging (intended) but used to remove it from
    /api/logged entirely -- and the viewer drops any ?points= id it cannot find there. The
    recorded past became unreachable through the UI while still sitting in the database.
    On the live building that hid 389 points."""
    dal = _dal_with_point(tmp_path)
    base = 3_600_000
    dal.write_samples([Sample(point_id=1, ts_utc=base + i * 120, value_num=1.0,
                              quality=Quality.GOOD) for i in range(60)])
    dal.build_rollups(base + 4 * 3600)

    assert [p["id"] for p in dal.logged_points()] == [1]
    assert dal.logged_points()[0]["logging"] is True

    dal._conn.execute("UPDATE points SET enabled = 0 WHERE id = 1")
    rows = dal.logged_points()
    assert [p["id"] for p in rows] == [1], "a point with history vanished when unticked"
    assert rows[0]["logging"] is False, "it must be marked as no longer logging"
    assert rows[0]["sample_count"] > 0, "its history must still be reachable"


def test_a_disabled_point_with_no_history_stays_hidden(tmp_path):
    """The picker must not grow by 11,000 enumerated-but-never-logged points."""
    dal = _dal_with_point(tmp_path)
    dal._conn.execute(
        "INSERT INTO points (id, device_id, system_id, object_type, object_instance, "
        " name, enabled, created_utc) VALUES (99, 1, 1, 'analog-input', 99, 'NEVER', 0, 0)")
    assert 99 not in [p["id"] for p in dal.logged_points()]


def test_series_batch_is_bounded(tmp_path):
    """M-10: the route is remote-reachable; one request must not be able to pull the whole
    record into Python on the collector's event loop."""
    from hvactrend.api.app import (_SERIES_MAX_IDS, _bounded_rollup)

    # duplicates collapse, and the id list is capped
    ids = list(dict.fromkeys([1, 1, 2, 2] + list(range(100))))[:_SERIES_MAX_IDS]
    assert len(ids) == _SERIES_MAX_IDS
    assert ids[:2] == [1, 2], "de-duplication must preserve the caller's order"

    # a narrow raw window is left alone
    assert _bounded_rollup(None, 2, 3600) is None
    # 64 points over the whole two-year policy at raw is coarsened, not served
    assert _bounded_rollup(None, 64, 730 * 86400) == "1h"
    # and an explicit coarse request is never made FINER
    assert _bounded_rollup("1h", 1, 3600) == "1h"


def test_a_backward_clock_step_does_not_overwrite_the_record(tmp_path):
    """M-12: every timestamp comes from one time.time() call and nothing checked it. A
    backward step collided with stored rows; INSERT OR REPLACE overwrote the earlier
    observation silently. First observation wins, and the collision is logged."""
    dal = _dal_with_point(tmp_path)
    ts = 3_600_000
    dal.write_samples([Sample(point_id=1, ts_utc=ts, value_num=11.0, quality=Quality.GOOD)])
    # the clock steps back and the same second is polled again with a different value
    dal.write_samples([Sample(point_id=1, ts_utc=ts, value_num=99.0, quality=Quality.GOOD)])

    rows = dal.query_series(1, ts - 1, ts + 1)
    assert len(rows) == 1
    assert rows[0]["value_num"] == 11.0, "the earlier observation was overwritten"


def test_a_rollup_window_reaches_the_live_edge(tmp_path):
    """M-9: the rollups are materialised hourly, so a rollup query used to stop at the last
    materialised bucket. The fallback to raw only fired when the table was EMPTY, so a wide
    window ended up to an hour short and read as "collection has stopped an hour ago"."""
    from hvactrend.api.app import _series_with_fallback

    dal = _dal_with_point(tmp_path)
    base = 3_600_000                                   # exactly on an hour boundary
    # four hours of 2-minute samples
    dal.write_samples([Sample(point_id=1, ts_utc=base + i * 120, value_num=float(i),
                              quality=Quality.GOOD) for i in range(120)])
    # materialise only the FIRST two hours, as an hourly maintenance pass would
    dal.build_rollups(base + 2 * 3600)
    end = base + 4 * 3600 - 1

    stored = dal.query_series(1, base, end, rollup="1h")
    assert stored, "fixture built no rollups"
    assert int(stored[-1]["bucket_utc"]) == base + 3600,         "fixture should leave two hours unmaterialised"

    eff, rows = _series_with_fallback(dal, 1, base, end, "1h")
    assert eff == "1h", "the tail must not silently downgrade the whole window to raw"
    buckets = [int(r["bucket_utc"]) for r in rows]
    assert buckets == [base, base + 3600, base + 7200, base + 10800], buckets

    # the stitched buckets carry the same shape as the stored ones, F-11 counts included
    tail = rows[-1]
    assert tail["n"] == 30 and tail["n_total"] == 30
    assert tail["v_avg"] is not None and tail["v_min"] is not None


def test_a_faulted_tail_bucket_is_recorded_not_omitted(tmp_path):
    """The stitched tail must keep F-11's distinction: recorded-and-faulted is not a gap."""
    dal = _dal_with_point(tmp_path)
    base = 3_600_000
    dal.write_samples([Sample(point_id=1, ts_utc=base + i * 120, value_num=1.0,
                              quality=Quality.FAULT) for i in range(30)])
    got = dal.tail_buckets(1, base, base + 3600 - 1, "1h")
    assert len(got) == 1, got
    assert got[0]["n"] == 0 and got[0]["n_total"] == 30
    assert got[0]["v_avg"] is None


# ⚠️ REMOVED 2026-09-22: test_extent_does_not_scan_the_rollup_tables.
#
# It was written to pin the 2.2 s extent regression and pinned NOTHING, two ways over.
# Found by the second external audit (M-2), which re-injected the exact regression into a
# scratch copy and watched the whole suite stay green:
#
#   1. it EXPLAINed a hand-copied SQL STRING LITERAL that lived in this file, never the SQL
#      `samples_time_extent()` actually executes. Reverting the production code could not
#      fail it, because the production code was never involved.
#   2. its assertion -- `"USING PRIMARY KEY" in plan` -- matched the regression anyway.
#      SQLite plans a bare `MIN(bucket_utc)` on a WITHOUT ROWID table as
#      `SEARCH samples_5m USING PRIMARY KEY`, which reads as optimal and measures 1,126 ms
#      against 10.7 ms on the live database. Plan text alone cannot tell the forms apart.
#
# Replaced by tests/test_plan_guard.py, which captures the SQL each DAL method really runs
# (set_trace_callback), asserts on the ABSENCE of an unconstrained plan line, and carries
# test_guard_discriminates to prove it can fail at all.
#
# The lesson is kept here rather than in a commit message: a check that cannot fail is
# worse than no check, because it retires the attention that would have caught the thing.


# ── 2026-09-29 audit ── regressions introduced by the 2026-09-22 fixes ───────────────────

def test_series_computes_the_extent_once(tmp_path):
    """A7: the M-10 bound read the extent to clamp the window, then recomputed it for the
    response -- a second 34 ms query on every chart load, added by the fix itself."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from tests.test_browser import _viewer_app
    from hvactrend.storage.sqlite_dal import SQLiteDAL
    app = _viewer_app(tmp_path)
    with TestClient(app) as c:
        # On the CLASS: since 2026-10-27 the work runs in a worker thread on its own
        # read-only connection, not on app.state.dal.
        real = SQLiteDAL.samples_time_extent
        calls = []

        def counting(self):
            calls.append(1)
            return real(self)
        SQLiteDAL.samples_time_extent = counting
        try:
            r = c.post("/api/series", json={"ids": [1, 2, 3], "start": 0, "end": 2_000_000_000})
        finally:
            SQLiteDAL.samples_time_extent = real
        assert r.status_code == 200 and "extent" in r.json()
        assert len(calls) == 1, f"/api/series computed the extent {len(calls)} times"


def test_manual_backup_refuses_while_a_copy_is_running(tmp_path):
    """A8: one database copy at a time. The copy function refuses anyway, but the route used
    to report that refusal as "failed verification" -- the wrong alarm for a button press."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from tests.test_browser import _viewer_app
    from hvactrend.collector.service import _BACKUP_LOCK
    app = _viewer_app(tmp_path)
    with TestClient(app) as c:
        assert _BACKUP_LOCK.acquire(blocking=False)
        try:
            r = c.post("/api/backup")
        finally:
            _BACKUP_LOCK.release()
        assert r.status_code == 409, r.text
