"""Detect each unit's REAL occupancy schedule from trended history.

The plant staggers start times (floors differ by an hour or more) and some units keep
different weekend hours, but the analysis stack only knows ONE building-wide schedule.
Hand-typing 14 unit schedules is tedious and drifts from reality, so this derives the
actual schedule from data and hands it back for the operator to CONFIRM.

Signal priority
---------------
1. **The unit's own OCCUPANCY point** (Trane `Occupancy Status`, multi-state). This is the
   controller's own occupied/unoccupied state — ground truth. Which multistate value means
   "occupied" is SELF-CALIBRATED (whichever value dominates mid-workday) rather than
   hardcoded, so an inverted convention on some controller can't silently flip the result.
2. **Fan speed**, only when no occupancy point is trended. This needs care: in cold weather
   the fans are commanded to ~30% for freeze protection even while UNOCCUPIED, so a naive
   "fan running = occupied" test would report those units occupied 24/7 all winter. We
   require a clearly-occupied fan speed AND explicitly discard low-speed running while the
   outdoor air is cold.

Gaps are first-class
--------------------
The collector has had outages (a weekend with zero samples). A gap must never be read as
"occupied the whole time": a calendar day only counts if its samples actually SPAN the
transition windows, and each weekday reports how many days backed it. A weekday with no
usable days is returned as `insufficient` — never guessed.
"""

from __future__ import annotations

import statistics
import time
from typing import Any, Callable, Optional

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")   # index == tm_wday

# Fan-fallback thresholds (see module docstring — the cold-weather freeze-circulation trap)
_DEF = {
    "lookback_days": 14,
    "occupied_fan_pct": 45.0,    # at/above this the unit is clearly running for occupancy
    "freeze_fan_pct": 35.0,      # at/below this it may be cold-weather circulation
    "cold_oat_f": 40.0,          # ... when the outdoor air is this cold
    "min_samples_per_day": 200,  # ~7 h at a 120 s poll
    "cover_from_hour": 4,        # a usable day must have samples by this local hour ...
    "cover_to_hour": 19,         # ... and still be sampling at this one
    "round_to_min": 5,
    # Transition estimation. The occupied window is the LONGEST CONTIGUOUS block of
    # occupied samples, not min()..max() of them — see _occupied_span.
    "max_gap_min": 60,           # occupied samples further apart than this = separate blocks
    "min_block_min": 30,         # a block shorter than this is a blip, not a schedule
    # Deep-night reference window used to learn which state means "unoccupied"
    # (commercial office plant — nothing here starts before 05:00).
    "night_from_hour": 0,
    "night_to_hour": 4,
}


def _cfg(config: Any) -> dict:
    out = dict(_DEF)
    try:
        node = config.get("analysis").get("schedule_detect") if config else None
        if node:
            d = node.as_dict() if hasattr(node, "as_dict") else dict(node)
            for k in out:
                if k in d and d[k] is not None:
                    out[k] = type(out[k])(d[k])
    except (AttributeError, TypeError, ValueError):
        pass
    return out


def _round_to(minutes: int, step: int) -> int:
    return int(round(minutes / step) * step) if step > 0 else int(minutes)


def _pick_signal(points: list[dict]) -> tuple[Optional[dict], str]:
    """Choose the best occupancy signal for one system: an OCCUPANCY 'status' point first
    (the controller's own state), then any OCCUPANCY point, then FAN_SPEED."""
    occ = [p for p in points if p.get("role") == "OCCUPANCY"]
    status = [p for p in occ if "status" in (p.get("name") or "").lower()]
    if status:
        return status[0], "occupancy-point"
    if occ:
        return occ[0], "occupancy-point"
    fans = [p for p in points if p.get("role") == "FAN_SPEED" and p.get("role_canonical")]
    if fans:
        return fans[0], "fan-speed"
    return None, "none"


def _occupied_value(rows: list[dict], localtime: Callable,
                    cfg: Optional[dict] = None) -> Optional[float]:
    """Self-calibrate which multistate value means OCCUPIED, without hardcoding
    1=occupied.

    NOT simply "the value that dominates mid-workday": a unit that is mostly
    UNOCCUPIED (a vacant floor with a short midday call) spends most of the workday in
    its UNOCCUPIED state, so the dominant midday value IS the unoccupied one — taking
    it inverts the whole result and reports the unit occupied 24/7. That is exactly
    what AC-9 did on the live plant (unoccupied 22 h/day, brief 10:00-13:00 call ->
    detected 00:00-24:00).

    Instead: learn the RESTING state from deep night (nothing in a commercial office
    plant starts before 05:00), then take the most common mid-workday value that
    DIFFERS from it. A point that never leaves its night state has no distinguishable
    occupied period and returns None rather than a guess.
    """
    c = cfg or _DEF
    night: dict[float, int] = {}
    day: dict[float, int] = {}
    for r in rows:
        v = r.get("value_num")
        if v is None:
            continue
        lt = localtime(r["ts_utc"])
        if lt.tm_wday >= 5:
            continue
        if c["night_from_hour"] <= lt.tm_hour < c["night_to_hour"]:
            night[v] = night.get(v, 0) + 1
        elif 10 <= lt.tm_hour < 15:
            day[v] = day.get(v, 0) + 1
    if not day:
        return None
    if not night:                       # no night coverage -> best effort, old behaviour
        return max(day.items(), key=lambda kv: kv[1])[0]
    resting = max(night.items(), key=lambda kv: kv[1])[0]
    cands = {v: n for v, n in day.items() if v != resting}
    if not cands:
        return None                     # never differs from its night state
    return max(cands.items(), key=lambda kv: kv[1])[0]


def _occupied_span(minutes: list[int], max_gap: int,
                   min_len: int) -> Optional[tuple[int, int]]:
    """Start/end of the LONGEST contiguous block of occupied minutes.

    min()..max() over every occupied sample is not robust: ONE isolated sample outside
    the real window stretches the detected day by hours. On the live plant HV1 read
    02:28-22:17 from single stray samples while its 10th-90th percentile occupied span
    was 06:18-16:47. Blocks are joined across gaps up to `max_gap`; a winning block
    shorter than `min_len` is a blip, not a schedule, and returns None.
    """
    ms = sorted(set(int(m) for m in minutes))
    if not ms:
        return None
    best: tuple[int, int] = (ms[0], ms[0])
    s = e = ms[0]
    for m in ms[1:]:
        if m - e <= max_gap:
            e = m
            continue
        if (e - s) > (best[1] - best[0]):
            best = (s, e)
        s = e = m
    if (e - s) > (best[1] - best[0]):
        best = (s, e)
    return best if (best[1] - best[0]) >= min_len else None


def _weather_by_hour(dal: Any, start: int, end: int) -> dict[int, float]:
    """hour-floored ts -> outdoor air temp, for the cold-weather fan exclusion."""
    out: dict[int, float] = {}
    try:
        for w in dal.weather_range(start, end):
            if w.get("oat_f") is not None:
                out[(int(w["ts_utc"]) // 3600) * 3600] = float(w["oat_f"])
    except Exception:  # noqa: BLE001 — weather is optional context
        pass
    return out


def detect_schedules(dal: Any, config: Any = None, *, now_utc: Optional[int] = None,
                     lookback_days: Optional[int] = None,
                     localtime: Callable = time.localtime) -> dict:
    """Derive each system's occupancy schedule from history.

    Returns {"systems": [{code, system_id, signal, point_name, days: {mon: {...}}, note}]}
    where each day is either {"start_min","end_min","observed_days","confidence"},
    {"unoccupied": True, ...}, or {"insufficient": True, "observed_days": 0}.
    """
    cfg = _cfg(config)
    days_back = int(lookback_days if lookback_days is not None else cfg["lookback_days"])
    now = now_utc if now_utc is not None else int(time.time())
    start = now - days_back * 86400

    pts = dal.points_for_analysis()
    by_sys: dict[Any, list[dict]] = {}
    for p in pts:
        by_sys.setdefault(p.get("system_id"), []).append(p)

    weather = _weather_by_hour(dal, start, now)
    out = []
    for sys_id, spts in by_sys.items():
        code = spts[0].get("system_code") or "?"
        sig, kind = _pick_signal(spts)
        entry = {"system_id": sys_id, "code": code, "signal": kind,
                 "point_name": (sig or {}).get("name"), "days": {}, "note": ""}
        if sig is None:
            entry["note"] = "no occupancy or fan-speed point trended for this unit"
            entry["days"] = {d: {"insufficient": True, "observed_days": 0} for d in DAYS}
            out.append(entry)
            continue

        rows = [r for r in dal.query_series(sig["id"], start, now)
                if r.get("quality") == "good" and r.get("value_num") is not None]
        occ_val = _occupied_value(rows, localtime, cfg) if kind == "occupancy-point" else None
        if kind == "occupancy-point" and occ_val is None:
            entry["note"] = ("occupancy point never leaves its overnight state (always "
                             "occupied, always unoccupied, or stuck) — no schedule can be "
                             "derived; set this unit's mode manually")
            entry["days"] = {d: {"insufficient": True, "observed_days": 0} for d in DAYS}
            out.append(entry)
            continue

        # bucket samples by LOCAL calendar date -> (minute-of-day, occupied?)
        per_date: dict[str, list[tuple[int, bool]]] = {}
        for r in rows:
            lt = localtime(r["ts_utc"])
            minute = lt.tm_hour * 60 + lt.tm_min
            v = r["value_num"]
            if kind == "occupancy-point":
                occupied = (v == occ_val)
            else:
                oat = weather.get((int(r["ts_utc"]) // 3600) * 3600)
                cold_circ = (oat is not None and oat < cfg["cold_oat_f"]
                             and v <= cfg["freeze_fan_pct"])
                occupied = (v >= cfg["occupied_fan_pct"]) and not cold_circ
            key = f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"
            per_date.setdefault(key, []).append((minute, occupied, lt.tm_wday))

        # a date only counts if its samples SPAN the transition windows (never infer
        # across a collector gap)
        per_wday: dict[int, list[tuple[int, int]]] = {}
        unocc_wday: dict[int, int] = {}
        for key, vals in per_date.items():
            if len(vals) < cfg["min_samples_per_day"]:
                continue
            minutes = [m for m, _o, _w in vals]
            if min(minutes) > cfg["cover_from_hour"] * 60 or max(minutes) < cfg["cover_to_hour"] * 60:
                continue                      # partial day / gap — unusable
            wday = vals[0][2]
            occ_minutes = [m for m, o, _w in vals if o]
            span = _occupied_span(occ_minutes, cfg["max_gap_min"],
                                  cfg["min_block_min"]) if occ_minutes else None
            if span is None:      # nothing occupied, or only a blip -> unoccupied day
                unocc_wday[wday] = unocc_wday.get(wday, 0) + 1
                continue
            per_wday.setdefault(wday, []).append(span)

        for i, dname in enumerate(DAYS):
            obs = per_wday.get(i, [])
            n_unocc = unocc_wday.get(i, 0)
            if not obs and not n_unocc:
                entry["days"][dname] = {"insufficient": True, "observed_days": 0}
            elif not obs:
                entry["days"][dname] = {"unoccupied": True, "observed_days": n_unocc,
                                        "confidence": _conf(n_unocc)}
            else:
                s = _round_to(int(statistics.median([a for a, _b in obs])), cfg["round_to_min"])
                e = _round_to(int(statistics.median([b for _a, b in obs])), cfg["round_to_min"])
                entry["days"][dname] = {"start_min": s, "end_min": e,
                                        "observed_days": len(obs),
                                        "confidence": _conf(len(obs))}
        out.append(entry)

    out.sort(key=lambda e: _natural(e["code"]))
    return {"lookback_days": days_back, "systems": out}


def _conf(n: int) -> str:
    return "high" if n >= 3 else ("medium" if n == 2 else "low")


def _natural(code: str) -> tuple:
    import re
    m = re.match(r"^([A-Za-z\-]+)(\d+)?", code or "")
    return (m.group(1) if m else code, int(m.group(2)) if (m and m.group(2)) else 0)


def hhmm(minutes: Optional[int]) -> str:
    """Minutes-from-midnight -> 'HH:MM' (for display/logging)."""
    if minutes is None:
        return "--:--"
    return f"{int(minutes) // 60:02d}:{int(minutes) % 60:02d}"
