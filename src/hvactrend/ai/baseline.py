"""Learned-baseline builder (ported concept from the old AHU program's domain model).

Computes a per-point NORMAL model from OUR OWN accumulated samples — the deviation
reference the pre-screen lacked (the hand-set alarm_hi/lo are usually empty). Two
ideas carried over from the old program:

  1. **Occupancy partitioning.** A supply fan at 0% is normal at 2 AM, alarming at
     2 PM — so mean/std are computed separately for occupied vs unoccupied hours.
  2. **Relationship baselines.** A least-squares fit between role-paired points
     (setpoint↔DAT, OAT↔MAT, fan-speed↔duct-static) lets the pre-screen flag a
     control-loop fault a per-point band can't see.

Rolling window (default 14 days), self-refreshing, so shoulder-season transitions
don't blow up z-scores. Occupancy uses the box's local time (the collector runs
on-site in the building's timezone; the OS handles DST) — the core rule is a pure
function so it's testable without depending on the test machine's clock.

Read-only wrt BACnet: pure reads of stored samples + local DB writes.
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable, Optional, Sequence

from .roles import ANALYSIS_ROLES

STD_FLOOR = 0.4                 # fallback floor when a point's units are unknown

# Noise floor for a learned band, IN THE POINT'S OWN ENGINEERING UNITS.
#
# This has to be per-unit. A flat 0.4 is sensible for degrees-fahrenheit and nonsense
# for inches-of-water: building static pressure lives around 0.001-0.07 in wc and duct
# static around 0.5-2.0, so a 0.4 floor inflated those bands by 4x-400x and made
# |z| >= 3 PHYSICALLY UNREACHABLE — silently disabling the deviation AND drift detectors
# on all 26 pressure points in the plant (13 DUCT_PRESS + 13 SPACE_PRESS). A real
# over-pressurization from 0.016 to 0.25 in wc scored z = 0.59 and raised nothing.
# Found in review #3. Values are "the smallest move an operator would call real".
_STD_FLOOR_BY_UNIT = {
    "degrees-fahrenheit": 0.4,
    "degrees-celsius": 0.25,
    "degrees-kelvin": 0.25,
    "inches-of-water": 0.01,
    "centimeters-of-water": 0.025,
    "millibars": 0.025,
    "pascals": 2.5,
    "kilopascals": 0.0025,
    "percent": 1.0,
    "percent-relative-humidity": 1.0,
    "parts-per-million": 20.0,
    "cubic-feet-per-minute": 25.0,
    "liters-per-second": 12.0,
}


def std_floor(units_text: Optional[str]) -> float:
    """Smallest std a learned band may claim, in `units_text`'s own units. Keeps a
    z-score finite on a near-constant point without blinding a fine-grained one."""
    return _STD_FLOOR_BY_UNIT.get((units_text or "").strip().lower(), STD_FLOOR)
_DEFAULT_WINDOW_DAYS = 14

# Occupancy schedule default: weekday 05:00–18:00, unoccupied on weekends. Override
# via config.analysis.occupancy (per building — this is not universal).
DEFAULT_OCC = {"weekday_start": 5, "weekday_end": 18, "weekend_occupied": False}

# Role pairs whose learned relationship is worth tracking (x drives y).
CORR_PAIRS = [
    ("sp_dat", "DAT_ACTIVE_SP", "DAT"),     # discharge setpoint should drive discharge temp
    ("oat_mat", "OAT", "MAT"),              # outdoor temp ↔ mixed air (economizer)
    ("fan_press", "FAN_SPEED", "DUCT_PRESS"),  # fan speed ↔ duct static
]


# ── occupancy ────────────────────────────────────────────────────────────────
def occupied_local(hour: int, wday: int, sched: dict = DEFAULT_OCC) -> bool:
    """Pure rule: is (local hour, weekday 0=Mon..6=Sun) an occupied hour? Testable."""
    if wday >= 5:
        return bool(sched.get("weekend_occupied", False))
    return sched.get("weekday_start", 5) <= hour < sched.get("weekday_end", 18)


def is_occupied(ts_utc: int, sched: dict = DEFAULT_OCC,
                localtime: Callable = time.localtime) -> bool:
    """Occupied? `sched` is the building config dict, OR a per-unit day-map (see
    daymap_for_system), OR a plain callable predicate — so callers can be handed a
    unit-specific rule without changing their signature."""
    if callable(sched):
        return bool(sched(ts_utc))
    lt = localtime(ts_utc)
    if isinstance(sched, dict) and "daymap" in sched:
        win = sched["daymap"].get(lt.tm_wday)
        if not win:
            return False
        m = lt.tm_hour * 60 + lt.tm_min
        return win[0] <= m < win[1]
    return occupied_local(lt.tm_hour, lt.tm_wday, sched)


_DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")   # index == tm_wday


def default_daymap(sched: dict = DEFAULT_OCC) -> dict[int, Optional[tuple[int, int]]]:
    """The building-wide config schedule expressed as a day-map (minutes from midnight)."""
    ws = int(sched.get("weekday_start", 5)) * 60
    we = int(sched.get("weekday_end", 18)) * 60
    weekend = bool(sched.get("weekend_occupied", False))
    return {i: ((ws, we) if i < 5 else ((ws, we) if weekend else None)) for i in range(7)}


def daymap_for_system(system: Any, building: dict[int, Optional[tuple[int, int]]],
                      ) -> dict[int, Optional[tuple[int, int]]]:
    """Resolve ONE unit's occupancy day-map: its occupancy MODE wins first (vacant = never
    occupied, always = 24/7), then its own `occ_schedule` override, else the building
    default. `system` is a dict/row with occupancy_mode + occ_schedule."""
    get = system.get if hasattr(system, "get") else (lambda k, d=None: getattr(system, k, d))
    mode = (get("occupancy_mode") or "scheduled")
    if isinstance(mode, str):
        mode = mode.lower()
    if mode == "vacant":
        return {i: None for i in range(7)}
    if mode == "always":
        return {i: (0, 1440) for i in range(7)}
    raw = get("occ_schedule")
    if raw:
        try:
            import json
            d = json.loads(raw) if isinstance(raw, str) else dict(raw)
            out: dict[int, Optional[tuple[int, int]]] = {}
            for i, k in enumerate(_DAY_KEYS):
                v = d.get(k, "missing")
                if v == "missing":
                    out[i] = building.get(i)          # day not specified -> inherit
                elif v is None:
                    out[i] = None                     # explicitly unoccupied that day
                else:
                    out[i] = (int(v[0]), int(v[1]))
            return out
        except (ValueError, TypeError, KeyError, IndexError):
            pass                                      # malformed -> fall back to building
    return dict(building)


def system_daymaps(systems: Any, config: Any = None) -> dict[Any, dict]:
    """{system_id: day-map} for every system, so the analysis passes can judge each unit
    against ITS OWN hours (staggered starts / weekend variance) instead of one global
    schedule. Pass dal.list_systems()."""
    building = default_daymap(occ_schedule(config))
    return {s["id"]: daymap_for_system(s, building) for s in systems}


def occ_pred(daymap: dict, localtime: Callable = time.localtime) -> Callable[[int], bool]:
    """A ts->bool predicate bound to one unit's day-map (hand to is_occupied/consumers)."""
    def _p(ts_utc: int) -> bool:
        lt = localtime(ts_utc)
        win = daymap.get(lt.tm_wday)
        if not win:
            return False
        m = lt.tm_hour * 60 + lt.tm_min
        return win[0] <= m < win[1]
    return _p


# ── per-point baseline ───────────────────────────────────────────────────────
def _stats(vals: list[float], floor: float = STD_FLOOR) -> Optional[dict]:
    if not vals:
        return None
    mean = sum(vals) / len(vals)
    var = sum((x - mean) ** 2 for x in vals) / len(vals) if len(vals) > 1 else 0.0
    return {"mean": mean, "std": max(math.sqrt(var), floor), "n": len(vals)}


def build_point_baseline(rows: Sequence[dict], sched: dict = DEFAULT_OCC,
                         localtime: Callable = time.localtime,
                         units_text: Optional[str] = None) -> dict:
    """From a point's raw window rows, compute occ/unocc mean·std, changed_frac
    (activity — ~0 = stuck), and long_slope (value/day drift over the window).

    `units_text` selects the band's noise floor (see `std_floor`); omitting it falls
    back to the degF-scale default, which over-widens a fine-grained point's band."""
    occ: list[float] = []
    unocc: list[float] = []
    changed = 0
    n_polls = 0
    prev: Optional[float] = None
    good_ts_val: list[tuple[int, float]] = []
    for r in rows:
        if r.get("quality") != "good" or r.get("value_num") is None:
            continue
        v = r["value_num"]
        n_polls += 1
        if prev is not None and v != prev:
            changed += 1
        prev = v
        good_ts_val.append((r["ts_utc"], v))
        (occ if is_occupied(r["ts_utc"], sched, localtime) else unocc).append(v)
    fit = _linfit(good_ts_val)
    floor = std_floor(units_text)
    return {
        "occ": _stats(occ, floor), "unocc": _stats(unocc, floor),
        "changed_frac": (changed / n_polls) if n_polls else None,
        "long_slope": (fit["slope"] * 86400.0) if fit else None,   # value per day
        "n_polls": n_polls,
    }


# ── least squares (shared by long-slope + correlations) ──────────────────────
def _linfit(pairs: Sequence[tuple[float, float]]) -> Optional[dict]:
    n = len(pairs)
    if n < 3:
        return None
    sx = sum(x for x, _ in pairs)
    sy = sum(y for _, y in pairs)
    sxx = sum(x * x for x, _ in pairs)
    sxy = sum(x * y for x, y in pairs)
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    rvar = sum((y - (slope * x + intercept)) ** 2 for x, y in pairs) / n
    return {"slope": slope, "intercept": intercept,
            "residual_std": max(math.sqrt(rvar), 1e-6), "n": n}


# ── orchestration ────────────────────────────────────────────────────────────
def occ_schedule(config: Any) -> dict:
    try:
        node = config.get("analysis").get("occupancy") if config else None
        if node:
            d = node.as_dict() if hasattr(node, "as_dict") else dict(node)
            return {**DEFAULT_OCC, **d}
    except (AttributeError, TypeError):
        pass
    return DEFAULT_OCC


def baseline_window_days(config: Any) -> int:
    """`analysis.baseline.window_days` — the rolling window the normal bands learn from.

    Read from config so every trigger agrees. It previously wasn't: the CLI
    (scripts/build_model.py) hard-coded 30 while the UI's "Rebuild model" button fell
    through to this module's 14-day default, so the SAME rebuild produced different
    bands depending on how it was started, and the configured value was read by neither.
    """
    try:
        node = config.get("analysis").get("baseline") if config else None
        if node:
            d = node.as_dict() if hasattr(node, "as_dict") else dict(node)
            v = int(d.get("window_days", _DEFAULT_WINDOW_DAYS))
            if v > 0:
                return v
    except (AttributeError, TypeError, ValueError):
        pass
    return _DEFAULT_WINDOW_DAYS


def _preds_by_system(dal: Any, config: Any = None,
                     localtime: Callable = time.localtime) -> dict[Any, Callable]:
    """{system_id: ts->bool occupancy predicate} using each unit's own resolved day-map.
    Falls back to {} (callers then use the building schedule) if systems can't be read."""
    try:
        return {sid: occ_pred(dm, localtime)
                for sid, dm in system_daymaps(dal.list_systems(), config).items()}
    except Exception:  # noqa: BLE001 — never break analysis over schedule resolution
        return {}


def build_baselines(dal: Any, config: Any = None, *, window_days: Optional[int] = None,
                    now_utc: Optional[int] = None,
                    localtime: Callable = time.localtime) -> dict:
    """Rebuild baselines for every enabled analysis-role point + the relationship
    correlations per system, from the trailing `window_days`. Returns a summary.

    `window_days=None` (the default) resolves `analysis.baseline.window_days` from
    config, so the scheduled rebuild, the CLI and the UI button all agree. Pass an
    explicit value only to override it."""
    now = now_utc if now_utc is not None else int(time.time())
    window_days = (baseline_window_days(config) if window_days is None
                   else int(window_days))
    start = now - window_days * 86400
    sched = occ_schedule(config)
    pts = dal.points_for_analysis()
    # Judge each point against ITS OWN unit's hours. The plant staggers start times (floors
    # differ by an hour+) and some units keep different weekend hours — using one global
    # schedule folds legitimately-off operation into the "occupied" band, widening it and
    # blunting deviation detection.
    preds = _preds_by_system(dal, config, localtime)

    n_pts = 0
    for p in pts:
        if p.get("role") not in ANALYSIS_ROLES:
            continue
        rows = dal.query_series(p["id"], start, now)
        b = build_point_baseline(rows, preds.get(p.get("system_id"), sched), localtime,
                                 units_text=p.get("units_text"))
        for mode in ("occ", "unocc"):
            s = b[mode]
            dal.upsert_baseline(
                p["id"], mode,
                mean=s["mean"] if s else None, std=s["std"] if s else None,
                n=s["n"] if s else 0, changed_frac=b["changed_frac"],
                long_slope=b["long_slope"], window_start_utc=start,
                window_end_utc=now, updated_utc=now)
        n_pts += 1

    n_corr = _build_corrs(dal, pts, start, now)
    return {"points": n_pts, "corrs": n_corr, "window_days": window_days}


def _canonical_by_role(pts: list[dict]) -> dict:
    """{(system_id, role): point_id} for canonical analysis points."""
    out = {}
    for p in pts:
        if p.get("role_canonical"):
            out[(p.get("system_id"), p.get("role"))] = p["id"]
    return out


def _build_corrs(dal: Any, pts: list[dict], start: int, now: int) -> int:
    canon = _canonical_by_role(pts)
    systems = {p.get("system_id") for p in pts if p.get("system_id") is not None}
    count = 0
    for sys_id in systems:
        for kind, x_role, y_role in CORR_PAIRS:
            xid = canon.get((sys_id, x_role))
            yid = canon.get((sys_id, y_role))
            if not xid or not yid:
                continue
            xs = {r["ts_utc"]: r["value_num"] for r in dal.query_series(xid, start, now)
                  if r.get("quality") == "good" and r.get("value_num") is not None}
            ys = {r["ts_utc"]: r["value_num"] for r in dal.query_series(yid, start, now)
                  if r.get("quality") == "good" and r.get("value_num") is not None}
            pairs = [(xs[t], ys[t]) for t in xs.keys() & ys.keys()]   # paired by poll ts
            fit = _linfit(pairs)
            if not fit:
                continue
            dal.upsert_corr(sys_id, kind, x_point_id=xid, y_point_id=yid,
                            slope=fit["slope"], intercept=fit["intercept"],
                            residual_std=fit["residual_std"], n=fit["n"], updated_utc=now)
            count += 1
    return count
