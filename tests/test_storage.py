"""Storage-layer tests for rollup materialization, retention pruning, and the cheap
time-extent — behaviors the trend viewer + 24/7 collector depend on. Stdlib sqlite3
only; a minimal FK parent chain + synthetic samples."""

from hvactrend.models import Point, PointSource, Quality, Sample
from hvactrend.storage.sqlite_dal import SQLiteDAL


def _dal_with_point(tmp_path):
    dal = SQLiteDAL(tmp_path / "s.db")
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


def test_rediscovery_cannot_erase_commissioned_system_id_or_name(tmp_path):
    """Review #3: upsert_point's DO UPDATE wrote system_id=excluded.system_id, but
    system_id is COMMISSIONED (from equipment.sc_dac_map), not discovered — the generic
    discovery path always passes None. One re-scan therefore NULLed the AHU mapping on
    every commissioned point, silently: points keep enabled=1 and keep logging, so the
    collector and raw viewer look fine while every unit dashboard empties, the fleet
    matrix goes blank and canonical_ids collapses all 14 units into one. Same shape for
    `name`, which a device that errors on every property read returns as None — and a
    NULL name makes assign_roles write role=NULL, killing that unit's detectors."""
    dal = _dal_with_point(tmp_path)

    def _p(**kw):
        return Point(source=PointSource.BACNET, device_id=1, object_type="analogInput",
                     object_instance=7, **kw)

    pid = dal.upsert_point(_p(system_id=1, name="Discharge Air Temp",
                              units_text="degrees-fahrenheit"))
    row = dal._conn.execute(
        "SELECT system_id, name, enabled FROM points WHERE id=?", (pid,)).fetchone()
    assert (row[0], row[1]) == (1, "Discharge Air Temp")

    # the discovery-shaped call: same object, no system_id, and a device that answered
    # nothing so the name came back None
    again = dal.upsert_point(_p(system_id=None, name=None,
                                units_text="degrees-fahrenheit"))
    assert again == pid                                   # same row, not a duplicate
    row = dal._conn.execute(
        "SELECT system_id, name, enabled FROM points WHERE id=?", (pid,)).fetchone()
    assert row[0] == 1, "re-discovery must not orphan a commissioned point from its AHU"
    assert row[1] == "Discharge Air Temp", "a failed read must not blank a known name"
    assert row[2] == 1                                    # still logged, as before

    # an EXPLICIT re-assignment must still work (re-commissioning to another AHU)
    dal._conn.execute("INSERT INTO systems(id,building_id,code,name) "
                      "VALUES (2,1,'AC-2','AC-2')")
    dal.upsert_point(_p(system_id=2, name="Discharge Air Temp"))
    row = dal._conn.execute(
        "SELECT system_id FROM points WHERE id=?", (pid,)).fetchone()
    assert row[0] == 2


def test_build_rollups_aggregates_good_samples_and_excludes_faults(tmp_path):
    dal = _dal_with_point(tmp_path)
    base = 1_000_020
    rows = [Sample(point_id=1, ts_utc=base + i * 60, value_num=50.0 + i,
                   quality=(Quality.FAULT if i == 5 else Quality.GOOD))
            for i in range(20)]
    dal.write_samples(rows)
    now = base + 20 * 60
    built = dal.build_rollups(now)
    assert built["samples_5m"] > 0 and built["samples_1h"] > 0

    # a 5-min bucket holding the fault sample (i=5) must count only the GOOD ones
    b = dal._conn.execute(
        "SELECT v_min,v_max,v_last,n FROM samples_5m WHERE bucket_utc=1000200").fetchone()
    assert b["n"] == 4                       # i=3,4,6,7 (i=5 fault dropped)
    assert b["v_min"] == 53.0 and b["v_max"] == 57.0
    assert b["v_last"] == 57.0               # newest good sample in the bucket


def test_build_rollups_excludes_in_progress_bucket_and_is_idempotent(tmp_path):
    dal = _dal_with_point(tmp_path)
    base = 1_000_020
    dal.write_samples([Sample(point_id=1, ts_utc=base + i * 60, value_num=50.0 + i,
                              quality=Quality.GOOD) for i in range(20)])
    now = base + 20 * 60
    dal.build_rollups(now)
    n1 = dal._conn.execute("SELECT COUNT(*) FROM samples_5m").fetchone()[0]
    # newest sample (i=19) lives in the still-open bucket -> not yet materialized
    latest_bucket = dal._conn.execute("SELECT MAX(bucket_utc) FROM samples_5m").fetchone()[0]
    assert latest_bucket < (now // 300) * 300
    # re-running builds no duplicates (INSERT OR REPLACE on the boundary bucket)
    dal.build_rollups(now)
    n2 = dal._conn.execute("SELECT COUNT(*) FROM samples_5m").fetchone()[0]
    assert n1 == n2


def test_point_labels_round_trip_and_survive_rediscovery(tmp_path):
    """An operator label has to outlive a re-scan. BACnet names are commissioning strings —
    on the tower panel the reading and its setpoint are SITE.CHW.HDR.SUP.TEMP and
    ...TEMP.SP, two characters apart — so the whole point of renaming is to tell them apart
    on a chart. `description` would have been the tempting place to put a note and is
    exactly wrong: upsert_point overwrites it on every discovery."""
    dal = _dal_with_point(tmp_path)

    def _p(**kw):
        return Point(source=PointSource.BACNET, device_id=1, object_type="analogInput",
                     object_instance=7, **kw)

    pid = dal.upsert_point(_p(system_id=1, name="SITE.CHW.HDR.SUP.TEMP",
                              description="HDR SUP TEMP", units_text="degrees-fahrenheit"))
    assert dal.set_point_labels(pid, display_name="Tower supply temp (ACTUAL)",
                                operator_note="not the setpoint — that is ...TEMP.SP")
    m = dal.point_meta(pid)
    assert m["display_name"] == "Tower supply temp (ACTUAL)"
    assert m["operator_note"].startswith("not the setpoint")
    assert m["name"] == "SITE.CHW.HDR.SUP.TEMP", "the real BACnet name must remain intact"

    # a re-discovery re-upserts the point with no knowledge of either field
    dal.upsert_point(_p(system_id=None, name="SITE.CHW.HDR.SUP.TEMP",
                        description="HDR SUP TEMP", units_text="degrees-fahrenheit"))
    m = dal.point_meta(pid)
    assert m["display_name"] == "Tower supply temp (ACTUAL)", "re-scan destroyed the rename"
    assert m["operator_note"], "re-scan destroyed the operator note"

    # each field is independently optional; "" clears back to the BACnet name
    dal.set_point_labels(pid, operator_note="just the note")
    assert dal.point_meta(pid)["display_name"] == "Tower supply temp (ACTUAL)"
    dal.set_point_labels(pid, display_name="")
    m = dal.point_meta(pid)
    assert m["display_name"] is None and m["operator_note"] == "just the note"
    assert dal.set_point_labels(9999, display_name="nope") is False   # unknown point

    # and the viewer's picker source carries them, so it can render the label
    dal.set_point_enabled(pid, True)
    dal.set_point_labels(pid, display_name="Tower supply temp (ACTUAL)")
    row = {p["id"]: p for p in dal.logged_points()}[pid]
    assert row["display_name"] == "Tower supply temp (ACTUAL)"
    assert row["operator_note"] == "just the note"


def test_logged_points_count_matches_raw_across_the_rollup_watermark(tmp_path):
    """logged_points() stopped doing `COUNT(*) GROUP BY point_id` over raw — that was a
    full 10M-row scan on every trend-viewer load (~10 s), on the event loop, so it stalled
    the collector too. It now sums the hourly rollup below its watermark and counts raw
    above it. That split is only safe if the two halves never double-count and never drop
    a sample, so pin the total against a direct count at each stage: no rollups at all,
    rollups built, and fresh samples arriving after the watermark."""
    dal = _dal_with_point(tmp_path)
    base = 1_000_020

    def good_total():
        return dal._conn.execute(
            "SELECT COUNT(*) FROM samples WHERE quality='good' AND value_num IS NOT NULL"
        ).fetchone()[0]

    def reported():
        return {p["id"]: p for p in dal.logged_points()}[1]

    # i=5 is a fault and i=11 is non-numeric: neither is chartable, so neither counts
    dal.write_samples([
        Sample(point_id=1, ts_utc=base + i * 60,
               value_num=(None if i == 11 else 50.0 + i),
               value_text=("offline" if i == 11 else None),
               quality=(Quality.FAULT if i == 5 else Quality.GOOD))
        for i in range(200)])

    # (a) no rollups yet -> watermark 0, everything counted from raw
    assert dal._conn.execute("SELECT COUNT(*) FROM samples_1h").fetchone()[0] == 0
    assert reported()["sample_count"] == good_total() == 198

    # (b) rollups built -> the count is now stitched from both tables, same answer
    now = base + 200 * 60
    dal.build_rollups(now)
    w = dal._conn.execute("SELECT MAX(bucket_utc) FROM samples_1h").fetchone()[0]
    assert w > 0, "the split is only exercised once a watermark exists"
    assert reported()["sample_count"] == good_total() == 198

    # (c) new samples land above the watermark -> the raw tail picks them up immediately,
    # without waiting for the next maintenance cycle to roll them up
    dal.write_samples([Sample(point_id=1, ts_utc=now + i * 60, value_num=70.0,
                              quality=Quality.GOOD) for i in range(1, 6)])
    assert reported()["sample_count"] == good_total() == 203

    # last_ts spans EVERY quality — it answers "when did this point last report", which
    # is the freshness signal; a point reporting only faults must not look disconnected.
    dal.write_samples([Sample(point_id=1, ts_utc=now + 999, value_num=None,
                              quality=Quality.COMM_LOSS)])
    r = reported()
    assert r["sample_count"] == 203 and r["last_ts"] == now + 999


def test_enforce_retention_prunes_old_raw_only(tmp_path):
    dal = _dal_with_point(tmp_path)
    now = 10_000_000
    old = now - 100 * 86400      # older than 90d
    new = now - 1 * 86400        # within window
    dal.write_samples([
        Sample(point_id=1, ts_utc=old, value_num=1.0, quality=Quality.GOOD),
        Sample(point_id=1, ts_utc=new, value_num=2.0, quality=Quality.GOOD),
    ])
    deleted = dal.enforce_retention(now, {"raw_days": 90})
    assert deleted.get("samples") == 1
    remaining = [r["ts_utc"] for r in dal.query_series(1, 0, now + 1)]
    assert remaining == [new]

    # a null/absent window keeps everything (no accidental purge)
    dal.write_samples([Sample(point_id=1, ts_utc=old, value_num=1.0, quality=Quality.GOOD)])
    assert dal.enforce_retention(now, {"raw_days": None}).get("samples") in (None, 0)
    assert len(dal.query_series(1, 0, now + 1)) == 2


def test_samples_time_extent_matches_min_max_without_count(tmp_path):
    dal = _dal_with_point(tmp_path)
    dal.write_samples([
        Sample(point_id=1, ts_utc=100, value_num=1.0, quality=Quality.GOOD),
        Sample(point_id=1, ts_utc=500, value_num=2.0, quality=Quality.GOOD),
    ])
    ext = dal.samples_time_extent()
    assert ext == {"min_ts": 100, "max_ts": 500}   # cheap extent: no COUNT(*)
    assert "n" not in ext


def test_retention_prunes_diagnostics_but_never_an_open_alert(tmp_path):
    """Only the three sample tables had retention windows, so poll_health (~3,450 rows/day)
    and alerts grew forever. Both are now pruned -- with one hard exception: an OPEN alert
    is a live condition and must survive at any age. Ageing out an unacknowledged fault is
    the single way this could lose something that matters."""
    from hvactrend.storage.sqlite_dal import SQLiteDAL

    dal = SQLiteDAL(tmp_path / "r.db")
    dal.apply_schema()
    c = dal._conn
    now = 1_800_000_000
    old = now - 400 * 86400          # older than every window below
    recent = now - 3 * 86400

    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'DEMO','Demo Building')")
    c.execute("INSERT INTO devices(id,site_id,device_instance,address,created_utc) "
              "VALUES (1,1,100100,'192.0.2.19',0)")
    for ts in (old, recent):
        c.execute("INSERT INTO poll_health(device_id,ts_utc,ok,reads_attempted,reads_ok) "
                  "VALUES (1,?,1,10,10)", (ts,))

    def alert(fp, status, resolved, created):
        c.execute("INSERT INTO alerts(kind,severity,system_code,message,fingerprint,status,"
                  "created_utc,last_seen_utc,resolved_utc,source) "
                  "VALUES ('deviation','high','AC-1','x',?,?,?,?,?,'prescreen')",
                  (fp, status, created, created, resolved))

    alert("old-resolved", "resolved", old, old)
    alert("new-resolved", "resolved", recent, recent)
    alert("old-OPEN", "open", None, old)          # ancient but still live

    got = dal.enforce_retention(now, {"poll_health_days": 30, "alerts_days": 180})

    assert got.get("poll_health") == 1
    assert [r[0] for r in c.execute("SELECT ts_utc FROM poll_health")] == [recent]

    kept = {r[0] for r in c.execute("SELECT fingerprint FROM alerts")}
    assert "old-resolved" not in kept, "a long-resolved alert should be pruned"
    assert "new-resolved" in kept
    assert "old-OPEN" in kept, "an OPEN alert must never be pruned, however old"

    # absent keys mean keep-forever, exactly as for the rollup tables
    assert "poll_health" not in dal.enforce_retention(now, {})
    dal.close() if hasattr(dal, "close") else None


def test_samples_extent_does_not_scan_the_whole_index(tmp_path):
    """MIN, MAX and COUNT in ONE select makes SQLite abandon the index shortcut and SCAN
    the whole covering index. Split into separate scalar subqueries the two extremes become
    O(log n) SEARCHes and only the COUNT walks anything.

    Measured on the live 16.6 M-row table: 1.563 s combined vs 0.352 s split, and that
    difference WAS the latency of /api/poll-health -- which runs on the same event loop the
    BACnet collector polls from. samples_time_extent() already used this form; this is the
    same lesson applied one function over."""
    from hvactrend.models import Quality, Sample
    from hvactrend.storage.sqlite_dal import SQLiteDAL

    dal = SQLiteDAL(tmp_path / "extent.db")
    dal.apply_schema()
    c = dal._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'DEMO','Demo Building')")
    c.execute("INSERT INTO buildings(id,site_id,code,name) VALUES (1,1,'MAIN','Main')")
    c.execute("INSERT INTO systems(id,building_id,code,name,kind) "
              "VALUES (1,1,'AC-1','AC-1','ahu')")
    c.execute("INSERT INTO devices(id,site_id,device_instance,address,created_utc) "
              "VALUES (1,1,100100,'192.0.2.19',0)")
    for pid in (1, 2):
        c.execute("INSERT INTO points(id,device_id,system_id,source,object_type,"
                  "object_instance,name,enabled,created_utc) "
                  "VALUES (?,1,1,'bacnet','analog-input',?,?,1,0)", (pid, pid, "p%d" % pid))
    dal.write_samples([Sample(point_id=pid, ts_utc=1000 + i * 60, value_num=float(i),
                              quality=Quality.GOOD)
                       for pid in (1, 2) for i in range(50)])

    e = dal.samples_extent()
    assert e == {"min_ts": 1000, "max_ts": 1000 + 49 * 60, "n": 100}, e

    # per-point path still works, and an unknown point is empty rather than an error
    assert dal.samples_extent(point_id=1)["n"] == 50
    assert dal.samples_extent(point_id=999) == {"min_ts": None, "max_ts": None, "n": 0}

    # the shape that matters: neither extreme may be answered by a full scan
    plan = " | ".join(
        r[-1] for r in c.execute(
            "EXPLAIN QUERY PLAN SELECT (SELECT MIN(ts_utc) FROM samples) AS min_ts, "
            "(SELECT MAX(ts_utc) FROM samples) AS max_ts, "
            "(SELECT COUNT(*) FROM samples) AS n"))
    assert "SEARCH" in plan, plan
    combined = " | ".join(
        r[-1] for r in c.execute(
            "EXPLAIN QUERY PLAN SELECT MIN(ts_utc), MAX(ts_utc), COUNT(*) FROM samples"))
    assert "SCAN" in combined, \
        "if the combined form stopped scanning, this guard is no longer meaningful: " + combined


def test_poll_health_counts_the_samples_off_the_event_loop(tmp_path, monkeypatch):
    """/api/poll-health's exact COUNT(*) walks the whole index: MEASURED 10-23 s at 43.8 M
    rows (2026-10-20), and it ran on the loop the BACnet collector polls from. It must run in
    a worker thread on its own read-only connection, never on the collector's."""
    import asyncio

    import pytest
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from hvactrend.api.app import create_app
    from hvactrend.storage import sqlite_dal

    db = tmp_path / "ph.db"
    dal = SQLiteDAL(db)
    dal.apply_schema()
    c = dal._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'DEMO','Demo Building')")
    c.execute("INSERT INTO buildings(id,site_id,code,name) VALUES (1,1,'MAIN','Main')")
    c.execute("INSERT INTO systems(id,building_id,code,name,kind) "
              "VALUES (1,1,'AC-1','AC-1','ahu')")
    c.execute("INSERT INTO devices(id,site_id,device_instance,address,created_utc) "
              "VALUES (1,1,100100,'192.0.2.19',0)")
    for pid in (1, 2):
        c.execute("INSERT INTO points(id,device_id,system_id,source,object_type,"
                  "object_instance,name,enabled,created_utc) "
                  "VALUES (?,1,1,'bacnet','analog-input',?,?,1,0)", (pid, pid, "p%d" % pid))
    dal.write_samples([Sample(point_id=pid, ts_utc=1000 + i * 60, value_num=float(i),
                              quality=Quality.GOOD)
                       for pid in (1, 2) for i in range(50)])
    dal.close()

    where = []
    real = sqlite_dal.samples_extent_readonly

    def spy(path):
        try:
            asyncio.get_running_loop()
            where.append("on the event loop")
        except RuntimeError:
            where.append("worker thread")
        return real(path)

    monkeypatch.setattr(sqlite_dal, "samples_extent_readonly", spy)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("site: {code: DEMO, name: Demo Building}\n"
                   "bacnet:\n  local_address: '<COLLECTOR_IP>/24'\n"
                   "  device_instance: '<LOGGER_DEVICE_INSTANCE>'\n"
                   "  poll: {interval_s: 120}\n"
                   "  safety: {allow_writes: false, allow_cov: false}\n"
                   f"storage: {{path: '{db.as_posix()}'}}\n", encoding="utf-8")
    app = create_app(str(cfg), enable_bacnet=False)
    with TestClient(app) as tc:
        app.state.dal.samples_extent = \
            lambda *a, **k: where.append("the collector's connection") or {}
        r = tc.get("/api/poll-health").json()
    assert where == ["worker thread"], where
    assert (r["samples"], r["oldest_sample_utc"], r["newest_sample_utc"]) == \
        (100, 1000, 1000 + 49 * 60), r
