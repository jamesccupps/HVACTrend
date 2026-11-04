"""Weather ingestion tests — psychrometrics + Open-Meteo parse + storage.
Hermetic: the HTTP getter is injected, so no network call."""

import calendar

from hvactrend.ai import weather
from hvactrend.ai.weather import (dewpoint_f, enthalpy_btu, fetch_open_meteo,
                                     update_weather, wet_bulb_f)
from hvactrend.storage.sqlite_dal import SQLiteDAL


def test_psychrometrics_known_points():
    # 90 F / 40% RH: wet-bulb ~71-72 F, dewpoint ~62 F (standard psychrometric chart)
    assert 69.0 <= wet_bulb_f(90.0, 40.0) <= 73.0
    assert 60.0 <= dewpoint_f(90.0, 40.0) <= 64.0
    # saturated: wet-bulb ≈ dry-bulb, dewpoint ≈ dry-bulb
    assert abs(wet_bulb_f(75.0, 100.0) - 75.0) < 1.5
    assert abs(dewpoint_f(75.0, 100.0) - 75.0) < 1.0
    # drier air -> wet-bulb well below dry-bulb; enthalpy rises with moisture
    assert wet_bulb_f(90.0, 20.0) < wet_bulb_f(90.0, 60.0)
    assert enthalpy_btu(80.0, 70.0) > enthalpy_btu(80.0, 30.0)
    # ABSOLUTE anchor on the 0 F datum: standard psychrometric-chart enthalpy at
    # 75 F / 50% RH is ~28.1 Btu/lb. This must match the ASHRAE-90.1 fixed-enthalpy
    # economizer changeover (28 Btu/lb) it is compared against in ai/derived.py — a
    # 0 C-datum value would read ~20.4 here and silently break the changeover.
    assert 27.5 <= enthalpy_btu(75.0, 50.0) <= 28.7
    assert 33.5 <= enthalpy_btu(82.0, 58.0) <= 35.5   # warm/humid -> above changeover
    # None-safe
    assert wet_bulb_f(None, 50.0) is None and dewpoint_f(70.0, None) is None


class _FakeResp:
    """Injected http_get returns two hourly obs (one future, to be skipped)."""
    payload = {
        "hourly": {
            "time": ["2026-07-16T10:00", "2026-07-16T11:00", "2099-01-01T00:00"],
            "temperature_2m": [68.0, 72.0, 99.0],
            "relative_humidity_2m": [55.0, 50.0, 10.0],
            "wind_speed_10m": [6.0, 8.0, 3.0],
            "shortwave_radiation": [420.0, 610.0, 0.0],
            "surface_pressure": [1012.0, 1011.0, 1000.0],
        }
    }


def _fake_get(url, params):
    return _FakeResp.payload


def test_fetch_open_meteo_parse():
    rows = fetch_open_meteo(43.66, -70.26, http_get=_fake_get)
    assert len(rows) == 3
    assert rows[0]["ts_utc"] == calendar.timegm((2026, 7, 16, 10, 0, 0, 0, 0, 0))
    assert rows[0]["oat_f"] == 68.0 and rows[0]["rh_pct"] == 55.0
    assert rows[0]["solar_wm2"] == 420.0 and rows[0]["source"] == "open-meteo"


def test_update_weather_stores_and_derives(tmp_path):
    dal = SQLiteDAL(tmp_path / "w.db")
    dal.apply_schema()

    from hvactrend.config import _Node
    cfg = _Node({"weather": {"open_meteo": {"latitude": 43.66, "longitude": -70.26}}})

    now = calendar.timegm((2026, 7, 16, 12, 0, 0, 0, 0, 0))
    res = update_weather(dal, cfg, http_get=_fake_get, now_utc=now)
    assert res["stored"] == 2               # the 2099 future hour skipped

    rng = dal.weather_range(0, now + 10_000)
    assert len(rng) == 2
    assert rng[0]["wet_bulb_f"] is not None and rng[0]["dewpoint_f"] is not None
    assert rng[0]["enthalpy"] is not None

    w = dal.weather_window(0, now + 10_000)
    assert w["n"] == 2 and w["oat_min"] == 68.0 and w["oat_max"] == 72.0
    assert w["wb_avg"] is not None


def test_update_weather_no_coords():
    dal = SQLiteDAL(":memory:")
    dal.apply_schema()
    res = update_weather(dal, None, http_get=_fake_get, now_utc=0)
    assert res["stored"] == 0 and "config" in res["error"]
    dal.close()
