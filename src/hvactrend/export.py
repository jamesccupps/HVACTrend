"""Bulk data export: any logged points, any days, as CSV (2026-10-20: "there should be
a way to just select points and dates and everything to export").

The trend viewer's CSV button exports what is on the chart, and a chart holds 64 points and
shows every 2-minute reading only for a window of 2 days or less. The operator wanted the 84 cooling
tower and condenser pump points at 2 minutes for every day of October, which took 40
exports. This writes any set of points over any run of local days at the resolution asked
for: one CSV, or a zip with a CSV per day.

The columns match the viewer's export, so a file opens the same whichever way it was made:
timestamp_iso, timestamp_utc, timestamp_local, resolution, then per point either a value and
a quality column (2-minute readings) or avg / min / max / n / n_total (the 5-minute and
hourly rollups). Each header is "<label> [<unit> <BACnet name>]".

At 2 minutes, a row is one POLL CYCLE. The collector reads the controllers a few seconds
apart, so readings from different panels in the same cycle differ by up to about half a
minute; they share a row stamped with the cycle's first reading. A point that gave nothing
in a cycle is blank, and one that answered badly says so in its quality column.

WEATHER (2026-10-23: "add a way to export tempest weather data too ... maybe all
weather data together"). One file for the whole range, one row per hour: the Tempest
station's stored snapshot (the program fetches one observation an hour, 15-25 minutes past
it; it is not an hourly average) beside the hourly averages of the building's own outdoor
sensors (the Siemens and Trane outdoor temperature, humidity, dew point and enthalpy, and the
boiler panel's). Hours with no Tempest reading stay in as blank rows, so the grid is whole.

It reads the database on its OWN read-only connection and is meant for a worker thread: a
month of 2-minute readings on a hundred points is millions of rows, and the collector polls
from the event loop.
"""

from __future__ import annotations

import csv
import io
import re
import time
import zipfile
from datetime import datetime, timezone
from typing import Any, Iterator

STEP_S = {"raw": 120, "5m": 300, "1h": 3600}
RES_LABEL = {"raw": "2-min polls", "5m": "5-min avg", "1h": "hourly avg"}
RES_FILE = {"raw": "2min", "5m": "5min", "1h": "hourly"}
# Points x rows. A month of 2-minute readings on 1,400 points, or a year of hourly averages
# on 3,400: about 0.5 GB of CSV, which is where a spreadsheet stops being the right tool.
MAX_CELLS = 30_000_000
_CYCLE_TOL_S = 60          # half a poll: the readings of one cycle, staggered by controller
# the building's outdoor-air sensors, by name or label; dampers named "OA" are not weather
# "Outdoor ...", a role-style OA_TEMP, or a bare OAT / OAH token (BLDG.OAT, OAT-1): the common
# ways a BAS names its outdoor temperature and humidity. A damper is excluded by the caller.
_OUTDOOR = re.compile(r"outdoor|\bOA_(TEMP|REL_HUMID|ENTHALPY)\b|\bOA[TH]\b",
                      re.I)
_UNIT_SHORT = {"degrees-fahrenheit": "°F", "percent": "%", "btus-per-pound-dry-air": "Btu/lb"}
WEATHER_COLS = [("outdoor_air_f", "oat_f"), ("relative_humidity_pct", "rh_pct"),
                ("wet_bulb_f", "wet_bulb_f"), ("dewpoint_f", "dewpoint_f"),
                ("enthalpy_btu_per_lb", "enthalpy"), ("wind_avg_mph", "wind_mph"),
                ("solar_w_m2", "solar_wm2"), ("station_pressure_mb", "pressure_mb")]


def local_days(start: int, end: int) -> list[tuple[int, int, str]]:
    """[(t0, t1, 'YYYY-MM-DD')] for each local day [t0, t1) that overlaps [start, end)."""
    out = []
    lt = time.localtime(start)
    day = (lt.tm_year, lt.tm_mon, lt.tm_mday)
    while True:
        t0 = int(time.mktime((day[0], day[1], day[2], 0, 0, 0, 0, 0, -1)))
        t1 = int(time.mktime((day[0], day[1], day[2] + 1, 0, 0, 0, 0, 0, -1)))
        if t0 >= end:
            break
        out.append((max(t0, start), min(t1, end), f"{day[0]:04d}-{day[1]:02d}-{day[2]:02d}"))
        nt = time.localtime(t1)
        day = (nt.tm_year, nt.tm_mon, nt.tm_mday)
    return out


def estimate_cells(n_points: int, start: int, end: int, res: str) -> int:
    return int(n_points * max(0, end - start) / STEP_S[res])


def _label(m: dict) -> str:
    bac = f"{m.get('system_code') or ''} {m.get('name') or ''}".strip()
    return f"{m['display_name']} [{bac}]" if m.get("display_name") else bac


def header(metas: list[dict], res: str) -> list[str]:
    hdr = ["timestamp_iso", "timestamp_utc", "timestamp_local", "resolution"]
    for m in metas:
        nm = _label(m)
        if res == "raw":
            hdr += [nm, nm + " quality"]
        else:
            hdr += [nm + " avg", nm + " min", nm + " max", nm + " n", nm + " n_total"]
    return hdr


def _stamp(t: int, res: str) -> list:
    return [datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"), t,
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)), RES_LABEL[res]]


def _blank(v: Any) -> Any:
    return "" if v is None else v


def day_rows(dal: Any, metas: list[dict], t0: int, t1: int, res: str) -> Iterator[list]:
    """The rows of [t0, t1) for these points, in time order."""
    if res == "raw":
        hits = []
        for i, m in enumerate(metas):
            for r in dal.query_series(m["id"], t0, t1 - 1):
                v = r["value_text"] if r["value_text"] is not None else r["value_num"]
                hits.append((r["ts_utc"], i, v, r["quality"]))
        hits.sort(key=lambda h: (h[0], h[1]))
        row_t, got = None, {}
        for t, i, v, q in hits:
            # a new cycle: past half a poll from the cycle's first reading, or this point
            # already has its reading in the cycle (two cycles closer together than usual)
            if row_t is None or t - row_t > _CYCLE_TOL_S or i in got:
                if row_t is not None:
                    yield _raw_row(row_t, got, len(metas))
                row_t, got = t, {}
            got[i] = (v, q)
        if row_t is not None:
            yield _raw_row(row_t, got, len(metas))
        return
    step = STEP_S[res]
    by_t: dict = {}
    for i, m in enumerate(metas):
        for r in _buckets(dal, m["id"], t0, t1 - 1, res, step):
            by_t.setdefault(int(r["bucket_utc"]), {})[i] = r
    for t in sorted(by_t):
        line = _stamp(t, res)
        got = by_t[t]
        for i in range(len(metas)):
            r = got.get(i)
            line += (["", "", "", "", ""] if r is None else
                     [_blank(r.get("v_avg")), _blank(r.get("v_min")), _blank(r.get("v_max")),
                      _blank(r.get("n")), _blank(r.get("n_total"))])
        yield line


def _raw_row(t: int, got: dict, n: int) -> list:
    line = _stamp(t, "raw")
    for i in range(n):
        v, q = got.get(i, (None, None))
        line += [_blank(v), q or ""]
    return line


def _buckets(dal: Any, pid: int, start: int, end: int, res: str, step: int) -> list:
    """The stored rollup buckets with their gaps bucketed from the 2-minute readings: the
    same DAL.rollup_series the viewer's chart uses."""
    return dal.rollup_series(pid, start, end, res)


def outdoor_points(dal: Any, start: int, end: int) -> list[dict]:
    """The building's own outdoor-air sensors with readings in [start, end), as point_meta
    rows: the Siemens and Trane outdoor temperature and humidity, the Trane dew point and
    enthalpy, the boiler panel's outdoor temperature (and, for July, the per-unit copies)."""
    out = []
    for p in dal.logged_points():
        text = f"{p['name']} {p.get('display_name') or ''}"
        if not _OUTDOOR.search(text) or "damper" in text.lower():
            continue
        if _buckets(dal, p["id"], start, end - 1, "1h", 3600):
            out.append(dal.point_meta(p["id"]))
    return out


def weather_header(sensors: list[dict]) -> list[str]:
    hdr = ["hour_utc_iso", "hour_utc", "hour_local", "tempest_observed_local"]
    hdr += ["tempest_" + name for name, _ in WEATHER_COLS] + ["tempest_source"]
    for m in sensors:
        u = _UNIT_SHORT.get(m.get("units_text") or "", "")
        hdr.append(f"{_label(m)} hourly avg" + (f" ({u})" if u else ""))
    return hdr


def weather_rows(dal: Any, sensors: list[dict], start: int, end: int) -> Iterator[list]:
    """One row per hour of [start, end): the Tempest snapshot, then each sensor's hourly
    average of its good readings. An hour with no Tempest reading reads "missing"."""
    end = min(end, int(time.time()))
    tempest = {int(r["ts_utc"]): r for r in dal.weather_rows(start, end)}
    avg = [{int(b["bucket_utc"]): b.get("v_avg") for b in _buckets(dal, m["id"], start, end - 1,
                                                                 "1h", 3600)}
           for m in sensors]
    for t in range(start - start % 3600, end, 3600):
        r = tempest.get(t)
        line = [datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), t,
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))]
        if r is None:
            line += [""] * (len(WEATHER_COLS) + 1) + ["missing"]
        else:
            line.append(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["created_utc"])))
            line += [_blank(r.get(k)) for _, k in WEATHER_COLS] + [r.get("source") or ""]
        line += [_blank(a.get(t)) for a in avg]
        yield line


def _write_weather(f: Any, dal: Any, start: int, end: int) -> int:
    sensors = outdoor_points(dal, start, end)
    w = csv.writer(f)
    w.writerow(weather_header(sensors))
    n = 0
    for line in weather_rows(dal, sensors, start, end):
        w.writerow(line)
        n += 1
    return n


def write_export(db_path: str, ids: list[int], start: int, end: int, res: str, layout: str,
                 out_path: str, *, weather: bool = False) -> dict:
    """Write the export to `out_path`: a CSV (layout 'one') or a zip of a CSV per local day
    (layout 'day'). With `weather`, the weather file goes in too: on its own it is the CSV;
    with points the download is a zip whatever the layout, since a CSV holds one table.
    Returns {filename, zip, points, rows, weather_hours, days, missing}. Read-only."""
    from .storage.sqlite_dal import SQLiteDAL
    dal = SQLiteDAL(db_path, read_only=True)
    try:
        metas, missing = [], []
        for pid in ids:
            m = dal.point_meta(pid)
            (metas.append(m) if m else missing.append(pid))
        days = local_days(start, end)
        first, last = (days[0][2], days[-1][2]) if days else ("", "")
        span = first if first == last else f"{first}_to_{last}"
        base = f"hvactrend_export_{span}_{RES_FILE[res]}"
        weather_name = f"hvactrend_weather_{span}_hourly.csv"
        rows = hours = 0
        as_zip = bool(metas) and (layout == "day" or weather)
        if not metas:
            # utf-8-sig: Excel reads the BOM and shows °F and the em dashes correctly
            with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
                hours = _write_weather(f, dal, start, end) if weather else 0
            filename = weather_name
        elif as_zip:
            with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
                groups = ([(f"hvactrend_{name}_{RES_FILE[res]}.csv", [(t0, t1)])
                           for t0, t1, name in days] if layout == "day" else
                          [(base + ".csv", [(t0, t1) for t0, t1, _ in days])])
                for name, spans in groups:
                    with zf.open(name, "w") as raw:
                        with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as f:
                            w = csv.writer(f)
                            w.writerow(header(metas, res))
                            for t0, t1 in spans:
                                for line in day_rows(dal, metas, t0, t1, res):
                                    w.writerow(line)
                                    rows += 1
                if weather:
                    with zf.open(weather_name, "w") as raw:
                        with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as f:
                            hours = _write_weather(f, dal, start, end)
            filename = base + ".zip"
        else:
            with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(header(metas, res))
                for t0, t1, _ in days:
                    for line in day_rows(dal, metas, t0, t1, res):
                        w.writerow(line)
                        rows += 1
            filename = base + ".csv"
        return {"filename": filename, "zip": filename.endswith(".zip"), "points": len(metas),
                "rows": rows, "weather_hours": hours, "days": len(days), "missing": missing}
    finally:
        dal.close()
