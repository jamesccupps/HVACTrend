"""Weather ingestion + psychrometrics.

HVAC performance can't be judged without outdoor conditions — and **wet-bulb**, not
dry-bulb, is the real cooling/economizer driver (dry-bulb misleads in humid weather).
So rather than depend on a station to report wet-bulb, we compute wet-bulb / dewpoint /
enthalpy ourselves from temperature + RH; any source then yields the same derived set.

Default source is **Open-Meteo** (free, no API key, hourly, includes solar radiation).
An on-site Tempest could be added behind the same `fetch_*` seam later. The HTTP getter
is injectable so the parse + psychrometrics are tested without a network call.

Read-only: fetches public weather + writes the local `weather` table.
"""

from __future__ import annotations

import calendar
import math
import os
import time
from datetime import datetime
from typing import Any, Callable, Optional

_OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
_TEMPEST_REST = "https://swd.weatherflow.com/swd/rest"


# ── psychrometrics (inputs °F / RH% ; source-independent) ────────────────────
def _c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def _f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def wet_bulb_f(temp_f: Optional[float], rh: Optional[float]) -> Optional[float]:
    """Wet-bulb (°F) via Stull's 2011 empirical fit — good to a few tenths °C for
    RH 5–99%, typical surface conditions."""
    if temp_f is None or rh is None:
        return None
    t = _c(temp_f)
    r = max(1.0, min(100.0, rh))
    tw = (t * math.atan(0.151977 * math.sqrt(r + 8.313659))
          + math.atan(t + r) - math.atan(r - 1.676331)
          + 0.00391838 * r ** 1.5 * math.atan(0.023101 * r) - 4.686035)
    return round(_f(tw), 1)


def dewpoint_f(temp_f: Optional[float], rh: Optional[float]) -> Optional[float]:
    """Dewpoint (°F) via the Magnus formula."""
    if temp_f is None or rh is None:
        return None
    t = _c(temp_f)
    r = max(1.0, min(100.0, rh))
    a, b = 17.27, 237.7
    alpha = (a * t / (b + t)) + math.log(r / 100.0)
    return round(_f(b * alpha / (a - alpha)), 1)


def enthalpy_btu(temp_f: Optional[float], rh: Optional[float],
                 pressure_mb: Optional[float] = 1013.25) -> Optional[float]:
    """Moist-air enthalpy (Btu per lb dry air) from temp + RH (+ station pressure)."""
    if temp_f is None or rh is None:
        return None
    t = _c(temp_f)
    r = max(1.0, min(100.0, rh))
    p_kpa = (pressure_mb or 1013.25) / 10.0
    pws = 0.6108 * math.exp(17.27 * t / (t + 237.3))     # kPa, Tetens
    pw = r / 100.0 * pws
    w = 0.62198 * pw / max(p_kpa - pw, 1e-6)             # humidity ratio kg/kg (== lb/lb)
    # ASHRAE IP moist-air enthalpy on the 0 °F dry-air datum (Btu per lb dry air).
    # NOTE: must stay on the 0 °F datum — it is compared directly against the
    # 28 Btu/lb ASHRAE-90.1 fixed-enthalpy economizer changeover in ai/derived.py.
    # (A 0 °C-datum SI value converted by *0.42992 reads ~7.7 Btu/lb low and would
    # push the effective changeover to ~35.6 Btu/lb — see the psychrometrics fix.)
    return round(0.240 * temp_f + w * (1061.0 + 0.444 * temp_f), 1)


def derive(row: dict) -> dict:
    """Attach computed wet-bulb / dewpoint / enthalpy to a raw {oat_f, rh_pct, ...} row."""
    t, rh = row.get("oat_f"), row.get("rh_pct")
    row["wet_bulb_f"] = wet_bulb_f(t, rh)
    row["dewpoint_f"] = dewpoint_f(t, rh)
    row["enthalpy"] = enthalpy_btu(t, rh, row.get("pressure_mb"))
    return row


# ── Open-Meteo provider ──────────────────────────────────────────────────────
def _http_get(url: str, params: dict) -> Any:
    import httpx  # lazy: keeps the module import dependency-free
    r = httpx.get(url, params=params, timeout=15.0)
    r.raise_for_status()
    return r.json()


def fetch_open_meteo(lat: float, lon: float, *, past_days: int = 2,
                     http_get: Callable = _http_get) -> list[dict]:
    """Fetch recent hourly weather for (lat, lon). Returns raw rows (no derived
    fields yet) sorted by time. `http_get(url, params)->json` is injectable."""
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": ("temperature_2m,relative_humidity_2m,wind_speed_10m,"
                   "shortwave_radiation,surface_pressure"),
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
        "past_days": past_days, "forecast_days": 1, "timezone": "UTC",
    }
    data = http_get(_OPEN_METEO, params)
    h = (data or {}).get("hourly") or {}
    times = h.get("time") or []
    out = []
    for i, iso in enumerate(times):
        out.append({
            "ts_utc": _iso_to_epoch(iso),
            "oat_f": _at(h.get("temperature_2m"), i),
            "rh_pct": _at(h.get("relative_humidity_2m"), i),
            "wind_mph": _at(h.get("wind_speed_10m"), i),
            "solar_wm2": _at(h.get("shortwave_radiation"), i),
            "pressure_mb": _at(h.get("surface_pressure"), i),
            "source": "open-meteo",
        })
    return [r for r in out if r["ts_utc"] is not None]


# ── Tempest (WeatherFlow) cloud provider ─────────────────────────────────────
def _num(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _c_to_f(c: Any) -> Optional[float]:
    v = _num(c)
    return None if v is None else round(v * 9.0 / 5.0 + 32.0, 2)


def _ms_to_mph(ms: Any) -> Optional[float]:
    v = _num(ms)
    return None if v is None else round(v * 2.2369362920544, 1)


def fetch_tempest_cloud(station_id: Any, token: str, *,
                        http_get: Callable = _http_get) -> list[dict]:
    """Fetch the latest station observation from the WeatherFlow (Tempest) cloud REST
    API, returned in the SAME raw-row shape as fetch_open_meteo (SI -> °F/mph, STATION
    pressure, hour-floored ts). `http_get(url, params)->json` is injectable for tests.

    Tempest reports SI and does NOT pre-compute wet-bulb — derive() handles that. The
    station endpoint gives named obs fields; using station (not sea-level) pressure keeps
    the enthalpy honest."""
    url = f"{_TEMPEST_REST}/observations/station/{station_id}"
    data = http_get(url, {"token": token})
    out = []
    for o in (data or {}).get("obs") or []:
        ts = o.get("timestamp")
        if ts is None:
            continue
        out.append({
            "ts_utc": int(ts) - int(ts) % 3600,          # hour-align (weather PK is hourly)
            "oat_f": _c_to_f(o.get("air_temperature")),
            "rh_pct": _num(o.get("relative_humidity")),
            "wind_mph": _ms_to_mph(o.get("wind_avg")),
            "solar_wm2": _num(o.get("solar_radiation")),
            "pressure_mb": _num(o.get("station_pressure")),
            "source": "tempest",
        })
    return [r for r in out if r["ts_utc"] is not None]


def _tempest_cfg(config: Any) -> Optional[dict]:
    """{enabled, mode, station_id} from config.weather.tempest, or None if absent."""
    try:
        w = config.get("weather") if config else None
        t = w.get("tempest") if (w is not None and hasattr(w, "get")) else None
        if t is None or not hasattr(t, "get"):
            return None
        cloud = t.get("cloud")
        sid = cloud.get("station_id") if (cloud is not None and hasattr(cloud, "get")) else None
        return {"enabled": bool(t.get("enabled", False)),
                "mode": t.get("mode", "cloud"), "station_id": sid}
    except (AttributeError, TypeError, ValueError):
        return None


def update_weather(dal: Any, config: Any = None, *, http_get: Callable = _http_get,
                   now_utc: Optional[int] = None) -> dict:
    """Fetch + derive + store hourly weather up to now. Idempotent (upsert on ts_utc).
    Uses the on-site Tempest (WeatherFlow cloud) when weather.tempest.enabled, else
    Open-Meteo from config coordinates. Returns a summary.

    The two halves are separate so a server can run the network half in a worker thread:
    on a BAS network with no internet, a GET waits out DNS and the connect timeout, and on
    the event loop that froze polling and every page for 15-25 s an hour (2026-11-06
    review, M4). The database half stays where the connection lives."""
    return store_weather(dal, fetch_weather(config, http_get=http_get), now_utc=now_utc)


def _fetch_error(exc: BaseException, secret: Optional[str]) -> str:
    """An error for the log and the page, without the Tempest token. httpx puts the full
    URL in an HTTPStatusError, query string and all, and the token rides in the query: a
    revoked token, a 429 or a 5xx wrote it into the log every hour (M3)."""
    try:
        import httpx
        if isinstance(exc, httpx.HTTPStatusError):
            return f"HTTP {exc.response.status_code}"
    except ImportError:
        pass
    text = f"{type(exc).__name__}: {exc}"
    if secret:
        from urllib.parse import quote
        for form in {secret, quote(secret, safe="")}:
            text = text.replace(form, "***")
    return text[:300]


def fetch_weather(config: Any = None, *, http_get: Callable = _http_get) -> dict:
    """The network half: {"rows": [...], "source": s}, or {"stored": 0, "error": ...}.
    Touches no database, so it may run in any thread."""
    tcfg = _tempest_cfg(config)
    if tcfg and tcfg["enabled"] and tcfg["mode"] == "cloud":
        token = os.environ.get("TEMPEST_TOKEN")
        if not token:
            return {"stored": 0, "error": "weather.tempest cloud enabled but TEMPEST_TOKEN not set"}
        if not tcfg["station_id"]:
            return {"stored": 0, "error": "weather.tempest.cloud.station_id not set"}
        source = "tempest"
    else:
        lat, lon = _coords(config)
        if lat is None or lon is None:
            return {"stored": 0, "error": "no weather.latitude/longitude in config"}
        source = "open-meteo"

    # The network fetch (httpx) raises on any timeout / connection error / non-2xx —
    # common on a keyless API polled hourly (transient 429/503). Keep it inside the
    # "errors are returned, not raised" contract the callers rely on (scripts/fetch_weather
    # branches on res['error']; /api/weather/refresh returns this dict) so a weather blip
    # never crashes the scheduled task or the endpoint with a traceback.
    try:
        if source == "tempest":
            rows = fetch_tempest_cloud(tcfg["station_id"], token, http_get=http_get)
        else:
            rows = fetch_open_meteo(lat, lon, http_get=http_get)
    except Exception as exc:  # noqa: BLE001 — httpx.HTTPError/OSError/JSON etc. all become a value
        return {"stored": 0, "source": source, "error": f"{source} fetch failed: "
                + _fetch_error(exc, token if source == "tempest" else None)}
    return {"rows": rows, "source": source}


def store_weather(dal: Any, fetched: dict, *, now_utc: Optional[int] = None) -> dict:
    """The database half: store what fetch_weather got. A fetch error is passed through."""
    if "rows" not in fetched:
        return fetched
    now = now_utc if now_utc is not None else int(time.time())
    stored = 0
    for row in fetched["rows"]:
        if row["ts_utc"] > now + 3600:        # skip far-future forecast hours
            continue
        dal.upsert_weather(derive(row), updated_utc=now)
        stored += 1
    return {"stored": stored, "source": fetched.get("source")}


# ── helpers ──────────────────────────────────────────────────────────────────
def _iso_to_epoch(iso: str) -> Optional[int]:
    try:
        return calendar.timegm(datetime.strptime(iso, "%Y-%m-%dT%H:%M").timetuple())
    except (ValueError, TypeError):
        return None


def _at(seq: Any, i: int) -> Optional[float]:
    if isinstance(seq, list) and i < len(seq) and seq[i] is not None:
        return float(seq[i])
    return None


def _coords(config: Any) -> tuple[Optional[float], Optional[float]]:
    """Read site lat/lon from config.weather.open_meteo (falling back to
    config.weather). Placeholders (<LAT>) and non-numeric values read as absent."""
    try:
        w = config.get("weather") if config else None
        if w is None:
            return None, None
        node = w.get("open_meteo") if hasattr(w, "get") else None
        node = node if (node is not None and hasattr(node, "get")) else w
        return _f_or_none(node.get("latitude")), _f_or_none(node.get("longitude"))
    except (AttributeError, TypeError, ValueError):
        return None, None


def _f_or_none(v: Any) -> Optional[float]:
    if v is None or (isinstance(v, str) and (not v.strip() or v.strip().startswith("<"))):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
