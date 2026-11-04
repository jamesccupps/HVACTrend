"""Role-derived physics checks.

Once points carry semantic roles, cross-point relationships become computable —
"discharge air should track its active setpoint", "mixed air should sit between
outdoor and return", "don't heat and cool at once". These are deterministic leads
for the report (like the pre-screen), grounded in physics rather than statistics.

Each system's canonical role values (window means, + last state-text for mode/comm)
are gathered once, then a set of rules emits Anomaly candidates. Read-only.
"""

from __future__ import annotations

import math
import re
import time
from typing import Any, Callable, Optional, Sequence

from ..models import Anomaly, Severity
from .baseline import DEFAULT_OCC, is_occupied

# Roles where several points coexist (stages) — aggregate as "any active".
_STAGE_ROLES = {"COOLING_STAGES", "HEATING_STAGES"}
# Roles whose decoded state-text carries the meaning (not a number).
_TEXT_ROLES = {"MODE", "COMM", "OCCUPANCY"}

# Roles comparable across the near-identical AHU fleet (peer-outlier detection).
_FLEET_ROLES = {"DAT", "MAT", "RAT", "DUCT_PRESS", "FAN_SPEED", "HTG_VALVE", "OA_DAMPER"}
_FLEET_MIN = 4          # need enough peers before "outlier" means anything
_FLEET_Z = 2.5          # sigma from the fleet mean to flag

# Roles whose RAW value is not comparable across the fleet, and the per-unit target to
# subtract first so the compared quantity is CONTROL ERROR. These AHUs are near-identical
# hardware commissioned to DIFFERENT targets (discharge setpoints here span 55-69 F), so
# comparing raw readings measures commissioning, not health. A role listed here is
# SKIPPED for a unit with no target — never compared raw alongside errors.
_FLEET_REF = {
    "DAT": "DAT_ACTIVE_SP",
    "DUCT_PRESS": "DUCT_PRESS_SP",
    "OA_DAMPER": "OA_DAMPER_MIN_SP",
}
# Smallest deviation-from-peers worth reporting, in each role's own units. Sigma alone is
# not evidence: on a tightly-matched fleet a physically trivial spread still scores high.
_FLEET_ABS_MIN = {
    "DAT": 2.0, "MAT": 2.0, "RAT": 2.0,                       # degF
    "DUCT_PRESS": 0.15,                                       # in wc
    "FAN_SPEED": 8.0, "HTG_VALVE": 10.0, "OA_DAMPER": 8.0,    # percent
}
_FLEET_UNITS = {
    "DAT": "F", "MAT": "F", "RAT": "F", "DUCT_PRESS": " in wc",
    "FAN_SPEED": "%", "HTG_VALVE": "%", "OA_DAMPER": "%",
}

# Thresholds (tunable; conservative — the LLM is the second gate).
_DAT_DEV = 5.0          # deg F: |DAT - active setpoint| beyond this -> flag
_DSP_ERR = 0.3          # in w.c.: |duct static - setpoint| beyond this
_HTG_ON = 10.0          # % heating-valve open to count as heating
_MIXED_RESID = 6.0      # deg F: |MAT - blend(OAT,RAT,damper)| beyond this
_DAMPER_WIDE = 0.5      # fraction: damper more open than this
_DAMPER_MIN = 0.25      # fraction: damper near minimum

# Economizer changeover defaults. Free cooling is an ENTHALPY (total-heat) decision,
# not a dry-bulb one — 60 degF/90% RH outdoor air carries more heat than 75 degF/30%,
# so a dry-bulb "it's cool out" test drags moisture in and overworks the coils. We
# judge against the WEATHER table's measured wet-bulb / enthalpy (accurate on-site
# Tempest), falling back to dry-bulb only when no weather is stored.
_ECON_ENTH_CHANGEOVER = 28.0   # Btu/lb dry air; OA above -> lock economizer out
_ECON_WB_CHANGEOVER = 66.0     # deg F wet-bulb fallback when enthalpy absent
_ECON_FREE_OAT = 55.0          # deg F dry-bulb, last-resort fallback (no weather)
_ECON_HOT_OAT = 70.0           # deg F dry-bulb, last-resort fallback (no weather)


def _econ_cfg(config: Any) -> dict:
    """analysis.economizer changeover thresholds, with defaults."""
    out = {"enth": _ECON_ENTH_CHANGEOVER, "wb": _ECON_WB_CHANGEOVER}
    try:
        node = config.get("analysis").get("economizer") if config else None
        if node:
            d = node.as_dict() if hasattr(node, "as_dict") else dict(node)
            out["enth"] = float(d.get("enthalpy_changeover", out["enth"]))
            out["wb"] = float(d.get("wetbulb_changeover_f", out["wb"]))
    except (AttributeError, TypeError, ValueError):
        pass
    return out


def _econ_favorable(weather: Optional[dict], oat: Optional[float],
                    econ: dict) -> tuple[Optional[bool], str]:
    """Is outdoor air favorable for free cooling? Prefer measured enthalpy, then
    wet-bulb, then (only if no weather at all) the unit's own dry-bulb OAT sensor.
    Returns (favorable | None-if-unknowable, human driver string)."""
    if weather:
        enth = weather.get("enth_avg")
        if enth is not None:
            return enth < econ["enth"], f"OA enthalpy {enth:.1f} Btu/lb (changeover {econ['enth']:.0f})"
        wb = weather.get("wb_avg")
        if wb is not None:
            return wb < econ["wb"], f"OA wet-bulb {wb:.0f}F (changeover {econ['wb']:.0f}F)"
    if oat is not None:                     # last resort — no weather stored
        # prefer the unit's own dry-bulb changeover setpoint when it trends one
        db = econ.get("oat", _ECON_FREE_OAT)
        return oat < db, f"OA dry-bulb {oat:.0f}F (changeover {db:.0f}F; no weather)"
    return None, "outdoor conditions unknown"


def _bool_state(rv: dict, role: str) -> Optional[bool]:
    """Decode a binary/multistate role to True/False, preferring the decoded state text
    ('active'/'inactive') and falling back to the numeric mean. None when absent —
    callers must treat that as 'unknown', never as False."""
    r = rv.get(role)
    if not r:
        return None
    t = (r.get("text") or "").strip().lower()
    if t in ("active", "on", "true", "yes", "enabled", "ok"):
        return True
    if t in ("inactive", "off", "false", "no", "disabled"):
        return False
    m = r.get("mean")
    return None if m is None else m > 0.5


def _num(rv: dict, role: str) -> Optional[float]:
    r = rv.get(role)
    return r["mean"] if r else None


def _text(rv: dict, role: str) -> Optional[str]:
    r = rv.get(role)
    return (r.get("text") or "").lower() if r else None


# --- cooling tower / condenser water ----------------------------------------
# Every threshold here is a DESIGN RULE OF THUMB, not a commissioned plant value. Where
# the plant publishes its own target (supply-temp setpoint, differential-pressure
# setpoint) that value is used directly and none of these constants apply -- the same
# rule that ai/limits.py and ai/reconcile.py exist to enforce.
_CT_APPROACH_HI = 10.0        # F above wet-bulb before a tower looks fouled/undersized
# % fan speed at or above which the tower has effectively run out of headroom. A wide
# approach is only a FAULT signal when the fan is already near flat out and still cannot
# close the gap; at part speed a wide approach just means the loop is not asking for more.
# 30 was the first value tried and was wrong on this plant for a concrete reason: CT-1
# IDLES at 33% overnight, so a 30% gate filtered nothing and the "loaded" mean was still
# a 24-hour blend (9.6 F) that hid a solid 11-14 F daytime approach.
_CT_APPROACH_MIN_LOAD = 80.0
_CW_SP_DEV_F = 3.0            # F off the plant's OWN supply setpoint
_CW_DP_DEV_PCT = 20.0         # % off the plant's OWN differential-pressure setpoint
_CMD_FBK_TOL = 15.0           # % gap between a commanded and a proved position


def detect_tower_plant(srv, weather):
    """Condenser-loop and per-tower checks.

    APPROACH (supply temp - ambient wet-bulb) is the one number that says whether a
    tower is actually working: wet-bulb is the physical floor the tower is chasing, so
    approach normalises out the weather in a way raw supply temperature cannot. A tower
    holding 75 F on a 60 F wet-bulb day is loafing; the same 75 F on a 72 F wet-bulb day
    is a tower that has almost nothing left. Fouled fill, scale, a slipping fan or a
    restricted air path all show up here first and in supply temp not at all.
    """
    out = []
    wb = (weather or {}).get("wb_avg")

    for sysd in srv.values():
        roles = sysd["roles"]

        # -- supply temp vs the plant's OWN setpoint --------------------------
        # DIRECTIONAL, and it must stay that way. A cooling tower can only reject heat --
        # it has no way to WARM the water -- so on a cool night the supply falls below
        # setpoint and the only control action available is to stage fans off and open the
        # bypass. Running below a cooling setpoint is the plant working, not a fault.
        # Measured over 14 days on this plant: 110 of 305 hours breached +/-3 F, but 83 of
        # them were BELOW (worst 9.1 F below) and only 27 above. A symmetric test is
        # therefore ~75% false. This is the same conclusion the DAT_LO_LIMIT / RAT_HI_LIMIT
        # measurement reached in config.yaml -- a limit the loop controls TOWARD is not an
        # alarm threshold. Only "cannot make setpoint" is a finding.
        sup, sp = _num(roles, "CW_SUP_TEMP"), _num(roles, "CW_SUP_TEMP_SP")
        if sup is not None and sp is not None and (sup - sp) > _CW_SP_DEV_F:
            out.append(Anomaly(
                kind="cw-supply-setpoint", system_code=sysd["code"],
                point_id=roles["CW_SUP_TEMP"]["pid"],
                point_name=roles["CW_SUP_TEMP"]["name"],
                severity=Severity.MEDIUM, metric="CW_SUP_TEMP",
                detail=("condenser water supply {0:.1f} F is {1:.1f} F ABOVE its own "
                        "setpoint {2:.1f} F -- the plant is not making setpoint. Check "
                        "tower capacity, fan staging and condenser flow. (Supply BELOW "
                        "setpoint is normal on a cool night and is not reported: a tower "
                        "cannot heat water.)").format(sup, sup - sp, sp),
                evidence={"supply_f": round(sup, 2), "setpoint_f": round(sp, 2),
                          "deviation_f": round(sup - sp, 2), "direction": "above"}))

        # -- loop differential pressure vs its own setpoint --------------------
        dp, dsp = _num(roles, "CW_DP"), _num(roles, "CW_DP_SP")
        if dp is not None and dsp is not None and dsp > 0:
            off = abs(dp - dsp) / dsp * 100.0
            if off > _CW_DP_DEV_PCT:
                out.append(Anomaly(
                    kind="cw-differential-pressure", system_code=sysd["code"],
                    point_id=roles["CW_DP"]["pid"], point_name=roles["CW_DP"]["name"],
                    severity=Severity.HIGH if off > 60 else Severity.MEDIUM,
                    metric="CW_DP",
                    detail=("loop differential pressure {0:.2f} vs setpoint {1:.2f} "
                            "({2:.0f}% off) -- check the transmitter before the pump: a "
                            "loop reading about zero against a live setpoint is usually a "
                            "dead sensor, not a dead pump").format(dp, dsp, off),
                    evidence={"dp": round(dp, 3), "setpoint": round(dsp, 3),
                              "pct_off": round(off, 1)}))

        # -- range: is the loop rejecting heat at all --------------------------
        ret = _num(roles, "CW_RET_TEMP")
        if sup is not None and ret is not None and (ret - sup) < 0:
            out.append(Anomaly(
                kind="cw-range-inverted", system_code=sysd["code"],
                point_id=roles["CW_RET_TEMP"]["pid"],
                point_name=roles["CW_RET_TEMP"]["name"],
                severity=Severity.MEDIUM, metric="CW_RANGE",
                detail=("condenser return {0:.1f} F is COLDER than supply {1:.1f} F "
                        "({2:.1f} F) -- impossible while rejecting heat; suspect a "
                        "swapped or failed sensor").format(ret, sup, ret - sup),
                evidence={"supply_f": round(sup, 2), "return_f": round(ret, 2),
                          "range_f": round(ret - sup, 2)}))

    return out


# Commanded-vs-proved pairs: (command role, feedback role, label, PROVED-RUN role).
# The command is FIRST so a zero/near-zero command can gate the comparison off, and the
# run-status role is LAST because the speed reference alone is not proof that anything is
# turning -- see the CWP-2 note on _CMD_MIN_PCT below. None where the device has no run
# status: a modulating valve is positioned, not started, so its command IS its authority.
_CMD_FBK_PAIRS = (
    ("CT_FAN_SPEED_CMD", "CT_FAN_SPEED", "tower fan", "CT_FAN_STATUS"),
    ("CW_PUMP_SPEED_CMD", "CW_PUMP_SPEED", "condenser pump", "CW_PUMP_STATUS"),
    ("CT_ISO_VALVE_CMD", "CT_ISO_VALVE", "isolation valve", None),
    ("CW_BYPASS_VALVE_CMD", "CW_BYPASS_VALVE", "condenser bypass valve", None),
)
# Binary status can arrive as a number or as the controller's own state text.
_RUN_ON_WORDS = frozenset({"active", "on", "run", "running", "start", "started",
                           "enabled", "true", "1"})
_RUN_OFF_WORDS = frozenset({"inactive", "off", "stop", "stopped", "disabled",
                            "false", "0"})


def _status_on(row: dict) -> "Optional[bool]":
    """Is this status sample proving the equipment RUNNING? None = cannot tell."""
    v = row.get("value_num")
    if v is not None:
        return v > 0.5
    t = (row.get("value_text") or "").strip().lower()
    if t in _RUN_ON_WORDS:
        return True
    if t in _RUN_OFF_WORDS:
        return False
    return None
# Below this the drive is OFF and its feedback floor is not a fault. NOT sufficient on
# its own: CWP-2 sat at a 25% speed REFERENCE for 13 straight days with its status point
# reading 0 the whole time (it is the standby pump and has never once run), so this gate
# passed and the -4% idle 4-20 mA offset was reported as a 100%-disagreement drive fault.
# The reference says what the drive was ASKED for; only the status says it is turning.
_CMD_MIN_PCT = 10.0
_CMD_FBK_FRAC = 0.30     # fraction of commanded samples that must disagree before reporting
# Minimum span the TIME-SERIES water checks are given, however short the caller's window.
_WATER_TS_WINDOW_S = 21600


def detect_command_vs_feedback_water(dal, points, start_utc, end_utc, *, tol=None):
    """Commanded position vs proved position, compared SAMPLE BY SAMPLE.

    Two traps this exists to avoid, both hit on real data from this plant:

    1. WINDOW MEANS LIE. Over 24 h CT-1's fan command averages 52% and its feedback 68%,
       a 16-point gap that reads like a broken drive. Sample by sample the drive is
       perfect: commanded 100 -> proved 99.8.

    2. A DRIVE AT REST IS NOT A DRIVE AT FAULT. With the command at 0 the VFD feedback
       sits at its 33% minimum-speed floor. Comparing those two numbers manufactures a
       33-point "fault" every night on healthy equipment. Only samples where something
       is actually being COMMANDED are compared.
    """
    tol = _CMD_FBK_TOL if tol is None else tol
    by_sys = {}
    for p in points:
        by_sys.setdefault(p.get("system_id"), []).append(p)

    out = []
    for _sid, pts in by_sys.items():
        canon = {p["role"]: p for p in pts if p.get("role") and p.get("role_canonical")}
        for cmd_role, fbk_role, what, run_role in _CMD_FBK_PAIRS:
            cp, fp = canon.get(cmd_role), canon.get(fbk_role)
            if not cp or not fp:
                continue
            # PROVED RUN STATE. A drive at rest is not a drive at fault, and the speed
            # reference cannot tell us which it is.
            runs = {}
            rp = canon.get(run_role) if run_role else None
            if rp:
                for r in dal.query_series(rp["id"], start_utc, end_utc):
                    if r.get("quality") != "good":
                        continue
                    on = _status_on(r)
                    if on is not None:
                        runs[r["ts_utc"]] = on
                if runs and not any(runs.values()):
                    continue        # never ran in this window -- nothing to compare
            crows = {r["ts_utc"]: r["value_num"]
                     for r in dal.query_series(cp["id"], start_utc, end_utc)
                     if r.get("quality") == "good" and r.get("value_num") is not None}
            frows = [r for r in dal.query_series(fp["id"], start_utc, end_utc)
                     if r.get("quality") == "good" and r.get("value_num") is not None]
            if not crows or not frows:
                continue
            ctimes = sorted(crows)
            n_cmd = n_bad = 0
            worst = 0.0
            worst_pair = None
            for r in frows:
                t = r["ts_utc"]
                best = min(ctimes, key=lambda x: abs(x - t))
                if abs(best - t) > 180:
                    continue
                c = crows[best]
                if c < _CMD_MIN_PCT:
                    continue                 # at rest: the feedback floor is not a fault
                if runs:
                    rt = min(runs, key=lambda x: abs(x - t))
                    if abs(rt - t) <= 180 and not runs[rt]:
                        continue             # commanded a speed, but not actually running
                n_cmd += 1
                gap = abs(c - r["value_num"])
                if gap > tol:
                    n_bad += 1
                    if gap > worst:
                        worst, worst_pair = gap, (c, r["value_num"])
            if n_cmd < 30 or not n_bad:
                continue
            frac = n_bad / n_cmd
            if frac < _CMD_FBK_FRAC:
                continue
            code = next((p.get("system_code") for p in pts if p.get("system_code")), "?")
            c_v, f_v = worst_pair
            out.append(Anomaly(
                kind="cw-command-feedback", system_code=code,
                point_id=fp["id"], point_name=fp["name"],
                severity=Severity.HIGH, metric=fbk_role,
                detail=("{0} position disagrees with its command in {1:.0f}% of the samples "
                        "where it was actually being driven ({2} of {3}); worst {4:.0f}% "
                        "commanded vs {5:.0f}% proved -- drive fault, broken linkage, or a "
                        "feedback signal that was never wired back").format(
                            what, frac * 100, n_bad, n_cmd, c_v, f_v),
                evidence={"disagree_frac": round(frac, 3), "samples_commanded": n_cmd,
                          "samples_disagreeing": n_bad,
                          "worst_commanded_pct": round(c_v, 1),
                          "worst_feedback_pct": round(f_v, 1),
                          "tolerance_pct": tol}))
    return out


def _towers_out_of_service(dal, points, start_utc, end_utc):
    """Codes of tower systems that never ran in the window.

    Judged from DATA, not from the operator flag, so it is right even if nobody has
    marked the unit vacant yet: a tower whose fan never rose above its floor AND whose
    isolation valve stayed shut was not available, whatever the configuration says."""
    by_sys = {}
    for p in points:
        by_sys.setdefault(p.get("system_id"), []).append(p)
    out = []
    for _sid, pts in by_sys.items():
        canon = {p["role"]: p for p in pts if p.get("role") and p.get("role_canonical")}
        fan, iso = canon.get("CT_FAN_SPEED"), canon.get("CT_ISO_VALVE")
        if not fan:
            continue
        fmax = max((r["value_num"] for r in dal.query_series(fan["id"], start_utc, end_utc)
                    if r.get("quality") == "good" and r.get("value_num") is not None),
                   default=None)
        if fmax is None or fmax >= 40.0:
            continue                       # it ran at some point -> it was available
        if iso is not None:
            imax = max((r["value_num"] for r in dal.query_series(iso["id"], start_utc, end_utc)
                        if r.get("quality") == "good" and r.get("value_num") is not None),
                       default=None)
            if imax is not None and imax >= 20.0:
                continue                   # valve opened -> it was in the loop
        code = next((p.get("system_code") for p in pts if p.get("system_code")), None)
        if code:
            out.append(code)
    return sorted(out)


def detect_tower_approach(dal, points, start_utc, end_utc, weather,
                          *, min_load_pct=None, hi_f=None, min_hours=2.0):
    """Tower approach, measured ONLY over the hours the tower is actually loaded.

    This deliberately does NOT use the window mean. On this plant the fan runs 100% for
    ~12 daytime hours with an 11-14 F approach and idles at 33% overnight with a 7-8 F
    approach; the 24-hour average of those is ~9.6 F, which is under any sane threshold
    and reports a saturated tower as healthy. The identical mistake produced "eleven AHUs
    exceeded DAT alarm-high overnight" from a window-average airflow gate, and smeared
    the economizer check across weekly windows. A performance number is only meaningful
    over the hours the equipment was being ASKED to perform.

    Requires `min_hours` of loaded operation before reporting, so a brief startup spike
    cannot raise a finding.
    """
    hi = _CT_APPROACH_HI if hi_f is None else hi_f
    min_load = _CT_APPROACH_MIN_LOAD if min_load_pct is None else min_load_pct
    wb = (weather or {}).get("wb_avg")
    if wb is None:
        return []                       # no measured wet-bulb -> approach is undefined

    by_sys = {}
    for p in points:
        by_sys.setdefault(p.get("system_id"), []).append(p)
    # A tower carrying the plant alone saturates and opens its approach by arithmetic,
    # not by fouling. Find the peers that were unavailable so the finding can say so.
    down = _towers_out_of_service(dal, points, start_utc, end_utc)

    out = []
    for sys_id, pts in by_sys.items():
        fan = next((p for p in pts if p.get("role") == "CT_FAN_SPEED"
                    and p.get("role_canonical")), None)
        sup = next((p for p in pts if p.get("role") == "CT_SUP_TEMP"
                    and p.get("role_canonical")), None)
        if not fan or not sup:
            continue
        frows = {r["ts_utc"]: r["value_num"] for r in dal.query_series(fan["id"], start_utc, end_utc)
                 if r.get("quality") == "good" and r.get("value_num") is not None}
        srows = [r for r in dal.query_series(sup["id"], start_utc, end_utc)
                 if r.get("quality") == "good" and r.get("value_num") is not None]
        if not frows or not srows:
            continue
        # pair each supply reading with the nearest fan reading (same 2-min poll cycle)
        ftimes = sorted(frows)
        loaded, apps = 0, []
        for r in srows:
            t = r["ts_utc"]
            # nearest fan sample within one poll interval
            best = min(ftimes, key=lambda x: abs(x - t))
            if abs(best - t) > 180:
                continue
            if frows[best] < min_load:
                continue
            loaded += 1
            apps.append(r["value_num"] - wb)
        if not apps:
            continue
        hours = loaded * (end_utc - start_utc) / max(len(srows), 1) / 3600.0
        if hours < min_hours:
            continue
        mean_ap = sum(apps) / len(apps)
        if mean_ap <= hi:
            continue
        worst = max(apps)
        code = next((p.get("system_code") for p in pts if p.get("system_code")), "?")
        peers_down = [d for d in down if d != code]
        if peers_down:
            # N-1: do not accuse this tower's fill of what its absent partner explains.
            detail = ("tower approach averages {0:.1f} F over {1:.1f} h at fan >= {2:.0f}% "
                      "(peak {3:.1f} F, wet-bulb {4:.1f} F) -- but {5} was OUT OF SERVICE "
                      "for this whole window, so this tower is carrying the full condenser "
                      "load alone. A saturated fan and a wide approach are EXPECTED under "
                      "N-1 and are not evidence of fouling. Re-baseline once {5} is back; "
                      "until then the plant has no spare tower capacity on warm afternoons."
                      ).format(mean_ap, hours, min_load, worst, wb, ", ".join(peers_down))
            sev = Severity.MEDIUM
        else:
            detail = ("tower approach averages {0:.1f} F over {1:.1f} h of loaded operation "
                      "(fan >= {2:.0f}%), peaking at {3:.1f} F, against a {4:.1f} F wet-bulb "
                      "-- a wide approach while the fan is working, with every tower "
                      "available, points at fouled fill, scale or restricted airflow"
                      ).format(mean_ap, hours, min_load, worst, wb)
            sev = Severity.MEDIUM
        out.append(Anomaly(
            kind="tower-approach", system_code=code,
            point_id=sup["id"], point_name=sup["name"],
            severity=sev, metric="CT_APPROACH", detail=detail,
            evidence={"approach_mean_f": round(mean_ap, 2),
                      "approach_max_f": round(worst, 2),
                      "wet_bulb_f": round(wb, 2),
                      "loaded_hours": round(hours, 2),
                      "min_load_pct": min_load,
                      "threshold_f": hi,
                      "peers_out_of_service": peers_down,
                      "n_minus_1": bool(peers_down)}))
    return out


def diagnose(dal: Any, points: Sequence[dict], start_utc: int, end_utc: int, *,
             sched: dict = DEFAULT_OCC, localtime: Callable = time.localtime,
             config: Any = None, preds: Optional[dict] = None) -> list[Anomaly]:
    """Run the role-derived per-system checks + fleet peer-outlier comparison.
    `points` is dal.points_for_analysis(). Economizer sanity uses the window's stored
    weather (measured wet-bulb / enthalpy) as the free-cooling driver.

    `preds` is the {system_id: ts->bool} occupancy map; callers that already resolved it
    (screen / build_context) pass theirs so one analysis run doesn't re-query systems."""
    # each unit is judged against ITS OWN occupied hours (staggered starts / weekend
    # variance / vacant), not one building-wide schedule
    if preds is None:
        from .baseline import _preds_by_system
        preds = _preds_by_system(dal, config, localtime)
    srv = _role_values(dal, points, start_utc, end_utc, sched, localtime, preds=preds)
    weather = dal.weather_window(start_utc, end_utc)
    econ = _econ_cfg(config)
    # The economizer check compares window-AVERAGE damper vs window-AVERAGE enthalpy;
    # over a multi-day window that average smears hourly economizer decisions into a
    # meaningless "unused free cooling" lead on nearly every unit. Only run it on a
    # daily-scale window (<= 2 days); the weekly deep-dive leans on drift/persistence.
    coarse = (end_utc - start_utc) > 2 * 86400
    # operator-entered equipment specs: a cooling-only unit must not be judged as if it
    # had heat (the heating-valve / simultaneous-heat-cool checks would be nonsense)
    equip = dal.equipment_by_system() if hasattr(dal, "equipment_by_system") else {}
    sys_ids = {}
    for p in points:
        sys_ids.setdefault(p.get("system_code"), p.get("system_id"))
    dat_sp_alarm = _dat_sp_alarm(config)
    out: list[Anomaly] = []
    # water side: condenser loop + cooling towers + CW pumps.
    #
    # The two TIME-SERIES checks below need more span than the caller may have asked for.
    # The alert path runs a 30-minute fault screen (alerts.window_s=1800), which at a 120 s
    # poll is 15 samples -- fewer than the 30 this comparison requires and far short of the
    # 2 loaded hours the approach check needs. Both were therefore STRUCTURALLY UNABLE to
    # fire on the alert path, while the report path that would have caught them has never
    # been scheduled. Measured: 30 min -> nothing, 2 h -> approach, 6 h -> both.
    # Floor the window rather than replacing it, so the 24 h/7 d report path is untouched.
    ts_start = min(start_utc, end_utc - _WATER_TS_WINDOW_S)
    # The approach check needs a WET-BULB as well as samples, and weather rows are
    # hour-floored -- a 30-minute window contains none at all, so wb_avg comes back None
    # and the check returns before it ever looks at a sample. Widening only the sample
    # window left it just as silent. `detect_tower_plant` keeps the caller's weather: it
    # is a window-mean check and must stay aligned with the window it was asked about.
    ts_weather = weather if ts_start == start_utc else dal.weather_window(ts_start, end_utc)
    out += detect_tower_plant(srv, weather)
    out += detect_tower_approach(dal, points, ts_start, end_utc, ts_weather)
    out += detect_command_vs_feedback_water(dal, points, ts_start, end_utc)
    for sys in srv.values():
        spec = equip.get(sys_ids.get(sys["code"])) or {}
        out.extend(_diagnose_system(sys["code"], sys["roles"], weather=weather,
                                    econ=econ, coarse_window=coarse, spec=spec,
                                    dat_sp_alarm=dat_sp_alarm))
    out.extend(detect_fleet_outliers(srv))
    # The "it never went into alarm but it clearly wasn't cooling" class — ON by
    # default. Unlike the old window-mean dat-vs-setpoint check this requires the unit
    # to be simultaneously running and calling for cooling for a CONTIGUOUS span, which
    # is what makes it safe to leave on.
    nms = _nms_cfg(config)
    if nms.get("enabled", True):
        out.extend(detect_not_making_setpoint(
            dal, points, start_utc, end_utc, localtime=localtime,
            min_dev_f=nms.get("min_dev_f", 8.0),
            min_minutes=nms.get("min_minutes", 30)))
    # commanded ON but feedback says OFF — a failure that asserts no alarm
    out.extend(detect_command_feedback_mismatch(dal, points, start_utc, end_utc,
                                                localtime=localtime))
    # half a redundant pair idle while the other carries the unit — invisible to any
    # baseline-relative check, because a long-standing fault becomes its own normal
    out.extend(detect_idle_redundant_equipment(dal, points, start_utc, end_utc,
                                               localtime=localtime))
    return out


def _nms_cfg(config: Any) -> dict:
    """analysis.checks.not_making_setpoint — {enabled, min_dev_f, min_minutes}."""
    out = {"enabled": True, "min_dev_f": 8.0, "min_minutes": 30}
    try:
        node = config.get("analysis").get("checks").get("not_making_setpoint") if config else None
        if node:
            d = node.as_dict() if hasattr(node, "as_dict") else dict(node)
            out.update({k: d[k] for k in ("enabled", "min_dev_f", "min_minutes")
                        if k in d})
    except (AttributeError, TypeError, ValueError):
        pass
    return out


def detect_fleet_outliers(srv: dict) -> list[Anomaly]:
    """Peer comparison across the near-identical AHUs: for each comparable role, the
    one unit whose value sits >2.5σ from the fleet is the suspect — no per-unit
    threshold needed. Only RUNNING units are compared (an idle unit is legitimately
    different, not an outlier)."""
    by_role: dict[str, list[tuple]] = {}
    for sysd in srv.values():
        if not sysd.get("running", True) or sysd.get("vacant"):
            continue                        # idle or operator-marked-vacant != peer outlier
        roles = sysd["roles"]
        for role, e in roles.items():
            if role not in _FLEET_ROLES:
                continue
            v = e["mean"]
            # ── compare CONTROL ERROR, not the raw reading ────────────────────────
            # These AHUs are near-identical hardware commissioned to DIFFERENT targets,
            # so the raw value is not comparable across the fleet and the spread is
            # dominated by setpoint differences rather than by how well each unit is
            # controlling. Real example from the live plant: AC-1 holds a 69.0 F
            # discharge setpoint while the rest of the fleet runs 55-61, so AC-1's
            # 68.7 F occupied mean — which is 0.3 F off its OWN target, near-perfect
            # control — was reported as "+8.3 sigma fleet DAT outlier, peer outlier"
            # in every report. The mirror image is worse: a unit genuinely missing its
            # setpoint by 3 F scores near ZERO sigma, because 3 F is small next to the
            # 14 F spread of setpoints. The detector was strictly anti-informative.
            #
            # Subtracting each unit's own target makes "how far from where it is
            # supposed to be" the comparable quantity — the thing a peer comparison is
            # actually for. A unit with no target for its role is SKIPPED rather than
            # compared raw: mixing errors and absolute values in one population is how
            # the OA damper bug worked before it was fixed.
            ref_role = _FLEET_REF.get(role)
            ref = roles.get(ref_role, {}).get("mean") if ref_role else None
            by_role.setdefault(role, []).append(
                (sysd["code"], v, ref, e["pid"], e["name"]))
    out: list[Anomaly] = []
    for role, raw_members in by_role.items():
        # Prefer the control-ERROR population, but never MIX errors and raw values in
        # one comparison — that mixture is precisely how the OA-damper false positive
        # worked. If too few units publish a target for this role, fall back to raw and
        # say so, rather than silently comparing nothing: a detector that quietly stops
        # running is the failure mode this codebase keeps producing.
        with_ref = [(c, v - r, p, n) for c, v, r, p, n in raw_members if r is not None]
        if len(with_ref) >= _FLEET_MIN:
            members, rel = with_ref, True
        else:
            members = [(c, v, p, n) for c, v, _r, p, n in raw_members]
            rel = False
        if len(members) < _FLEET_MIN:
            continue
        for code, v, pid, name in members:
            # leave-one-out: compare against the OTHER units, so a single extreme
            # outlier doesn't inflate the spread enough to mask itself.
            others = [m[1] for m in members if not (m[0] == code and m[2] == pid)]
            if len(others) < _FLEET_MIN - 1:
                continue
            mean = sum(others) / len(others)
            std = math.sqrt(sum((x - mean) ** 2 for x in others) / len(others))
            if std < 1e-6:
                std = 0.5                       # tiny floor for a tight cluster
            z = (v - mean) / std
            # Sigma alone is not evidence of a problem: on a tightly-matched fleet a
            # physically trivial spread still produces a big z. A 0.12 in-wc duct-static
            # difference across units holding the same setpoint is not an actionable
            # "+3.0 sigma outlier" — it is a well-controlled plant. Require the deviation
            # to also clear the role's own meaningful-change floor.
            floor = _FLEET_ABS_MIN.get(role, 0.0)
            if abs(z) >= _FLEET_Z and abs(v - mean) >= floor:
                unit = _FLEET_UNITS.get(role, "")
                what = (f"{role} error {v:+.1f}{unit} vs its own setpoint" if rel
                        else f"{role} {v:.1f}{unit}")
                out.append(Anomaly(
                    kind="fleet-outlier", system_code=code, point_id=pid or 0,
                    point_name=name, severity=Severity.MEDIUM, metric=role,
                    detail=(f"{code}: {what} is {z:+.1f}sigma from the other "
                            f"{len(others)} running AHUs (mean {mean:+.1f}{unit}"
                            f"{' error' if rel else ''}) — peer outlier."),
                    evidence={"role": role, "value": round(v, 2),
                              "peer_mean": round(mean, 2), "z": round(z, 2),
                              "peers": len(others), "vs_own_setpoint": rel,
                              "reference_role": _FLEET_REF.get(role)}))
    return out


def _role_values(dal: Any, points: Sequence[dict], start: int, end: int,
                 sched: dict = DEFAULT_OCC, localtime: Callable = time.localtime,
                 *, preds: Optional[dict] = None) -> dict:
    by_sys: dict = {}
    for p in points:
        by_sys.setdefault(p.get("system_id"), []).append(p)
    result: dict = {}
    for sys_id, pts in by_sys.items():
        sys_sched = (preds or {}).get(sys_id, sched)     # this unit's own hours
        rvals: dict = {}
        for p in pts:
            role = p.get("role")
            if not role or role == "OTHER":
                continue
            is_stage = role in _STAGE_ROLES
            if not is_stage and role not in _TEXT_ROLES and not p.get("role_canonical"):
                continue
            rows = dal.query_series(p["id"], start, end)
            good = [r for r in rows
                    if r.get("quality") == "good" and r.get("value_num") is not None]
            if not good:
                continue
            # Judge control performance over OCCUPIED hours (when the unit is supposed
            # to condition) — a 24h mean blends in night setback and reads as a fleet-
            # wide "missing setpoint" false positive. Fall back to all if no occ samples.
            occ = [r for r in good if is_occupied(r["ts_utc"], sys_sched, localtime)]
            use = occ if occ else good
            mean = sum(r["value_num"] for r in use) / len(use)
            entry = {"mean": mean, "last": good[-1]["value_num"],
                     "text": good[-1].get("value_text"), "name": p["name"],
                     "pid": p["id"], "code": p.get("system_code")}
            if is_stage:                       # keep the most-active stage point
                cur = rvals.get(role)
                if cur is None or mean > cur["mean"]:
                    rvals[role] = entry
            else:
                rvals[role] = entry
        fs, ft = rvals.get("FAN_SPEED"), rvals.get("FAN_STATUS")
        running = (fs["mean"] > 10.0 if fs else
                   (ft["mean"] > 0.5 if ft else True))    # unknown -> don't suppress
        vacant = bool(pts and pts[0].get("system_occupancy") == "vacant")
        result[sys_id] = {"code": (pts[0].get("system_code") if pts else "?"),
                          "running": running, "vacant": vacant, "roles": rvals}
    return result


def detect_idle_redundant_equipment(dal: Any, points: Sequence[dict], start: int,
                                    end: int, *,
                                    localtime: Callable = time.localtime) -> list[Anomaly]:
    """One half of a redundant pair doing nothing while the other carries the unit.

    AC-2's SF~-A VFD sat at 0.0% for SEVEN STRAIGHT DAYS while SF~-B modulated normally
    (0-100%, avg 54%). The unit has been running on a single fan and nothing ever said
    so. No existing detector could see it:
      · stuck/flatline deliberately excludes actuators — a VFD legitimately rests.
      · z-score deviation sees nothing, because the fan's own learned baseline is ALSO
        zero: it has been off for the whole baseline window, so being off is "normal".
    A baseline learns whatever has been happening, which means a fault that predates the
    baseline window becomes invisible precisely BECAUSE it has lasted a long time. That
    is the worst possible failure mode for a long-running fault, and the only way out is
    to compare the pair against EACH OTHER rather than against history.

    Reported as MEDIUM, not HIGH: lead/lag rotation is a legitimate design, so this is
    "confirm this is intentional", not "something is broken".
    """
    found: list[Anomaly] = []
    by_sys: dict = {}
    for p in points:
        if p.get("role") in ("FAN_SPEED", "EXH_FAN_SPEED") and p.get("system_id"):
            by_sys.setdefault(p["system_id"], []).append(p)

    for sid, fans in by_sys.items():
        if len(fans) < 2:
            continue
        stats = []
        for f in fans:
            vals = [r["value_num"] for r in dal.query_series(f["id"], start, end)
                    if r.get("quality") == "good" and r.get("value_num") is not None]
            if not vals:
                continue
            stats.append((f, max(vals), sum(vals) / len(vals)))
        if len(stats) < 2:
            continue
        idle = [s for s in stats if s[1] <= 1.0]          # never left zero
        active = [s for s in stats if s[2] > 10.0]        # genuinely carrying load
        if not idle or not active:
            continue
        for f, _mx, _mean in idle:
            code = f.get("system_code") or "?"
            partner = max(active, key=lambda s: s[2])
            hrs = (end - start) / 3600.0
            found.append(Anomaly(
                kind="idle-redundant", system_code=code, point_id=f["id"],
                point_name=f.get("name") or "fan", severity=Severity.MEDIUM,
                metric=f.get("role") or "FAN_SPEED",
                detail=(f"{code}: {_short(f.get('name'))} never left 0% across the whole "
                        f"{hrs:.0f} h window while {_short(partner[0].get('name'))} "
                        f"carried the unit (avg {partner[2]:.0f}%, peak "
                        f"{partner[1]:.0f}%). The unit is running on one fan. Confirm "
                        f"this is intended lead/lag or a planned outage — if not, that "
                        f"fan is not starting. NOTE: this cannot show up as a deviation, "
                        f"because a fan that has been off for the whole baseline window "
                        f"has learned 'off' as its own normal."),
                evidence={"idle_point": f.get("name"), "hours": round(hrs, 1),
                          "partner": partner[0].get("name"),
                          "partner_avg": round(partner[2], 1)}))
    return found


def _short(name: Optional[str]) -> str:
    return (name or "?").split("|", 1)[0].strip()


def detect_command_feedback_mismatch(dal: Any, points: Sequence[dict], start: int,
                                     end: int, *, min_minutes: int = 20,
                                     localtime: Callable = time.localtime) -> list[Anomaly]:
    """Commanded ON, feedback says OFF — for a sustained period.

    The most general fault signature in a BAS, and it needs no alarm to fire: the
    controller asked for something and the equipment did not do it. This plant trends
    both halves for every supply fan (`SF~-A Enable` -> `SF~-A Run Status`) and both
    were invisible — Enable was classified INTERNAL and nothing compared them.

    The operator's point: "just because the Multistack wasn't in alarm doesn't mean
    there isn't an issue — one of the trended points is whether they are running or not.
    This should apply to ALL points trended." A failed fan or a tripped compressor
    frequently reports a perfectly healthy alarm point while simply not running, so
    watching alarms alone is not enough. Comparing the command against its own feedback
    catches it directly.

    Requires a contiguous span (default 20 min) so normal start-up lag — a VFD spinning
    up, a proof switch settling — is not reported. Measured on 7 days of live data this
    fires once, on a 15-minute AC-2 SF-B start-up lag, which is correctly below the
    threshold; it is a quiet check that speaks when something is genuinely stuck.
    """
    found: list[Anomaly] = []
    by_sys: dict = {}
    for p in points:
        if p.get("system_id") is None or not p.get("enabled", 1):
            continue
        by_sys.setdefault(p["system_id"], []).append(p)

    for sid, pts in by_sys.items():
        cmds = [p for p in pts if p.get("role") == "FAN_CMD"]
        fbs = [p for p in pts if p.get("role") == "FAN_STATUS"]
        for cmd in cmds:
            # pair by the shared designator: "SF~-A Enable" <-> "SF~-A Run Status"
            key = _designator(cmd.get("name") or "")
            fb = next((f for f in fbs if _designator(f.get("name") or "") == key), None)
            if not fb or not key:
                continue

            def series(pt):
                return {r["ts_utc"]: r["value_num"]
                        for r in dal.query_series(pt["id"], start, end)
                        if r.get("quality") == "good" and r.get("value_num") is not None}

            C, R = series(cmd), series(fb)
            run: list[int] = []
            worst: list[int] = []
            for t in sorted(t for t in C if t in R):
                if C[t] > 0.5 and R[t] <= 0.5:
                    run.append(t)
                else:
                    if len(run) > len(worst):
                        worst = run
                    run = []
            if len(run) > len(worst):
                worst = run
            if not worst:
                continue
            mins = (worst[-1] - worst[0]) / 60.0
            if mins < min_minutes:
                continue
            code = cmd.get("system_code") or "?"
            found.append(Anomaly(
                kind="command-feedback", system_code=code, point_id=fb["id"],
                point_name=fb.get("name") or key, severity=Severity.HIGH,
                metric="FAN_STATUS",
                detail=(f"{code}: {key} was COMMANDED ON but its run-status feedback "
                        f"stayed OFF for {mins:.0f} min "
                        f"({time.strftime('%a %H:%M', localtime(worst[0]))}-"
                        f"{time.strftime('%a %H:%M', localtime(worst[-1]))} local). "
                        f"The controller asked and the equipment did not answer — check "
                        f"starter/VFD, overload trip, belt, and whether it needs a "
                        f"manual reset. No alarm point has to assert for this to be a "
                        f"real failure."),
                evidence={"designator": key, "minutes": round(mins),
                          "start_local": time.strftime("%Y-%m-%d %H:%M", localtime(worst[0])),
                          "end_local": time.strftime("%Y-%m-%d %H:%M", localtime(worst[-1]))}))
    return found


def _designator(name: str) -> str:
    """'SF~-A Enable|dac-3_BV~_001' -> 'SF-A'. Pairs a command with its own feedback
    rather than with the other fan's."""
    base = (name or "").split("|", 1)[0]
    m = re.search(r"\b(SF|EF|RF)\s*[~]?\s*-?\s*([AB])\b", base, re.I)
    if m:
        return f"{m.group(1).upper()}-{m.group(2).upper()}"
    m = re.search(r"\b(exhaust fan|supply fan)\b", base, re.I)
    return m.group(1).title() if m else ""


def detect_not_making_setpoint(dal: Any, points: Sequence[dict], start: int, end: int,
                               *, min_dev_f: float = 8.0, min_minutes: int = 30,
                               localtime: Callable = time.localtime) -> list[Anomaly]:
    """A unit that is RUNNING and CALLING FOR COOLING but sitting well above its own
    discharge setpoint, for a sustained period. The most operationally important fault
    in this plant, and until now completely undetected.

    THE OPERATOR'S OWN WORDS: "if it wasn't running when the discharge setpoint was say
    55 and the discharge temp was say 75, then there is still an issue, EVEN WITHOUT IT
    GOING INTO ALARM. I had a few units do that and I had to manually power cycle them."
    A failed / locked-out / tripped compressor does not necessarily assert an alarm
    point — so alarm-watching alone misses it, and a mean-based comparison misses it too
    because a few bad hours average away against a good day.

    Measured over 7 days of real data before this existed: AC-12 spent 90 five-minute
    buckets running + calling for cooling at up to 25.2 F above a 55 F setpoint, AC-6
    92 buckets at 24.9 F, with AC-4/9/10 all past 20 F. None of it was reported.

    Why this is a TIME-SERIES check, not the old window-mean one: the pre-existing
    dat-vs-setpoint check compared window AVERAGES and was gated off
    (`analysis.checks.dat_vs_setpoint`) because DAT legitimately sits above setpoint
    whenever a unit is venting, economizing or heating — averaged over 24 h that is
    indistinguishable from a real failure. Requiring the unit to be simultaneously
    RUNNING and CALLING FOR COOLING, for a CONTIGUOUS span, removes every one of those
    benign cases, which is what makes this safe to have on by default.
    """
    found: list[Anomaly] = []
    canon: dict = {}
    for p in points:
        if p.get("role_canonical") and p.get("system_id") is not None:
            canon[(p["system_id"], p.get("role"))] = p

    sids = {sid for sid, _r in canon}
    for sid in sids:
        dat = canon.get((sid, "DAT"))
        sp = canon.get((sid, "DAT_ACTIVE_SP"))
        fan = canon.get((sid, "FAN_SPEED")) or canon.get((sid, "FAN_STATUS"))
        if not (dat and sp and fan):
            continue
        cool = canon.get((sid, "COOLING_STAGES"))
        nocool = canon.get((sid, "MECH_COOL_DISABLE"))

        def series(pt):
            return {r["ts_utc"]: r["value_num"] for r in dal.query_series(pt["id"], start, end)
                    if r.get("quality") == "good" and r.get("value_num") is not None}

        D, S, F = series(dat), series(sp), series(fan)
        C = series(cool) if cool else {}
        N = series(nocool) if nocool else {}
        fan_on = 10.0 if fan.get("role") == "FAN_SPEED" else 0.5

        # contiguous run of "running + cooling + above setpoint"
        ts = sorted(t for t in D if t in S and t in F)
        run: list[tuple] = []
        worst: Optional[tuple] = None
        for t in ts:
            dev = D[t] - S[t]
            cooling = (C.get(t, 1.0) > 0.5) and (N.get(t, 0.0) <= 0.5)
            if F[t] > fan_on and cooling and dev > min_dev_f:
                run.append((t, dev, D[t], S[t]))
            else:
                if run and (worst is None or len(run) > len(worst)):
                    worst = run
                run = []
        if run and (worst is None or len(run) > len(worst)):
            worst = run
        if not worst:
            continue
        mins = (worst[-1][0] - worst[0][0]) / 60.0
        if mins < min_minutes:
            continue

        peak = max(worst, key=lambda x: x[1])
        code = dat.get("system_code") or "?"
        sev = Severity.HIGH if (mins >= 60 or peak[1] >= 15.0) else Severity.MEDIUM
        found.append(Anomaly(
            kind="not-making-setpoint", system_code=code, point_id=dat["id"],
            point_name=dat.get("name") or "DAT", severity=sev, metric="DAT",
            detail=(f"{code}: supply fan running and calling for cooling, but discharge "
                    f"air stayed above its own setpoint for {mins:.0f} min "
                    f"({time.strftime('%a %H:%M', localtime(worst[0][0]))}-"
                    f"{time.strftime('%a %H:%M', localtime(worst[-1][0]))} local); "
                    f"worst {peak[2]:.1f}F vs setpoint {peak[3]:.1f}F "
                    f"(+{peak[1]:.1f}F) at "
                    f"{time.strftime('%a %H:%M', localtime(peak[0]))}. "
                    f"The unit is not delivering cooling it is asking for — check "
                    f"compressor lockout/trip, refrigerant charge, condenser, and "
                    f"whether it needs a manual reset. A compressor can fail this way "
                    f"WITHOUT asserting an alarm point."),
            evidence={"minutes": round(mins), "peak_dat": round(peak[2], 1),
                      "setpoint": round(peak[3], 1), "peak_dev_f": round(peak[1], 1),
                      "start_local": time.strftime("%Y-%m-%d %H:%M", localtime(worst[0][0])),
                      "end_local": time.strftime("%Y-%m-%d %H:%M", localtime(worst[-1][0])),
                      "samples": len(worst)}))
    return found


def running_by_system(dal: Any, pts: list[dict], start: int, end: int) -> dict:
    """{system_id: bool} — is each unit actually moving air over the window?

    Shared by the out-of-range detector and anything else that must not judge idle
    equipment against running thresholds. An air-side limit (discharge/mixed/return)
    only means something when there is airflow: a stopped unit's duct sits at room
    temperature, and AC-9 — fan 0% for 24 h, unoccupied — reads 77 F against its own
    70 F discharge high limit. That is still air, not an over-temperature fault.

    Deliberately returns nothing for a unit whose fan state is unknown, so the caller
    treats it as "can't tell" and does NOT suppress. Cheap: touches only the canonical
    fan points (~14-28 series), not every point.
    """
    out: dict[Any, bool] = {}
    for p in pts:
        if not p.get("role_canonical") or p.get("role") not in ("FAN_SPEED", "FAN_STATUS"):
            continue
        sid = p.get("system_id")
        if out.get(sid):                 # a FAN_SPEED already proved it running
            continue
        rows = dal.query_series(p["id"], start, end)
        vals = [r["value_num"] for r in rows
                if r.get("quality") == "good" and r.get("value_num") is not None]
        if not vals:
            continue
        mean = sum(vals) / len(vals)
        thresh = 10.0 if p.get("role") == "FAN_SPEED" else 0.5
        out[sid] = bool(out.get(sid)) or mean > thresh
    return out


def running_windows(dal: Any, pts: list[dict], start: int, end: int) -> dict:
    """{system_id: [(t0, t1), ...]} — the spans during which each unit was moving air.

    A WINDOW-WIDE running flag is not good enough for any check that fires on an
    instantaneous value. `running_by_system` averages the fan across the whole window,
    so a unit that runs 05:00-18:00 and shuts off overnight reads as "running" for all
    24 h — and the out-of-range check, which uses the window MAX, then charged every
    unit with a limit breach for still air sitting at room temperature overnight. That
    produced "Eleven AHUs exceeded their discharge-air-temp alarm-high limits overnight"
    on a night when the fans were at 0.2-0.4%.

    Ranges are compact (a couple of spans per day), so filtering samples against them is
    cheap and lets a check ask "was air moving AT THIS MOMENT" instead of "on average".
    """
    out: dict = {}
    for p in pts:
        if not p.get("role_canonical") or p.get("role") not in ("FAN_SPEED", "FAN_STATUS"):
            continue
        sid = p.get("system_id")
        if sid in out and p.get("role") != "FAN_SPEED":
            continue                      # prefer the modulating signal when present
        thresh = 10.0 if p.get("role") == "FAN_SPEED" else 0.5
        spans: list[list[int]] = []
        for r in dal.query_series(p["id"], start, end):
            if r.get("quality") != "good" or r.get("value_num") is None:
                continue
            on = r["value_num"] > thresh
            t = r["ts_utc"]
            if on:
                # extend the current span across normal poll spacing, else start a new one
                if spans and t - spans[-1][1] <= 900:
                    spans[-1][1] = t
                else:
                    spans.append([t, t])
        if spans:
            out[sid] = [(a, b) for a, b in spans]
    return out


def in_windows(ts: int, spans: Optional[list]) -> bool:
    """Was the unit running at this instant? None = unknown, which never suppresses."""
    if spans is None:
        return True
    return any(a <= ts <= b for a, b in spans)


def _dat_sp_alarm(config: Any) -> bool:
    """analysis.checks.dat_vs_setpoint — whether to ALARM on discharge temp off its
    setpoint. Default OFF: the discharge setpoint is now paired with DAT for trending
    (viewer / AI context / correlation), but DAT sits above the cooling setpoint whenever
    a unit is venting / economizing / heating, so a raw deviation alarm would cry wolf
    until it is made cooling-mode-aware. Operator turns it on when ready."""
    try:
        node = config.get("analysis").get("checks") if config else None
        if node:
            d = node.as_dict() if hasattr(node, "as_dict") else dict(node)
            return bool(d.get("dat_vs_setpoint", False))
    except (AttributeError, TypeError):
        pass
    return False


def _diagnose_system(code: str, rv: dict, *, weather: Optional[dict] = None,
                     econ: Optional[dict] = None,
                     coarse_window: bool = False,
                     spec: Optional[dict] = None,
                     dat_sp_alarm: bool = False) -> list[Anomaly]:
    out: list[Anomaly] = []
    econ = econ or {"enth": _ECON_ENTH_CHANGEOVER, "wb": _ECON_WB_CHANGEOVER}
    # operator-declared equipment: 'none' heating means the heat-related checks don't apply
    _ht = str(((spec or {}).get("heating_type") or {}).get("value") or "").strip().lower()
    has_heat = _ht not in ("none", "no", "cooling only", "cooling-only")

    def add(kind, sev, metric, detail, pid, **ev):
        out.append(Anomaly(kind=kind, system_code=code or "?", point_id=pid or 0,
                           point_name=rv.get(metric, {}).get("name", metric),
                           severity=sev, metric=metric, detail=detail, evidence=ev))

    dat, dat_sp = _num(rv, "DAT"), _num(rv, "DAT_ACTIVE_SP")
    dsp, dsp_sp = _num(rv, "DUCT_PRESS"), _num(rv, "DUCT_PRESS_SP")
    mat, rat, oat = _num(rv, "MAT"), _num(rv, "RAT"), _num(rv, "OAT")
    damper = _num(rv, "OA_DAMPER")
    min_oad = _num(rv, "OA_DAMPER_MIN_SP")   # operator-set minimum OA position
    # The unit's OWN economizer changeover, trended per unit (this plant: 19 Btu/lb and
    # 60 F, NOT the ASHRAE 28/66 defaults the code used to assume), plus the
    # controller's own "is free cooling available right now" verdict.
    econ_sp = _num(rv, "ECON_ENTH_SP")
    econ_db = _num(rv, "ECON_DB_SP")
    econ_ok = _bool_state(rv, "ECON_OK")
    htg = _num(rv, "HTG_VALVE")
    cooling = _num(rv, "COOLING_STAGES")
    fan_speed, fan_status = _num(rv, "FAN_SPEED"), _num(rv, "FAN_STATUS")

    # An idle unit isn't a control fault — don't flag "not making setpoint" on a fan
    # that's off (the vacant-floor units run this way). Gate the control-loop checks.
    if fan_speed is not None:
        running = fan_speed > 10.0
    elif fan_status is not None:
        running = fan_status > 0.5
    else:
        running = True                      # can't tell -> don't suppress

    # 1) discharge-air not tracking its active setpoint. OFF by default (the setpoint is
    # paired for TRENDING, but DAT is legitimately above the cooling setpoint when the
    # unit isn't actively cooling — this alarm needs cooling-mode awareness before it is
    # trustworthy). Enable via analysis.checks.dat_vs_setpoint.
    if dat_sp_alarm and running and dat is not None and dat_sp is not None:
        dev = dat - dat_sp
        if abs(dev) > _DAT_DEV:
            add("dat-deviation", Severity.MEDIUM, "DAT",
                f"{code}: discharge air {dat:.1f} is {dev:+.1f}F off its active setpoint "
                f"{dat_sp:.1f} — coil/valve or airflow issue.",
                rv.get("DAT", {}).get("pid"), dat=round(dat, 1), setpoint=round(dat_sp, 1),
                deviation=round(dev, 1))

    # 2) duct static not holding setpoint
    if running and dsp is not None and dsp_sp is not None and abs(dsp - dsp_sp) > _DSP_ERR:
        add("duct-static", Severity.MEDIUM, "DUCT_PRESS",
            f"{code}: duct static {dsp:.2f} vs setpoint {dsp_sp:.2f}"
            f" ({dsp - dsp_sp:+.2f} in wc) — fan/VFD or damper control issue.",
            rv.get("DUCT_PRESS", {}).get("pid"), value=round(dsp, 2), setpoint=round(dsp_sp, 2))

    # 3) simultaneous heating + cooling (energy waste)
    if has_heat and running and htg is not None and cooling is not None \
            and htg > _HTG_ON and cooling > 0.1:
        add("sim-heat-cool", Severity.HIGH, "HTG_VALVE",
            f"{code}: heating valve {htg:.0f}% open WHILE cooling is active "
            f"(stage {cooling:.1f}) — simultaneous heat/cool, wasted energy.",
            rv.get("HTG_VALVE", {}).get("pid"), htg_pct=round(htg, 1), cooling=round(cooling, 1))

    # 4) mixed-air plausibility: MAT should blend OAT & RAT by damper position
    #    (only meaningful with airflow across the mixing box)
    #
    # The outdoor temperature may come from the unit's OWN sensor or from site weather.
    # This check was DEAD for the entire life of the program because it required a
    # per-unit OAT role: this plant trends 39 OAT points but every one of them is
    # enabled=0, so `oat` was always None and the check never executed on any of the 14
    # units — silently, with no log. It is the check whose own message names "stuck
    # damper or bad MAT sensor", i.e. exactly the AC-1 pegged-MAT fault we already
    # know about. The measured site OAT was already being passed into this function as
    # weather['oat_avg'] and used by the economizer check three lines below. Found in
    # review #3.
    oa_src = "unit OAT"
    oa = oat
    if oa is None:
        oa = (weather or {}).get("oat_avg")
        oa_src = "site weather"
    if running and None not in (mat, oa, rat, damper):
        frac = max(0.0, min(1.0, damper / 100.0))
        expected = frac * oa + (1 - frac) * rat
        resid = mat - expected
        if abs(resid) > _MIXED_RESID:
            add("mixed-air", Severity.MEDIUM, "MAT",
                f"{code}: mixed air {mat:.1f} is {resid:+.1f}F off the OA/RA blend "
                f"({expected:.1f} at {frac*100:.0f}% OA, OA temp {oa:.1f} from {oa_src}) "
                f"— stuck damper or bad MAT sensor.",
                rv.get("MAT", {}).get("pid"), mat=round(mat, 1), expected=round(expected, 1),
                residual=round(resid, 1), oa_pct=round(frac * 100, 0),
                oat=round(oa, 1), oat_source=oa_src)

    # 5) economizer sanity vs outdoor conditions — enthalpy/wet-bulb driven (the real
    #    free-cooling decision), from the window's measured weather; dry-bulb OAT sensor
    #    is only the last-resort fallback when no weather is stored. Skipped on coarse
    #    (multi-day) windows where the averaged comparison is not meaningful.
    if running and damper is not None and not coarse_window:
        frac = damper / 100.0
        # "should be at minimum" means the unit's OWN minimum. A damper sitting at a
        # min the operator raised to 35% is AT minimum, not pulling excess OA — only
        # flag when it's meaningfully open BEYOND that minimum.
        min_frac = (min_oad / 100.0) if min_oad is not None else _DAMPER_WIDE
        # ...and "the changeover" means the unit's OWN commissioned changeover, which it
        # trends. Judging against the ASHRAE-textbook default while this plant is
        # commissioned at 19 Btu/lb produced a REPORT THAT WAS BACKWARDS: at OA enthalpy
        # 27.8 it said "just under the 28 Btu/lb changeover — missed free cooling", when
        # 27.8 is far ABOVE the real 19 lockout and the dampers at 19-20% were correct.
        # It called correct economizer behaviour a fleet-wide fault in every report.
        econ_u = dict(econ)
        if econ_sp is not None:
            econ_u["enth"] = econ_sp
        if econ_db is not None:
            econ_u["oat"] = econ_db
        favorable, driver = _econ_favorable(weather, oat, econ_u)
        # The controller publishes its OWN answer to this exact question. If it says
        # free cooling is not available, it is not a missed opportunity — it is the
        # sequence working. Only override toward "not favorable"; a controller that
        # says OK while conditions are bad is itself worth flagging (above).
        if econ_ok is False and favorable is True:
            favorable, driver = False, f"{driver} (controller reports not OK to economize)"
        if favorable is False and frac > min_frac + 0.10:
            add("economizer", Severity.MEDIUM, "OA_DAMPER",
                f"{code}: OA damper {frac*100:.0f}% open (min {min_frac*100:.0f}%) while "
                f"{driver} is above the changeover — pulling in warm/humid outdoor air; "
                f"economizer should be locked out to minimum.",
                rv.get("OA_DAMPER", {}).get("pid"), damper_pct=round(frac * 100, 0),
                driver=driver)
        elif favorable is True and frac < _DAMPER_MIN and (cooling or 0) > 0.1:
            add("economizer", Severity.MEDIUM, "OA_DAMPER",
                f"{code}: mechanical cooling active while {driver} is below the changeover "
                f"but OA damper only {frac*100:.0f}% — free cooling available and unused.",
                rv.get("OA_DAMPER", {}).get("pid"), damper_pct=round(frac * 100, 0),
                driver=driver)

    # 6) mode inconsistency (decoded state-text vs actuator activity)
    mode = _text(rv, "MODE")
    if mode:
        if "heat" in mode and cooling is not None and cooling > 0.1 and "cool" not in mode:
            add("mode", Severity.MEDIUM, "MODE",
                f"{code}: unit mode reports '{rv['MODE']['text']}' but cooling stages are "
                f"active — mode/actuator mismatch.", rv.get("MODE", {}).get("pid"), mode=mode)
        if has_heat and "cool" in mode and htg is not None and htg > _HTG_ON and "heat" not in mode:
            add("mode", Severity.MEDIUM, "MODE",
                f"{code}: unit mode reports '{rv['MODE']['text']}' but the heating valve is "
                f"{htg:.0f}% open — mode/actuator mismatch.", rv.get("MODE", {}).get("pid"),
                mode=mode, htg_pct=round(htg, 1))

    # 7) BAS-native comm loss (decoded Communication Status). Match bad states
    # explicitly — "not communicating" contains the substring "communicating".
    comm = _text(rv, "COMM")
    if comm and any(b in comm for b in ("not communicating", "not comm",
                                        "unresolved", "no comm", "offline")):
        add("comm-loss", Severity.HIGH, "COMM",
            f"{code}: controller Communication Status = '{rv['COMM']['text']}'.",
            rv.get("COMM", {}).get("pid"), status=comm)

    return out
