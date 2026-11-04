"""Deterministic anomaly pre-screen — runs BEFORE the LLM.

This is the difference between "useful reports" and "the AI looked at some numbers."
It surfaces *candidate* anomalies (leads, not conclusions) from cheap, explainable
rules so the model gets pointed at what's off instead of a data dump — and so the
obvious stuff (a controller dropped, a sensor stuck) is caught even if the LLM call
is skipped or unavailable.

Detectors (all read-only over stored samples):
  • no-data / comm-loss — an enabled point with no good samples in the window
  • fault              — a persistent share of fault-quality samples
  • stuck / flatline   — a live analog SENSOR pinned to one value (catches sentinels
                         that slipped the curation net; ignores constant setpoints/
                         commands by whitelisting sensory units only)
  • out-of-range       — good values breach a configured alarm_hi/alarm_lo

Every candidate carries evidence (numbers, not adjectives) for the context block.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional, Sequence

from ..models import Anomaly, Severity
from .baseline import DEFAULT_OCC, is_occupied, occ_schedule, std_floor
from .roles import ANALYSIS_ROLES
from .stats import summarize

# Analog object types whose value is a physical measurement (not a command echo).
_SENSOR_TYPES = {"analog-input", "analog-value", "large-analog-value"}

# Sensory engineering units — a constant reading here is suspicious. Deliberately
# excludes 'percent' / 'no-units' (a VFD legitimately holds 100 %, a setpoint holds
# its target) so flatline detection doesn't cry wolf on commands/setpoints.
_SENSOR_UNITS = frozenset({
    "degrees-fahrenheit", "degrees-celsius", "degrees-kelvin",
    "inches-of-water", "pascals", "kilopascals", "centimeters-of-water",
    "millibars", "bars", "pounds-force-per-square-inch",
    "percent-relative-humidity",
    "cubic-feet-per-minute", "liters-per-second", "cubic-meters-per-hour",
    "gallons-per-minute", "us-gallons-per-minute",
    "btus", "joules", "kilojoules",
})

# Thresholds (tunable). Kept conservative — the LLM is the second gate.
_FAULT_FRACTION = 0.20      # >=20% fault samples -> flag
_FAULT_PERSISTENT = 0.80    # >=80% -> high severity
_COMM_FRACTION = 0.50       # >=50% comm-loss -> flag
_MIN_STUCK_SAMPLES = 12     # need enough samples before "flatline" means anything

# Baseline-aware detectors (need a learned baseline present)
_Z_THRESH = 3.0             # |z| beyond this vs the occ/unocc normal band -> flag
_Z_HIGH = 5.0               # -> high severity
_MIN_BASELINE_N = 20        # don't trust a band with too few samples behind it
_STUCK_CHANGED_FRAC = 0.02  # changed in <2% of polls over the window -> stuck
_REL_MIN_PAIRS = 8          # min paired samples for a relationship-break call

# Minutes after an occupancy transition during which the z-score deviation check is
# suspended. Equipment does not teleport between its occupied and unoccupied states:
# for the first minutes after the schedule flips, a unit is ramping up (or coasting
# down) and its readings legitimately belong to NEITHER learned band. A supply fan
# still at 0% three minutes after a 05:00 start z-scores at -11 sigma against its
# running normal, which is a schedule artifact, not a fault.
# Measured on the live plant before this gate existed: 178 of 267 stored deviation
# alerts (67%) were raised in the 05:00 and 18:00 transition hours, every one of them
# HIGH, and all of them self-resolved once the equipment finished ramping.
_OCC_SETTLE_MIN = 60

# Minutes after a FAN START during which air-side limit breaches are ignored — the duct
# is flushing out room-temperature air that sat in it while the unit was off. Anchored
# to the actual fan-start time rather than the schedule, because it is airflow that
# clears the duct.
_LIMIT_SETTLE_MIN = 30

# Drift / time-to-limit (wires baseline.long_slope = value/day). This is deliberately
# CONSERVATIVE: a slope fit over too little history is just a diurnal ramp, and a point
# sitting at its mean creeps toward the ±3-sigma edge on pure noise — either produces a
# flood of meaningless "reaches the edge in N days" leads. So a drift only counts when
# (a) enough real samples back the slope, (b) it clears the band's own noise, and (c)
# the point has ALREADY left the middle of its band and is heading further out.
_DRIFT_HORIZON_DAYS = 21    # project the slope this far ahead
_DRIFT_MIN_DAYS = 5         # days of history the slope needs behind it to be trusted
_DRIFT_SIG_FRAC = 0.15      # |slope/day| must exceed this * band std to count as drift
_DRIFT_SAMPLES_PER_DAY = 300  # occ samples/day floor (~390 at 120s polls) -> min_days*this
_DRIFT_MIN_OFFSET_SIG = 1.0   # last must be >= this many sigma out (in the drift dir)
_DRIFT_MIN_MOVE_SIG = 0.5     # projected excursion must also clear this * band std
_DRIFT_SOON_DAYS = 7        # reaching a limit within a week -> high severity
_PCT_ROLES = frozenset({"OA_DAMPER", "EF_DAMPER", "FAN_SPEED", "EXH_FAN_SPEED",
                        "HTG_VALVE", "CLG_VALVE"})   # physically bounded 0-100%

# Stuck detection applies ONLY to sensors that should always vary with conditions.
# A valve/damper/VFD legitimately holds still when the unit is idle (esp. overnight),
# so including actuators floods false positives — a temp/humidity sensor that never
# moves over a day IS suspect.
_STUCK_SENSOR_ROLES = frozenset({
    "MAT", "RAT", "DAT", "OAT", "MECH_TEMP", "ROOM_TEMP", "HUMIDITY"})

# COMMANDED actuator outputs (positions the controller/operator DRIVES, not measurements).
# The z-score deviation detector compares a point to its learned historical band, which
# is the right test for a SENSOR — but a commanded output legitimately moves with control
# demand and with operator setpoint changes, so z-scoring it flags every intentional
# change as a "deviation". A damper parked at a minimum the operator RAISED reads as a
# +6-sigma fault against a baseline built at the old minimum, which it is not. Real
# actuator faults are caught by the stuck detector (never moves) and the relationship-
# break detector (doesn't track its driver); z-score deviation is left to the sensors.
_COMMANDED_ROLES = frozenset({"OA_DAMPER", "EF_DAMPER", "HTG_VALVE", "CLG_VALVE"})


def _round(v: Optional[float], nd: int = 2) -> Optional[float]:
    return None if v is None else round(v, nd)


# Text a BAS uses for "this alarm is asserted". Checked before the numeric value so a
# multistate point whose ordinal happens to be 0 for an active state is still caught.
# Roles whose "active" state IS the finding, no statistics required. RESET_REQUIRED
# belongs here as much as ALARM: "SF~-A Man Reset Required" is the equipment saying it
# needs a human to go touch it, which is the most actionable state a point can report.
_ALARM_LIKE_ROLES = frozenset({"ALARM", "RESET_REQUIRED"})

_ALARM_ACTIVE_TEXT = frozenset({
    "active", "alarm", "on", "true", "fault", "failed", "failure", "trouble", "yes"})
_ALARM_CLEAR_TEXT = frozenset({
    "inactive", "normal", "off", "false", "ok", "no", "clear", "none"})


def _is_active(r: dict) -> bool:
    """Is this sample an asserted alarm? Prefers the decoded state text — a BAS that
    reports 'active'/'inactive' is unambiguous, whereas the numeric encoding is not
    guaranteed to be 1=bad."""
    t = str(r.get("value_text") or "").strip().lower()
    if t in _ALARM_ACTIVE_TEXT:
        return True
    if t in _ALARM_CLEAR_TEXT:
        return False
    v = r.get("value_num")
    return v is not None and v > 0.5


def screen(dal: Any, start_utc: int, end_utc: int, *,
           points: Optional[Sequence[dict]] = None, config: Any = None,
           use_baselines: bool = True,
           localtime: Callable = time.localtime) -> list[Anomaly]:
    """Run every detector over each enabled point's window. Returns candidates
    sorted most-severe first. `points` can be supplied to scope/inject; defaults
    to dal.points_for_analysis(). When baselines exist, the occupancy-aware
    deviation / stuck / relationship detectors run too."""
    pts = list(points) if points is not None else dal.points_for_analysis()
    baselines = dal.baselines() if use_baselines else {}
    sched = occ_schedule(config)
    from .baseline import _preds_by_system      # per-unit hours (staggered starts etc.)
    preds = _preds_by_system(dal, config, localtime)
    horizon = _drift_horizon(config)
    settle = occ_settle_min(config)
    from .derived import running_by_system, running_windows   # airflow gates
    runmap = running_by_system(dal, pts, start_utc, end_utc)
    runspans = running_windows(dal, pts, start_utc, end_utc)
    out: list[Anomaly] = []
    for p in pts:
        rows = dal.query_series(p["id"], start_utc, end_utc)
        s = summarize(rows)
        out.extend(detect_point(p, s, running=runmap.get(p.get("system_id")),
                                rows=rows, run_spans=runspans.get(p.get("system_id")),
                                localtime=localtime))
        if p["id"] in baselines:
            out.extend(detect_point_baseline(
                p, s, baselines[p["id"]],
                preds.get(p.get("system_id"), sched), localtime,
                settle_min=settle))
            out.extend(detect_point_drift(p, s, baselines[p["id"]], horizon_days=horizon))
    if use_baselines:
        out.extend(detect_relationships(dal, start_utc, end_utc, points=pts, config=config))
    from .derived import diagnose         # role-derived physics checks (P1)
    # pass config: diagnose re-derives the per-unit occupancy day-maps AND the
    # economizer changeover thresholds from it. Dropping it makes this (the ALERT)
    # path fall back to hardcoded defaults while the report path uses the operator's
    # configured values — two silently different verdicts over the same data.
    out.extend(diagnose(dal, pts, start_utc, end_utc, sched=sched,
                        localtime=localtime, config=config, preds=preds))
    out.sort(key=lambda a: a.severity.rank(), reverse=True)
    return out


def detect_point(p: dict, s: dict, *, running: Optional[bool] = None,
                 rows: Optional[Sequence[dict]] = None,
                 run_spans: Optional[list] = None,
                 localtime: Callable = time.localtime) -> list[Anomaly]:
    """Run the detectors on one point given its window summary. Public so the
    context builder can reuse a single series fetch instead of re-querying.

    `running` is the unit's airflow state (see derived.running_by_system): False
    suppresses the out-of-range check, because an air-side limit only means anything
    when air is moving. None = unknown, which never suppresses.

    `rows` enables the fired-alarm check, which needs WHEN the alarm went active, not
    just that it did."""
    found: list[Anomaly] = []
    code = p.get("system_code") or "?"
    name = p.get("name") or f"point {p['id']}"

    def anom(kind, sev, metric, detail, **ev):
        found.append(Anomaly(kind=kind, system_code=code, point_id=p["id"],
                             point_name=name, severity=sev, metric=metric,
                             detail=detail, evidence=ev))

    # 0) A BAS ALARM POINT THAT FIRED. This needs no statistics and outranks everything
    # else in this function: the equipment is telling us directly that something is
    # wrong, with a timestamp.
    #
    # These 102 enabled ALARM points (Multistack/compressor alarm, SF-A/B Failure,
    # Freezestat, Freeze Condition, Exhaust Fan Failure, Smoke Purge Call) were
    # completely invisible to the analysis: ALARM is a STATUS role, so no detector ran
    # on it, and in the prompt each was one line among 611 showing a window mean of
    # ~0.02 — two active 5-minute buckets out of 288 rounds to nothing.
    # THE COST, from a real report: on Mon 08:45 the Multistack (compressor) alarm went
    # active on AC-2, AC-3, AC-5 and AC-12 — at the EXACT minute all eight units began
    # exceeding their discharge-air high limit. The report never mentioned the alarms
    # and blamed "the fleet's recurring missed-free-cooling pattern" instead, on a
    # morning far too warm and humid to economize. The operator had to point out that
    # compressors in alarm are the obvious cause of high discharge air.
    # Emitting the alarm WITH ITS LOCAL TIMESTAMP is what lets the correlation be made.
    if p.get("role") in _ALARM_LIKE_ROLES and rows:
        act = [r for r in rows
               if r.get("quality") == "good" and _is_active(r)]
        if act:
            t0, t1 = act[0]["ts_utc"], act[-1]["ts_utc"]
            span = (time.strftime("%a %H:%M", localtime(t0)) if t0 == t1 else
                    f"{time.strftime('%a %H:%M', localtime(t0))}-"
                    f"{time.strftime('%a %H:%M', localtime(t1))}")
            anom("alarm", Severity.HIGH, "ALARM",
                 f"{code} {name}: BAS ALARM ACTIVE {span} local "
                 f"({len(act)} sample(s)). This is the equipment reporting a fault "
                 f"itself — check whether it explains other anomalies on {code} at the "
                 f"same time.",
                 active_n=len(act), first_utc=t0, last_utc=t1,
                 first_local=time.strftime("%Y-%m-%d %H:%M", localtime(t0)),
                 last_local=time.strftime("%Y-%m-%d %H:%M", localtime(t1)))

    n = s["n"]
    # 1) no data at all for an enabled point in this window
    if n == 0:
        anom("comm-loss", Severity.MEDIUM, "availability",
             f"{code} {name}: no samples logged in the window (not polled / device down).",
             good_n=0)
        return found

    good_frac = s["good_n"] / n
    # 2) comm-loss dominates
    if s["comm_n"] / n >= _COMM_FRACTION:
        anom("comm-loss", Severity.HIGH, "availability",
             f"{code} {name}: {s['comm_n']}/{n} samples comm-loss "
             f"({s['comm_n']/n:.0%}).", comm_n=s["comm_n"], n=n)
    # 3) persistent fault quality
    if n and s["fault_n"] / n >= _FAULT_FRACTION:
        sev = Severity.HIGH if s["fault_n"] / n >= _FAULT_PERSISTENT else Severity.MEDIUM
        anom("fault", sev, "reliability",
             f"{code} {name}: {s['fault_n']}/{n} samples flagged fault "
             f"({s['fault_n']/n:.0%}); last good value {_round(s['last_good'])}.",
             fault_n=s["fault_n"], n=n, last_good=_round(s["last_good"]))
    # 4) stuck / flatline live sensor. Respect the role when classified: a setpoint /
    # config / actuator is SUPPOSED to hold constant (45F freeze cutout, 1.2" static
    # setpoint) — only a real sensor flatlining is a fault. Unclassified points
    # (role None/OTHER) fall back to the units heuristic.
    role = p.get("role")
    if (role in _STUCK_SENSOR_ROLES or role is None) \
            and p.get("object_type") in _SENSOR_TYPES \
            and (p.get("units_text") in _SENSOR_UNITS) \
            and s["good_n"] >= _MIN_STUCK_SAMPLES and s["constant"]:
        anom("stuck", Severity.MEDIUM, "sensor",
             f"{code} {name}: flatlined at {_round(s['min'])} "
             f"{p.get('units_text') or ''} across {s['good_n']} good samples "
             f"(zero variance) — possible stuck sensor.",
             value=_round(s["min"]), good_n=s["good_n"])
    # 5) out-of-range vs configured alarm thresholds — evaluated ONLY over samples taken
    # WHILE THE UNIT WAS MOVING AIR.
    #
    # These are air-side limits; an idle unit's duct equalises to room temperature and
    # trips a discharge high-limit while doing nothing wrong. A window-wide "running"
    # flag is not sufficient, because it is an AVERAGE and this check reads an EXTREME:
    # a unit that runs 05:00-18:00 averages "running" across 24 h, and its overnight
    # still air then supplies the window max. That produced "Eleven AHUs exceeded their
    # discharge-air-temp alarm-high limits overnight" on a night the fans sat at
    # 0.2-0.4%. `run_spans` narrows it to the moments air was actually moving.
    if p.get("alarm_enabled") and s["good_n"] and running is not False:
        hi, lo = p.get("alarm_hi"), p.get("alarm_lo")
        vals = None
        if rows is not None and run_spans is not None:
            from .derived import in_windows
            # ...and not during the flush-out period right after the fan STARTS. A duct
            # that has sat still overnight is full of room-temperature air; the first
            # minutes of airflow push that past the sensor, which is the unit working,
            # not an over-temperature fault. Measured: after the idle-unit fix above,
            # EVERY remaining breach landed at Tue 05:10-05:56 — i.e. all ten were the
            # 05:00 start-up flush. Anchoring to the actual fan-start time (from
            # run_spans) is sharper than an occupancy-schedule settle, because it is the
            # airflow that clears the duct, not the clock.
            def _settled(t: int) -> bool:
                for a, b in (run_spans or []):
                    if a <= t <= b:
                        return (t - a) >= _LIMIT_SETTLE_MIN * 60
                return False
            vals = [(r["ts_utc"], r["value_num"]) for r in rows
                    if r.get("quality") == "good" and r.get("value_num") is not None
                    and in_windows(r["ts_utc"], run_spans) and _settled(r["ts_utc"])]
            if not vals:
                vals = []                       # ran at no point -> nothing to judge
        if vals is None:                        # no series available: fall back
            vmax, vmin, when_hi, when_lo = s["max"], s["min"], None, None
        elif vals:
            when_hi, vmax = max(vals, key=lambda x: x[1])
            when_lo, vmin = min(vals, key=lambda x: x[1])
        else:
            vmax = vmin = None
            when_hi = when_lo = None
        if hi is not None and vmax is not None and vmax > hi:
            at = (f" at {time.strftime('%a %H:%M', localtime(when_hi))} local"
                  if when_hi else "")
            anom("out-of-range", Severity.HIGH, "limit",
                 f"{code} {name}: reached {_round(vmax)} while running, above its "
                 f"alarm_hi {hi}{at}.",
                 max=_round(vmax), alarm_hi=hi, at_utc=when_hi)
        if lo is not None and vmin is not None and vmin < lo:
            at = (f" at {time.strftime('%a %H:%M', localtime(when_lo))} local"
                  if when_lo else "")
            anom("out-of-range", Severity.HIGH, "limit",
                 f"{code} {name}: fell to {_round(vmin)} while running, below its "
                 f"alarm_lo {lo}{at}.",
                 min=_round(vmin), alarm_lo=lo, at_utc=when_lo)
    return found


def detect_point_baseline(p: dict, s: dict, baseline: dict,
                          sched: dict = DEFAULT_OCC,
                          localtime: Callable = time.localtime,
                          settle_min: int = _OCC_SETTLE_MIN) -> list[Anomaly]:
    """Baseline-aware detectors for one point: occupancy-partitioned z-score
    deviation, and a baseline-backed stuck signal (changed in <2% of polls over the
    whole window — stronger than a single-day flatline). Analysis roles only.

    `settle_min` suppresses the deviation check for that many minutes after an
    occupancy transition in `sched` (see `_OCC_SETTLE_MIN`); 0 disables the gate."""
    found: list[Anomaly] = []
    role = p.get("role")
    if role not in ANALYSIS_ROLES:
        return found
    code = p.get("system_code") or "?"
    name = p.get("name") or f"point {p['id']}"
    # use the last GOOD reading — a faulted sentinel (356F) must not drive a deviation
    last, last_ts = s.get("last_good"), s.get("last_ts")

    # z-score vs the normal band matching the last sample's occupancy state. Skip for
    # COMMANDED outputs (dampers/valves) — a driven position following demand or an
    # operator setpoint change is not "deviating"; z-scoring it just flags the change.
    # ...and skip while the unit is still SETTLING after a schedule transition: the
    # band we would compare against describes the steady state the equipment has not
    # reached yet. Detect it by asking whether the occupancy state differs from what it
    # was `settle_min` ago — cheap, and DST-safe because is_occupied works in local
    # wall-clock minutes.
    settling = bool(
        last_ts is not None and settle_min > 0
        and is_occupied(last_ts - settle_min * 60, sched, localtime)
        != is_occupied(last_ts, sched, localtime))
    if last is not None and last_ts is not None \
            and role not in _COMMANDED_ROLES and not settling:
        mode = "occ" if is_occupied(last_ts, sched, localtime) else "unocc"
        band = baseline.get(mode)
        if band and band.get("n", 0) >= _MIN_BASELINE_N and band.get("std"):
            z = (last - band["mean"]) / band["std"]
            # EQUIPMENT RUNNING WHEN IT IS SUPPOSED TO RUN IS NEVER A FAULT.
            # A bounded output (fan VFD, damper, valve) whose learned occupied band sits
            # near the OFF rail will z-score hugely the moment it does its job. AC-8's
            # supply fan was reported at "+4.9 sigma vs occ normal 2.0+/-11.62%" for
            # running at 59% during occupied hours — the band only says the unit was
            # mostly off across the baseline window (which itself included a 23 h
            # outage). Flagging a fan for STARTING is backwards.
            # Deliberately NOT suppressed for a unit the operator marked VACANT: there,
            # running IS the news, and that is how "AC-9 is not idling as documented"
            # gets caught.
            vacant = str(p.get("system_occupancy") or "").lower() == "vacant"
            if (role in _PCT_ROLES and mode == "occ" and z > 0 and not vacant):
                pass                       # working harder than a mostly-off baseline
            elif abs(z) >= _Z_THRESH:
                sev = Severity.HIGH if abs(z) >= _Z_HIGH else Severity.MEDIUM
                found.append(Anomaly(
                    kind="deviation", system_code=code, point_id=p["id"],
                    point_name=name, severity=sev, metric=role,
                    detail=(f"{code} {name}: last {_round(last)} is {z:+.1f}sigma vs "
                            f"{mode} normal {_round(band['mean'])}+/-{_round(band['std'])} "
                            f"(n={band['n']})."),
                    evidence={"z": round(z, 2), "last": _round(last), "mode": mode,
                              "mean": _round(band["mean"]), "std": _round(band["std"])}))

    # baseline-backed stuck — analog sensors only (actuators legitimately rest when
    # idle; a binary demand flag like NEED_DEHUMID legitimately sits at 0).
    cf = baseline.get("changed_frac")
    if (role in _STUCK_SENSOR_ROLES and p.get("object_type") in _SENSOR_TYPES
            and cf is not None and cf < _STUCK_CHANGED_FRAC
            and s.get("good_n", 0) >= _MIN_STUCK_SAMPLES):
        found.append(Anomaly(
            kind="stuck", system_code=code, point_id=p["id"], point_name=name,
            severity=Severity.MEDIUM, metric=role,
            detail=(f"{code} {name}: changed in only {cf:.0%} of polls over the "
                    f"baseline window — likely stuck."),
            evidence={"changed_frac": round(cf, 4)}))
    return found


def _drift_horizon(config: Any) -> int:
    """analysis.drift.horizon_days (days to project a slope), default _DRIFT_HORIZON_DAYS."""
    try:
        node = config.get("analysis").get("drift") if config else None
        if node:
            d = node.as_dict() if hasattr(node, "as_dict") else dict(node)
            return int(d.get("horizon_days", _DRIFT_HORIZON_DAYS))
    except (AttributeError, TypeError, ValueError):
        pass
    return _DRIFT_HORIZON_DAYS


def occ_settle_min(config: Any) -> int:
    """analysis.deviation.settle_min — minutes after an occupancy transition during
    which the z-score deviation check is suspended. 0 disables the gate.

    Read by BOTH the alert path (screen) and the report path (context.build_context);
    they must resolve it identically or the two disagree about the same data."""
    try:
        node = config.get("analysis").get("deviation") if config else None
        if node:
            d = node.as_dict() if hasattr(node, "as_dict") else dict(node)
            return max(0, int(d.get("settle_min", _OCC_SETTLE_MIN)))
    except (AttributeError, TypeError, ValueError):
        pass
    return _OCC_SETTLE_MIN


def detect_point_drift(p: dict, s: dict, baseline: dict, *,
                       horizon_days: int = _DRIFT_HORIZON_DAYS,
                       min_days: int = _DRIFT_MIN_DAYS) -> list[Anomaly]:
    """Slow-drift / time-to-limit detector — wires the baseline's `long_slope`
    (value per day, fit over the rolling baseline window). A sensor creeping a
    fraction of a degree a day reads as normal on any single day but walks a unit
    toward a limit over weeks; the per-point z-score and the daily flatline both miss
    it. When a point is drifting fast enough to matter AND heading toward a limit
    (a configured alarm band, else the learned mean ± 3 sigma) within the horizon, we
    project the ETA and flag it — the actionable lead a snapshot can't produce.

    Analysis roles only; needs a trusted baseline band behind the slope. Skips COMMANDED
    outputs — a damper stepping from a 20% to a 35% operator-set minimum reads as a slow
    "trend toward a limit" over the rolling window, but it is a setpoint change, not
    drift."""
    found: list[Anomaly] = []
    role = p.get("role")
    if role not in ANALYSIS_ROLES or role in _COMMANDED_ROLES:
        return found
    slope = baseline.get("long_slope")          # value per day
    if not slope:                               # None or 0.0 -> no drift
        return found
    # pick the better-supported occupancy band as the deviation scale + trust gate
    bands = [b for b in (baseline.get("occ"), baseline.get("unocc"))
             if b and b.get("std") and b.get("mean") is not None]
    if not bands:
        return found
    band = max(bands, key=lambda b: b.get("n", 0) or 0)
    # (a) enough real samples behind the slope (a short window fits a diurnal ramp)
    min_n = max(_MIN_BASELINE_N, int(min_days * _DRIFT_SAMPLES_PER_DAY))
    if (band.get("n") or 0) < min_n:
        return found
    std, mean = band["std"], band["mean"]
    if abs(slope) < _DRIFT_SIG_FRAC * std:      # (b) within the noise of the band
        return found
    last = s.get("last_good")
    if last is None:
        return found

    up = slope > 0
    # (c) the point must already sit >= 1 sigma out IN the drift direction — i.e. it has
    #     left the middle of its band and is heading further out, not creeping off center.
    offset_sig = ((last - mean) if up else (mean - last)) / std
    if offset_sig < _DRIFT_MIN_OFFSET_SIG:
        return found

    # a physically-bounded actuator (0-100%) pinned at a rail isn't drifting — skip
    role = p.get("role")
    if role in _PCT_ROLES and (last >= 99.0 or last <= 1.0):
        return found

    # candidate limits ahead of the current value in the drift direction
    targets: list[tuple[str, float, bool]] = []
    if p.get("alarm_enabled"):
        hard = p.get("alarm_hi") if up else p.get("alarm_lo")
        if hard is not None and ((hard > last) if up else (hard < last)):
            targets.append(("alarm limit", float(hard), True))
    soft = mean + 3 * std if up else mean - 3 * std
    if role in _PCT_ROLES:                       # clamp the soft edge to the physical range
        soft = min(100.0, max(0.0, soft))
    if (soft > last) if up else (soft < last):
        targets.append(("normal-band edge", soft, False))
    if not targets:
        return found
    # nearest limit ahead = smallest positive time-to-reach
    label, target, is_hard = min(targets, key=lambda t: abs(t[1] - last))
    days = (target - last) / slope               # slope & (target-last) share sign -> >0
    if not (0 < days <= horizon_days):
        return found

    # (d) the projected excursion has to MEAN something. For the soft band edge, gates
    # (b) and (c) together make the horizon test vacuous: (b) forces the slope above
    # 0.15*std/day and (c) forces the point at least 1 sigma out, so the remaining
    # distance to the +3-sigma edge is at most 2 sigma and is always "reached" within
    # 2/0.15 = 13.3 days — inside the 21-day horizon by construction. The horizon can
    # therefore never reject, and the detector fires on noise: on the live plant it
    # produced HIGH alerts like "AC-10 RAT drifting -0.14/day ... reaches the normal-band
    # edge 69.63 in ~1 days", a forecast movement of 0.12 degF. Require the move to clear
    # the point's own meaningful-change floor. A CONFIGURED alarm limit is exempt —
    # if the operator set a limit, approaching it is worth saying however small the step.
    if not is_hard and abs(target - last) < max(2.0 * std_floor(p.get("units_text")),
                                                _DRIFT_MIN_MOVE_SIG * std):
        return found

    code = p.get("system_code") or "?"
    name = p.get("name") or f"point {p['id']}"
    sev = Severity.HIGH if days <= _DRIFT_SOON_DAYS else Severity.MEDIUM
    found.append(Anomaly(
        kind="drift", system_code=code, point_id=p["id"], point_name=name,
        severity=sev, metric=role,
        detail=(f"{code} {name}: drifting {slope:+.2f}/day (now {_round(last)}); at this "
                f"rate it reaches the {label} {_round(target)} in ~{days:.0f} days."),
        evidence={"slope_per_day": round(slope, 3), "last": _round(last),
                  "target": _round(target), "days_to_limit": round(days, 1),
                  "limit_type": "alarm" if is_hard else "band"}))
    return found


def detect_relationships(dal: Any, start_utc: int, end_utc: int, *,
                         points: Optional[Sequence[dict]] = None,
                         config: Any = None) -> list[Anomaly]:
    """Relationship-break detector: for each learned correlation (setpoint↔DAT,
    OAT↔MAT, fan↔duct-static), check whether the window's paired values sit
    systematically off the baseline fit (mean residual beyond 3x residual_std) —
    a control-loop fault a per-point band can't see.

    The setpoint↔DAT relationship-break is an ALARM about discharge temp not tracking
    its setpoint, so it is gated by the same analysis.checks.dat_vs_setpoint flag as the
    control-loop check (OFF by default; the pair is still learned for the viewer)."""
    corrs = dal.corrs()
    if not corrs:
        return []
    from .derived import _dat_sp_alarm
    dat_sp_alarm = _dat_sp_alarm(config)
    meta = {p["id"]: p for p in (points if points is not None
                                 else dal.points_for_analysis())}
    found: list[Anomaly] = []
    for c in corrs:
        if c.get("kind") == "sp_dat" and not dat_sp_alarm:
            continue                        # discharge-temp-vs-setpoint alarm off for now
        xid, yid, rstd = c.get("x_point_id"), c.get("y_point_id"), c.get("residual_std")
        if not xid or not yid or not rstd or c.get("slope") is None:
            continue
        xs = {r["ts_utc"]: r["value_num"] for r in dal.query_series(xid, start_utc, end_utc)
              if r.get("quality") == "good" and r.get("value_num") is not None}
        ys = {r["ts_utc"]: r["value_num"] for r in dal.query_series(yid, start_utc, end_utc)
              if r.get("quality") == "good" and r.get("value_num") is not None}
        pairs = [(xs[t], ys[t]) for t in xs.keys() & ys.keys()]
        if len(pairs) < _REL_MIN_PAIRS:
            continue
        resid = [y - (c["slope"] * x + c["intercept"]) for x, y in pairs]
        mean_resid = sum(resid) / len(resid)
        zr = mean_resid / rstd
        if abs(zr) < _Z_THRESH:
            continue
        ym = meta.get(yid, {})
        cd = ym.get("system_code") or "?"
        sev = Severity.HIGH if abs(zr) >= _Z_HIGH else Severity.MEDIUM
        found.append(Anomaly(
            kind="relationship", system_code=cd, point_id=yid,
            point_name=ym.get("name") or f"point {yid}", severity=sev, metric=c["kind"],
            detail=(f"{cd} {c['kind']}: {ym.get('name','')} runs {mean_resid:+.1f} off "
                    f"its expected relationship to the driver ({abs(zr):.1f}sigma, "
                    f"n={len(pairs)})."),
            evidence={"kind": c["kind"], "mean_residual": round(mean_resid, 2),
                      "z": round(zr, 2), "n": len(pairs)}))
    return found
