"""Schema tests. Stdlib sqlite3 only — no external deps.

Proves db/schema.sql applies cleanly and the long/narrow time-series round-trips
with status/quality captured, including idempotent (point_id, ts_utc) writes.
"""

from hvactrend.models import Point, PointSource, Quality, Sample
from hvactrend.storage.sqlite_dal import SQLiteDAL


def _fresh_dal(tmp_path):
    dal = SQLiteDAL(tmp_path / "t.db")
    dal.apply_schema()
    return dal


def test_schema_applies(tmp_path):
    dal = _fresh_dal(tmp_path)
    tables = {
        r["name"]
        for r in dal._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    expected = {
        "sites", "buildings", "systems", "devices", "points", "point_enum_states",
        "samples", "samples_5m", "samples_1h", "equipment_meta", "reports",
        "findings", "annotations", "tags", "point_tags", "system_tags", "views",
        "poll_health", "settings", "app_meta", "schema_migrations",
    }
    missing = expected - tables
    assert not missing, f"missing tables: {missing}"
    dal.close()


def test_sample_roundtrip_with_status(tmp_path):
    dal = _fresh_dal(tmp_path)
    # minimal parent rows for the FK chain
    c = dal._conn
    c.execute("INSERT INTO sites(id, code, name) VALUES (1,'DEMO','Demo Building')")
    c.execute("INSERT INTO buildings(id, site_id, code, name) VALUES (1,1,'B1','Main')")
    c.execute("INSERT INTO systems(id, building_id, code, name) VALUES (1,1,'AHU-1','AHU 1')")
    c.execute("INSERT INTO devices(id, site_id, device_instance, created_utc) VALUES (1,1,1001,0)")
    c.execute(
        "INSERT INTO points(id, device_id, system_id, source, object_type, "
        "object_instance, name, created_utc) VALUES "
        "(1,1,1,'bacnet','analogInput',1,'DAT',0)"
    )

    n = dal.write_samples([
        Sample(point_id=1, ts_utc=1000, value_num=55.2, status_flags=0,
               reliability=0, out_of_service=False, quality=Quality.GOOD),
        # the -58 degF sentinel case: fault quality captured, not silently stored as good
        Sample(point_id=1, ts_utc=1120, value_num=-58.0, status_flags=1,
               reliability=7, out_of_service=False, quality=Quality.FAULT),
    ])
    assert n == 2

    latest = dal.latest_value(1)
    assert latest is not None
    assert latest.ts_utc == 1120
    assert latest.quality == Quality.FAULT
    assert latest.value_num == -58.0

    # idempotent on (point_id, ts_utc): re-writing the same key does not duplicate
    dal.write_samples([Sample(point_id=1, ts_utc=1000, value_num=55.3)])
    rows = dal.query_series(1, 0, 2000)
    assert len(rows) == 2, "duplicate (point_id, ts_utc) must not create extra rows"
    dal.close()


def test_list_enabled_points(tmp_path):
    dal = _fresh_dal(tmp_path)
    c = dal._conn
    c.execute("INSERT INTO sites(id, code, name) VALUES (1,'DEMO','Demo Building')")
    c.execute("INSERT INTO buildings(id, site_id, code, name) VALUES (1,1,'B1','Main')")
    c.execute("INSERT INTO devices(id, site_id, device_instance, created_utc) VALUES (1,1,1001,0)")
    c.execute("INSERT INTO points(id, device_id, source, object_type, object_instance, "
              "enabled, created_utc) VALUES (1,1,'bacnet','analogInput',1,1,0)")
    c.execute("INSERT INTO points(id, device_id, source, object_type, object_instance, "
              "enabled, created_utc) VALUES (2,1,'bacnet','analogInput',2,0,0)")
    pts = dal.list_points(enabled_only=True)
    assert [p.id for p in pts] == [1]
    assert pts[0].source == PointSource.BACNET
    dal.close()
