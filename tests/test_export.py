"""Bulk data export (export.py, POST /api/export), 2026-10-20.

The operator: "there should be a way to just select points and dates and everything to export".
The chart's CSV holds what a chart holds: 64 points, and every 2-minute reading only for a
window of 2 days or less."""

import asyncio
import csv
import io
import time
import zipfile

import pytest

from hvactrend import export
from hvactrend.models import Quality, Sample
from hvactrend.storage.sqlite_dal import SQLiteDAL


def _midnight(y, m, d):
    return int(time.mktime((y, m, d, 0, 0, 0, 0, 0, -1)))


D1 = _midnight(2026, 10, 5)
D2 = _midnight(2026, 10, 6)
D3 = _midnight(2026, 10, 7)


def _db(path, n_extra=0):
    """Two controllers polled 5 s apart, as the collector staggers them: the tower panel's
    supply temp and fan status, and AC-1's discharge air. Two full days at 2 minutes."""
    dal = SQLiteDAL(path)
    dal.apply_schema()
    c = dal._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'DEMO','Demo Building')")
    c.execute("INSERT INTO buildings(id,site_id,code,name) VALUES (1,1,'MAIN','Main')")
    c.execute("INSERT INTO systems(id,building_id,code,name,kind) VALUES (1,1,'CT','CT','plant')")
    c.execute("INSERT INTO systems(id,building_id,code,name,kind) VALUES (2,1,'AC-1','AC-1','ahu')")
    for dev, inst in ((1, 100300), (2, 100100)):
        c.execute("INSERT INTO devices(id,site_id,device_instance,address,created_utc) "
                  "VALUES (?,1,?,'192.0.2.9',0)", (dev, inst))
    pts = [(1, 1, 1, "analog-input", "SITE.CT.SUP.TEMP", "CT Supply Temp"),
           (2, 1, 1, "binary-input", "SITE.CT1.FAN.STS", None),
           (3, 2, 2, "analog-input", "Discharge Air Temp|dac-3", None)]
    pts += [(10 + k, 1, 1, "analog-value", f"EXTRA.{k}", None) for k in range(n_extra)]
    for pid, dev, sid, ot, nm, dn in pts:
        c.execute("INSERT INTO points(id,device_id,system_id,source,object_type,object_instance,"
                  "name,display_name,enabled,created_utc) VALUES (?,?,?,'bacnet',?,?,?,?,1,0)",
                  (pid, dev, sid, ot, pid, nm, dn))
    rows = []
    for t in range(D1, D3, 120):
        bad = D1 + 3600 <= t < D1 + 7200           # the tower panel faulted for an hour
        rows += [Sample(point_id=1, ts_utc=t, value_num=None if bad else 75.0,
                        quality=Quality.COMM_LOSS if bad else Quality.GOOD),
                 Sample(point_id=2, ts_utc=t, value_num=1.0, value_text="active",
                        quality=Quality.GOOD),
                 Sample(point_id=3, ts_utc=t + 5, value_num=55.0 + (t - D1) % 3600 / 1800,
                        quality=Quality.GOOD)]
    dal.write_samples(rows)
    return dal


def _csvs(path):
    with zipfile.ZipFile(path) as zf:
        return {n: list(csv.reader(io.TextIOWrapper(zf.open(n), encoding="utf-8-sig")))
                for n in zf.namelist()}


def test_local_days_split_at_local_midnight():
    assert export.local_days(D1, D3) == [(D1, D2, "2026-10-05"), (D2, D3, "2026-10-06")]
    assert export.local_days(D1 + 3600, D2) == [(D1 + 3600, D2, "2026-10-05")]
    assert export.local_days(D1, D1 + 7200) == [(D1, D1 + 7200, "2026-10-05")]   # ends mid-day


def test_a_file_per_day_with_every_reading_and_its_quality(tmp_path):
    _db(tmp_path / "e.db").close()
    out = tmp_path / "x.zip"
    info = export.write_export(str(tmp_path / "e.db"), [1, 2, 3], D1, D3, "raw", "day", str(out))
    assert info == {"filename": "hvactrend_export_2026-10-05_to_2026-10-06_2min.zip",
                    "zip": True, "points": 3, "rows": 1440, "weather_hours": 0, "days": 2,
                    "missing": []}
    files = _csvs(out)
    assert sorted(files) == ["hvactrend_2026-10-05_2min.csv", "hvactrend_2026-10-06_2min.csv"]
    day = files["hvactrend_2026-10-05_2min.csv"]
    assert day[0] == ["timestamp_iso", "timestamp_utc", "timestamp_local", "resolution",
                      "CT Supply Temp [CT SITE.CT.SUP.TEMP]",
                      "CT Supply Temp [CT SITE.CT.SUP.TEMP] quality",
                      "CT SITE.CT1.FAN.STS", "CT SITE.CT1.FAN.STS quality",
                      "AC-1 Discharge Air Temp|dac-3", "AC-1 Discharge Air Temp|dac-3 quality"]
    # one row per poll cycle, although AC-1's controller answered 5 s after the tower's
    assert len(day) == 1 + 720
    first = day[1]
    assert first[1:4] == [str(D1), time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(D1)),
                          "2-min polls"]
    assert first[4:] == ["75.0", "good", "active", "good", "55.0", "good"]
    faulted = day[1 + 30]                                  # 01:00, inside the bad hour
    assert faulted[4:6] == ["", "comm-loss"], "a fault must not read as a value"


def test_hourly_averages_in_one_file_even_before_the_rollups_are_built(tmp_path):
    """The rollup tables are built hourly by the collector; an export of the last hour, or of
    a database that has never been rolled up, buckets the 2-minute readings itself."""
    _db(tmp_path / "h.db").close()
    out = tmp_path / "h.csv"
    info = export.write_export(str(tmp_path / "h.db"), [1, 3], D1, D2, "1h", "one", str(out))
    assert info["filename"] == "hvactrend_export_2026-10-05_hourly.csv" and info["rows"] == 24
    rows = list(csv.reader(open(out, encoding="utf-8-sig")))
    assert rows[0][4:9] == [f"CT Supply Temp [CT SITE.CT.SUP.TEMP] {k}"
                            for k in ("avg", "min", "max", "n", "n_total")]
    by_hour = {int(r[1]): r for r in rows[1:]}
    assert by_hour[D1][4:9] == ["75.0", "75.0", "75.0", "30", "30"]
    assert by_hour[D1 + 3600][4:9] == ["", "", "", "0", "30"], "recorded, and faulted throughout"
    # AC-1 climbs 55.0 -> 56.93 over the hour in 1/15 °F steps: avg 55.97
    avg, lo, hi, n, nt = by_hour[D1][9:14]
    assert (float(avg), float(lo), float(hi), n, nt) == \
        (pytest.approx(55.0 + 14.5 / 15), 55.0, pytest.approx(55.0 + 29 / 15), "30", "30")


def _app(tmp_path, n_extra=0):
    pytest.importorskip("fastapi")
    from hvactrend.api.app import create_app
    db = tmp_path / "a.db"
    _db(db, n_extra).close()
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("site: {code: DEMO, name: Demo Building}\n"
                   "bacnet:\n  local_address: '<COLLECTOR_IP>/24'\n"
                   "  device_instance: '<LOGGER_DEVICE_INSTANCE>'\n"
                   "  poll: {interval_s: 120}\n"
                   "  safety: {allow_writes: false, allow_cov: false}\n"
                   f"storage: {{path: '{db.as_posix()}'}}\n", encoding="utf-8")
    return create_app(str(cfg), enable_bacnet=False)


def test_the_export_endpoint_writes_off_the_event_loop_and_says_what_it_wrote(tmp_path,
                                                                             monkeypatch):
    from fastapi.testclient import TestClient
    where = []
    real = export.write_export

    def spy(*a, **kw):
        try:
            asyncio.get_running_loop()
            where.append("on the event loop")
        except RuntimeError:
            where.append("worker thread")
        return real(*a, **kw)
    monkeypatch.setattr(export, "write_export", spy)
    app = _app(tmp_path)
    body = {"ids": [1, 2, 3, 3], "start": D1, "end": D3, "resolution": "raw", "layout": "day"}
    with TestClient(app) as c:
        r = c.post("/api/export", json=body)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "application/zip"
        assert 'filename="hvactrend_export_2026-10-05_to_2026-10-06_2min.zip"' in \
            r.headers["content-disposition"]
        assert (r.headers["x-export-points"], r.headers["x-export-rows"]) == ("3", "1440")
        assert len(zipfile.ZipFile(io.BytesIO(r.content)).namelist()) == 2
        assert c.post("/api/export", json={**body, "layout": "one"}).headers[
            "content-type"].startswith("text/csv")
        # refused with the numbers rather than quietly coarsened or truncated
        r = c.post("/api/export", json={**body, "end": D1 + 400 * 86400 * 1000})
        assert r.status_code == 413 and "million readings" in r.json()["detail"]
        assert c.post("/api/export", json={**body, "resolution": "2m"}).status_code == 400
        assert c.post("/api/export", json={**body, "ids": []}).status_code == 400
    assert where == ["worker thread", "worker thread"]


def test_a_remote_viewer_can_export(tmp_path):
    """The export is part of the trend viewer, the one page a VLAN client is served."""
    from fastapi.testclient import TestClient
    with TestClient(_app(tmp_path), client=("192.0.2.77", 51234)) as rc:
        r = rc.post("/api/export", json={"ids": [1], "start": D1, "end": D2,
                                         "resolution": "1h", "layout": "one"})
        assert r.status_code == 200 and r.text.count("\n") == 25


def test_the_chart_says_when_it_holds_fewer_points_than_were_picked(tmp_path):
    """84 tower points: the 20 past the chart's 64 used to vanish from the chart and its CSV
    without a word."""
    from fastapi.testclient import TestClient
    with TestClient(_app(tmp_path, n_extra=70)) as c:
        ids = [1, 2, 3] + [10 + k for k in range(70)]
        d = c.post("/api/series", json={"ids": ids, "start": D1, "end": D1 + 3600}).json()
        assert len(d["series"]) == 64 and d["dropped"] == 9
        d = c.post("/api/series", json={"ids": [1, 2], "start": D1, "end": D1 + 3600}).json()
        assert d["dropped"] == 0


# ── weather (2026-10-23: "add a way to export tempest weather ... all weather data
#    together") ──────────────────────────────────────────────────────────────────────────
def _weather(dal):
    """Tempest snapshots for every hour of D1 but 03:00, and the tower panel's own outdoor
    sensor reading 4 °F warmer, as the Siemens one does."""
    c = dal._conn
    c.execute("INSERT INTO points(id,device_id,system_id,source,object_type,object_instance,"
              "name,display_name,units_text,enabled,created_utc) VALUES "
              "(4,1,1,'bacnet','analog-input',4,'SITE.BLDG.OAT','Outdoor Temp (Siemens)',"
              "'degrees-fahrenheit',1,0)")
    dal.write_samples([Sample(point_id=4, ts_utc=t, value_num=64.0, quality=Quality.GOOD)
                       for t in range(D1, D2, 120)])
    for h in range(24):
        if h == 3:
            continue
        c.execute("INSERT INTO weather(ts_utc, oat_f, rh_pct, wet_bulb_f, dewpoint_f, enthalpy, "
                  "wind_mph, solar_wm2, pressure_mb, source, created_utc) VALUES "
                  "(?,60.0,80.0,56.5,54.0,24.0,3.0,0.0,1010.0,'tempest',?)",
                  (D1 + h * 3600, D1 + h * 3600 + 1200))


def test_the_weather_alone_is_one_hourly_file_with_the_buildings_own_sensors(tmp_path):
    dal = _db(tmp_path / "w.db")
    _weather(dal)
    dal.close()
    out = tmp_path / "w.csv"
    info = export.write_export(str(tmp_path / "w.db"), [], D1, D2, "raw", "day", str(out),
                               weather=True)
    assert (info["filename"], info["zip"], info["weather_hours"]) == \
        ("hvactrend_weather_2026-10-05_hourly.csv", False, 24)
    rows = list(csv.reader(open(out, encoding="utf-8-sig")))
    hdr = rows[0]
    assert hdr[:5] == ["hour_utc_iso", "hour_utc", "hour_local", "tempest_observed_local",
                       "tempest_outdoor_air_f"]
    # the building's sensor sits beside the Tempest; a damper or a supply temp does not
    assert hdr[13:] == ["Outdoor Temp (Siemens) [CT SITE.BLDG.OAT] hourly avg (°F)"]
    first = rows[1]
    assert first[2:5] == [time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(D1)),
                          time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(D1 + 1200)), "60.0"]
    assert first[12:] == ["tempest", "64.0"]
    # an hour the Tempest missed stays in the grid, marked, with the building's reading
    assert rows[4][3:13] == [""] * 9 + ["missing"] and rows[4][13] == "64.0"


def test_the_weather_with_points_comes_as_a_zip_even_for_one_file(tmp_path):
    dal = _db(tmp_path / "z.db")
    _weather(dal)
    dal.close()
    out = tmp_path / "z.zip"
    info = export.write_export(str(tmp_path / "z.db"), [1, 3], D1, D2, "raw", "one", str(out),
                               weather=True)
    assert info["zip"] and info["rows"] == 720 and info["weather_hours"] == 24
    assert sorted(_csvs(out)) == ["hvactrend_export_2026-10-05_2min.csv",
                                  "hvactrend_weather_2026-10-05_hourly.csv"]
    info = export.write_export(str(tmp_path / "z.db"), [1], D1, D3, "raw", "day", str(out),
                               weather=True)
    assert "hvactrend_weather_2026-10-05_to_2026-10-06_hourly.csv" in _csvs(out)


def test_the_endpoint_exports_the_weather_on_its_own(tmp_path):
    from fastapi.testclient import TestClient
    app = _app(tmp_path)
    with TestClient(app) as c:
        body = {"ids": [], "start": D1, "end": D2, "resolution": "raw", "layout": "day"}
        assert c.post("/api/export", json=body).status_code == 400      # nothing picked
        r = c.post("/api/export", json={**body, "weather": True})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
        assert r.headers["x-export-weather-hours"] == "24"
        assert 'filename="hvactrend_weather_2026-10-05_hourly.csv"' in r.headers["content-disposition"]
