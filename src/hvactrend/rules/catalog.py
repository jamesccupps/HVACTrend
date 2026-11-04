"""The alarm rules and their default thresholds.

Unit rules share their ids with coverage.RULES, so the coverage review and the alarms never
disagree about what a rule reads (a test pins it). Thresholds are SETTINGS (the operator,
2026-09-29): `rules:` in config.yaml holds a building-wide value per rule and `overrides` per
unit; what is not set falls back to DEFAULTS below. The defaults are the operator's own examples
(discharge air 5 °F for 30 min -> ACTION, 10 °F for 10 min -> CRITICAL) and the programs' own
numbers (P11 calls a boiler with under 5 °F of rise for 50 minutes a low-differential).

Severity means something: CRITICAL is pushed at once as urgent, ACTION as a normal
notification (both since 2026-10-02, the operator), and both are listed in the daily check-in; WATCH
is informational.
"""

from __future__ import annotations

import copy
import math
import re
from typing import Any, Optional

from .engine import Tier

DEFAULTS: dict = {
    "sat-setpoint": {
        "tiers": {"action": {"delta": 5.0, "minutes": 30},
                  "critical": {"delta": 10.0, "minutes": 10}},
        "settle_min": 60, "clear_min": 30, "clear_ratio": 0.5},
    "static-setpoint": {
        "tiers": {"action": {"delta": 0.25, "minutes": 15}},
        "settle_min": 45, "clear_min": 30, "clear_ratio": 0.5},
    "boiler-rise": {
        "tiers": {"action": {"min_rise": 5.0, "minutes": 50}},
        "settle_min": 10, "clear_min": 20, "clear_margin": 2.0},
    "sensor-sanity": {
        "tiers": {"action": {"minutes": 10}},
        "clear_min": 30},
    "bas-alarm": {
        "tiers": {"action": {"minutes": 15}},
        "critical_minutes": 2, "clear_min": 5},
}

# Why the settles are what they are (measured 2026-09-30 over 30 days of occupied starts on
# AC-1..13 and HV1): discharge air came within 5 °F of setpoint in 4 min at the median, 16 at
# p90, 32 at p95, and a few mornings took 35-50; duct static in 4 / 18 / 34 min, Mondays up
# to ~70. A 60-min discharge settle is also what the old AI layer measured. BAS alarms need
# 15 min because HV1's own low-static alarm trips for 12 min at every 05:09 start and several
# units pulse "Man Reset Required" for 4 min at 09:00; freeze, smoke and flame still count
# after 2.

TITLES = {
    "sat-setpoint": "Discharge air not meeting its setpoint",
    "static-setpoint": "Duct static not meeting its setpoint",
    "boiler-rise": "Boiler enabled but not heating (no temperature rise)",
    "sensor-sanity": "Sensor reading impossible values",
    "bas-alarm": "Controller alarm",
}

UNIT_RULES = ("sat-setpoint", "static-setpoint", "boiler-rise")
POINT_RULES = ("sensor-sanity", "bas-alarm")

# Physically possible readings, °F. Outside these a sensor has failed, whatever its status
# flags say: on 2026-09-29/30 boiler 3's return read -60 °F with 546 of 576 bad samples
# flagged "good", and in September AC-1's mixed air read 186 °F flagged "good".
SANE_F = {
    **dict.fromkeys(("DAT", "MAT", "RAT", "OAT", "ROOM_TEMP", "MECH_TEMP"), (-40.0, 150.0)),
    **dict.fromkeys(("HW_SUP_TEMP", "HW_RET_TEMP", "BLR_SUP_TEMP", "BLR_RET_TEMP",
                     "DHW_TEMP"), (32.0, 250.0)),
    **dict.fromkeys(("CW_SUP_TEMP", "CW_RET_TEMP", "CT_SUP_TEMP", "CW_PUMP_TEMP"),
                    (20.0, 130.0)),
}

# The controllers' own alarm points. Only binary objects: an ALARM-role analog is a failure
# COUNTER, not an alarm state.
ALARM_ROLES = {"ALARM", "RESET_REQUIRED", "CT_FAN_ALARM", "CW_PUMP_ALARM", "CT_LEVEL_ALARM",
               "CW_PUMP_RUNTIME", "BLR_ALARM", "HW_PUMP_ALARM"}
_CRITICAL_ALARM = re.compile(r"freez|smoke|flame|\bfire\b|\bff\b", re.I)


def settings(rule: str, unit: Optional[str], cfg: Any) -> dict:
    """DEFAULTS[rule], then the config's value for the rule, then the unit's override.
    A tier set to null in the config is switched off."""
    cfg = cfg.as_dict() if hasattr(cfg, "as_dict") else (cfg or {})
    out = copy.deepcopy(DEFAULTS[rule])
    layers = [cfg.get(rule) or {}]
    if unit:
        layers.append(((cfg.get("overrides") or {}).get(unit) or {}).get(rule) or {})
    for layer in layers:
        for k, v in layer.items():
            if k == "tiers":
                for tname, tv in (v or {}).items():
                    if tv is None:
                        out["tiers"].pop(tname, None)
                    else:
                        out["tiers"].setdefault(tname, {}).update(tv)
            else:
                out[k] = v
    return out


def tiers_for(rule: str, s: dict, *, sane: Optional[tuple] = None,
              critical: bool = False) -> list:
    """Engine tiers (with breach tests) from a rule's settings."""
    out = []
    for name, t in s["tiers"].items():
        mins = float(t["minutes"])
        if rule in ("sat-setpoint", "static-setpoint"):
            d = float(t["delta"])
            unit = "°F" if rule == "sat-setpoint" else "in. w.c."
            out.append(Tier(name, mins, (lambda v, d=d: v >= d),
                            f"{d:g} {unit} off setpoint for {mins:g} min"))
        elif rule == "boiler-rise":
            r = float(t["min_rise"])
            out.append(Tier(name, mins, (lambda v, r=r: v < r),
                            f"under {r:g} °F of rise for {mins:g} min"))
        elif rule == "sensor-sanity":
            lo, hi = sane
            out.append(Tier(name, mins, (lambda v, lo=lo, hi=hi: math.isnan(v) or not lo <= v <= hi),
                            f"outside {lo:g}-{hi:g} °F for {mins:g} min"))
        elif rule == "bas-alarm":
            tier = "critical" if critical else name
            if critical:
                mins = float(s.get("critical_minutes", mins))
            out.append(Tier(tier, mins, (lambda v: v >= 0.5), f"on for {mins:g} min"))
    return out


def clear_for(rule: str, s: dict, *, sane: Optional[tuple] = None):
    """The hysteresis test: clearly back, not merely under the threshold."""
    if rule in ("sat-setpoint", "static-setpoint"):
        lowest = min(float(t["delta"]) for t in s["tiers"].values())
        c = lowest * float(s.get("clear_ratio", 0.5))
        return lambda v: v < c
    if rule == "boiler-rise":
        need = max(float(t["min_rise"]) for t in s["tiers"].values()) + float(s.get("clear_margin", 2))
        return lambda v: v >= need
    if rule == "sensor-sanity":
        lo, hi = sane
        return lambda v: (not math.isnan(v)) and lo <= v <= hi
    return lambda v: v < 0.5                                      # bas-alarm


def worse_for(rule: str, sane: Optional[tuple] = None):
    """Which of two values is worse, for the episode's peak."""
    if rule == "boiler-rise":
        return lambda a, b: a < b                                 # less rise is worse
    if rule == "sensor-sanity":
        lo, hi = sane

        def off(v):
            return math.inf if math.isnan(v) else max(lo - v, v - hi, 0.0)
        return lambda a, b: off(a) > off(b)
    return lambda a, b: a > b


def alarm_is_critical(name: str) -> bool:
    return bool(_CRITICAL_ALARM.search(name or ""))


def name(r: dict) -> str:
    """An incident in a few words, for the phone and the check-in. A controller's own alarm
    is named by its point ("Multistack Alarm") and an impossible reading by its sensor; the
    other rules by their title. On 2026-10-26 AC-2's and AC-8's Multistack alarms had been on
    for 12 days, and everywhere they were listed they were "controller alarm"."""
    label = str(r.get("point_label") or "").strip()
    if label and r.get("rule") == "bas-alarm":
        return label
    if label and r.get("rule") == "sensor-sanity":
        return f"{label} reading impossible values"
    return str(r.get("title") or TITLES.get(r.get("rule"), r.get("rule") or "incident"))
