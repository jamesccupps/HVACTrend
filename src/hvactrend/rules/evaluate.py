"""Run the rules over stored samples: the plumbing between the database and the engine.

For each unit, pick the points a rule reads (the canonical point per role, as the coverage
review and the viewer do), build the operating-state gate (state.py), line the series up in
time, and hand (t, value, judged) to the incident machine (engine.py).

Read-only: it needs `rule_points()` and `query_series()` from the DAL and writes nothing, so
the 30-day replay and the live evaluation are the same code. Points the site knowledge sets
aside (coverage.set_aside) are never judged, and units marked VACANT are judged only on their
life-safety alarms (freeze, smoke, flame): AC-9's floor is empty and CT-2 is out of service.
"""

from __future__ import annotations

import bisect
import math
import time
from dataclasses import dataclass
from typing import Any, Optional

from .. import coverage
from . import catalog, state
from .engine import Episode, episodes

_ALIGN_S = 300          # a companion reading older than this does not count as "now"
_GATE_LOOKBACK_S = 12 * 3600   # a fan that started before the window still has settled


@dataclass
class Finding:
    rule: str
    title: str
    unit: str
    key: str                 # '' for a unit rule, the point id for a per-point rule
    point: str               # what is being judged, for the message
    tier: str
    episode: Episode
    detail: str

    @property
    def fingerprint(self) -> str:
        return f"{self.rule}:{self.unit}:{self.key}"


def evaluate(dal: Any, rules_cfg: Any, start: int, end: int, *,
             knowledge: Any = None, now: Optional[int] = None,
             only: Optional[set] = None, seeds: Optional[dict] = None,
             errors: Optional[list] = None) -> list:
    """Every episode each rule finds between `start` and `end` (UTC seconds). `seeds` maps a
    fingerprint to an Episode already open (rules/live.py), which the engine continues rather
    than opening a second one; the SAME object comes back when it is still the one running.

    With `errors` (a list), a rule that fails on one unit or point is recorded there and the
    rest are still evaluated: on 2026-10-13 one boiler sensor's incident crashed every rule in
    the run (live.py passes a list). Without it, the failure is raised (the replay, tests)."""
    now = end if now is None else now
    seeds = seeds or {}
    facts = coverage.site_facts(knowledge)
    pts = [p for p in dal.rule_points() if not coverage.set_aside(p, facts, None)]
    by_unit: dict = {}
    for p in pts:
        if p.get("system_code"):
            by_unit.setdefault(p["system_code"], []).append(p)
    cache: dict = {}

    def series(pid: int, s: int, *, raw: bool = False) -> list:
        """(t, value) for one point. Unless `raw`, samples its controller flagged are None:
        a rule never judges on a reading the controller itself says is bad."""
        k = (pid, s, raw)
        if k not in cache:
            rows = dal.query_series(pid, s, end)
            if raw:
                cache[k] = [(r["ts_utc"], r["value_num"] if r.get("quality") == "good"
                             else math.nan) for r in rows if r.get("value_num") is not None]
            else:
                cache[k] = [(r["ts_utc"], r["value_num"] if r.get("quality") == "good" else None)
                            for r in rows]
        return cache[k]

    out: list = []
    for unit, upts in sorted(by_unit.items()):
        kind = upts[0].get("system_kind") or ""
        vacant = (upts[0].get("occupancy_mode") or "") == "vacant"
        canon = {p["role"]: p for p in upts if p.get("role_canonical")}
        for rule in catalog.UNIT_RULES:
            if (only and rule not in only) or vacant:
                continue
            try:
                out.extend(_unit_rule(rule, unit, kind, upts, canon, series, rules_cfg, start,
                                      end, now, seeds.get(f"{rule}:{unit}:")))
            except Exception as exc:  # noqa: BLE001 -- see `errors` above
                if errors is None:
                    raise
                errors.append(f"{rule} on {unit}: {type(exc).__name__}: {exc}")
        for p in upts:
            for rule in catalog.POINT_RULES:
                if only and rule not in only:
                    continue
                try:
                    out.extend(_point_rule(rule, unit, p, vacant, series, rules_cfg, start, end,
                                           now, seeds.get(f"{rule}:{unit}:{p['id']}")))
                except Exception as exc:  # noqa: BLE001 -- see `errors` above
                    if errors is None:
                        raise
                    errors.append(f"{rule} on {unit} point {p['id']}: {type(exc).__name__}: {exc}")
    return out


# ── unit rules ───────────────────────────────────────────────────────────────────────────
def _unit_rule(rule, unit, kind, upts, canon, series, cfg, start, end, now, seed=None) -> list:
    if rule in ("sat-setpoint", "static-setpoint"):
        if kind not in coverage.AIR_KINDS:
            return []
        pv, sp = (("DAT", "DAT_ACTIVE_SP") if rule == "sat-setpoint"
                  else ("DUCT_PRESS", "DUCT_PRESS_SP"))
        if pv not in canon or sp not in canon:
            return []
        s = catalog.settings(rule, unit, cfg)
        gate = _air_gate(upts, series, start, s["settle_min"] * 60)
        if gate is None:
            return []
        a, b = series(canon[pv]["id"], start), series(canon[sp]["id"], start)
        if rule == "sat-setpoint":
            rows = _joined(a, b, lambda x, y: x - y)        # too warm: DAT above setpoint
        else:
            rows = _joined(a, b, lambda x, y: y - x)        # too low: static under setpoint
        point = canon[pv]
    elif rule == "boiler-rise":
        if not {"BLR_CMD", "BLR_SUP_TEMP", "BLR_RET_TEMP"} <= set(canon):
            return []
        s = catalog.settings(rule, unit, cfg)
        cmd = series(canon["BLR_CMD"]["id"], start - _GATE_LOOKBACK_S)
        gate = state.make_gate([state.on_spans(cmd)], s["settle_min"] * 60)
        lo, hi = catalog.SANE_F["BLR_SUP_TEMP"]

        def rise(x, y):          # a failed sensor must neither raise nor hide this alarm
            return x - y if (lo <= x <= hi and lo <= y <= hi) else None
        rows = _joined(series(canon["BLR_SUP_TEMP"]["id"], start),
                       series(canon["BLR_RET_TEMP"]["id"], start), rise)
        point = canon["BLR_SUP_TEMP"]
    else:
        return []
    samples = [(t, v, gate(t)) for t, v in rows]
    eps = episodes(samples, catalog.tiers_for(rule, s), clear=catalog.clear_for(rule, s),
                   clear_s=s["clear_min"] * 60, worse=catalog.worse_for(rule), until=now,
                   initial=seed)
    return [_finding(rule, unit, "", point, e) for e in eps]


def _air_gate(upts, series, start, settle_s):
    """Running (any supply fan) AND occupied (when the unit reports it), each settled."""
    speeds = [p for p in upts if p.get("role") == "FAN_SPEED"]
    fans = [state.on_spans(series(p["id"], start - _GATE_LOOKBACK_S),
                           threshold=state.FAN_SPEED_ON) for p in speeds]
    if not fans:        # no speed signal: a proven supply fan, never the exhaust fan's status
        stats = [p for p in upts if p.get("role") == "FAN_STATUS"
                 and "exhaust" not in (p.get("name") or "").lower()]
        fans = [state.on_spans(series(p["id"], start - _GATE_LOOKBACK_S)) for p in stats]
    if not fans:
        return None
    occ = [p for p in upts if p.get("role") == "OCCUPANCY"
           and p.get("object_type", "").startswith("multi-state")]
    occ.sort(key=lambda p: "status" not in (p.get("name") or "").lower())
    occ_spans = (state.on_spans(state.occupied_series(series(occ[0]["id"],
                                                              start - _GATE_LOOKBACK_S)))
                 if occ else None)
    return state.make_gate([state.union(*fans), occ_spans], settle_s)


def _joined(a: list, b: list, fn) -> list:
    """(t, fn(a, b)) at each of a's samples, b taken as-of t (within _ALIGN_S)."""
    bt = [t for t, _ in b]
    out = []
    for t, x in a:
        i = bisect.bisect_right(bt, t) - 1
        y = b[i][1] if i >= 0 and t - bt[i] <= _ALIGN_S else None
        out.append((t, fn(x, y) if x is not None and y is not None else None))
    return out


# ── point rules ──────────────────────────────────────────────────────────────────────────
def _point_rule(rule, unit, p, vacant, series, cfg, start, end, now, seed=None) -> list:
    role = p.get("role")
    if rule == "sensor-sanity":
        sane = catalog.SANE_F.get(role)
        if vacant or sane is None or (p.get("units_text") or "") != "degrees-fahrenheit":
            return []
        s = catalog.settings(rule, unit, cfg)
        rows = series(p["id"], start, raw=True)
        tiers = catalog.tiers_for(rule, s, sane=sane)
        clear, worse = catalog.clear_for(rule, s, sane=sane), catalog.worse_for(rule, sane)
    elif rule == "bas-alarm":
        if role not in catalog.ALARM_ROLES or not (p.get("object_type") or "").startswith("binary"):
            return []
        crit = catalog.alarm_is_critical(p.get("display_name") or p.get("name"))
        if vacant and not crit:
            return []
        s = catalog.settings(rule, unit, cfg)
        rows = series(p["id"], start)
        tiers = catalog.tiers_for(rule, s, critical=crit)
        clear, worse = catalog.clear_for(rule, s), catalog.worse_for(rule)
    else:
        return []
    eps = episodes([(t, v, True) for t, v in rows], tiers, clear=clear,
                   clear_s=s["clear_min"] * 60, worse=worse, until=now, initial=seed)
    return [_finding(rule, unit, str(p["id"]), p, e) for e in eps]


# ── messages ─────────────────────────────────────────────────────────────────────────────
def _when(t: Optional[int]) -> str:
    return time.strftime("%a %m-%d %H:%M", time.localtime(t)) if t else "now"


def _dur(a: int, b: Optional[int], now: int) -> str:
    m = ((b if b is not None else now) - a) / 60
    return f"{m:.0f} min" if m < 120 else f"{m / 60:.1f} h"


def _num(v: Optional[float], fmt: str) -> str:
    """A peak as text; "?" when there is none to show."""
    return "?" if v is None or math.isnan(v) else format(v, fmt)


def _finding(rule: str, unit: str, key: str, p: dict, e: Episode) -> Finding:
    name = p.get("display_name") or (p.get("name") or "").split("|")[0]
    span = f"from {_when(e.start)} to {_when(e.end)}" if e.end else f"since {_when(e.start)}"
    # A peak can be None: an incident continued from the database with no new breach in this
    # window carries its stored peak, and a NaN peak (a sensor its controller flags
    # unreliable) is stored as NULL. Formatting None crashed every rule on 2026-10-13.
    pk = e.peak
    if rule == "sat-setpoint":
        d = f"{unit}: discharge air {_num(pk, '+.1f')} °F against its setpoint at worst, {span}"
    elif rule == "static-setpoint":
        d = f"{unit}: duct static {_num(pk, '.2f')} in. w.c. under its setpoint at worst, {span}"
    elif rule == "boiler-rise":
        d = f"{unit}: enabled, and only {_num(pk, '+.1f')} °F of rise at best, {span}"
    elif rule == "sensor-sanity":
        shown = "flagged unreliable by its controller" if pk is None or math.isnan(pk) \
            else f"read {pk:.1f} °F"
        d = f"{unit}: {name} {shown} at worst, {span}"
    else:
        d = f"{unit}: {name} on, {span}"
    if e.occurrences > 1:
        d += f" ({e.occurrences} separate spells)"
    return Finding(rule=rule, title=catalog.TITLES[rule], unit=unit, key=key, point=name,
                   tier=e.tier, episode=e, detail=d)
