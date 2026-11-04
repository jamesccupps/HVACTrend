"""Semantic point-role classifier (ported from the old AHU program's domain model).

The keystone for physics-aware analysis: every point gets tagged with what it IS
(discharge-air temp, mixed-air temp, heating valve, cooling stage, ...) so the
pre-screen and context builder can apply relationships ("DAT should track its active
setpoint", "MAT should sit between OAT and RAT") instead of treating points as
anonymous numbers.

Name-driven and dialect-aware: it normalizes the object name (dropping the Trane
`|dac-N_UIP~` suffix to the semantic base) so it handles the Trane verbose naming
(`Discharge Air Temp|dac-3_UIP~_06`) AND the AC-4/JCI variants (`SF-A VFD Modulation`,
`Mixed Air Temperature Evap A`). Unmatched points fall to OTHER and are simply not
used for role-based analysis — no harm.

Rules are ORDER-SENSITIVE (specific before general): a setpoint must be caught before
the bare sensor it names, alarms before the equipment they reference.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Continuous sensor/actuator roles — the ones a z-score deviation detector analyzes.
ANALYSIS_ROLES = frozenset({
    "DAT", "MAT", "RAT", "OAT", "MECH_TEMP", "ROOM_TEMP",
    "DUCT_PRESS", "SPACE_PRESS", "FAN_SPEED", "EXH_FAN_SPEED",
    "OA_DAMPER", "EF_DAMPER", "HTG_VALVE", "CLG_VALVE", "HUMIDITY", "OA_FLOW",
    # ── WATER SIDE (condenser loop + cooling towers + CW pumps) ───────────────
    # Everything above is air. Without these the whole tower plant classified as
    # OTHER, which meant no baseline was built (baseline.py skips non-analysis
    # roles), so no deviation or drift detection, and every physics check in
    # derived.py — which looks points up BY ROLE — simply never found them.
    "CW_SUP_TEMP", "CW_RET_TEMP", "CW_FLOW", "CW_DP",
    "CW_BYPASS_VALVE", "CW_BYPASS_FLOW",
    "CT_SUP_TEMP", "CT_FLOW", "CT_FAN_SPEED", "CT_BASIN_LEVEL", "CT_ISO_VALVE",
    "CW_PUMP_SPEED", "CW_PUMP_DP", "CW_PUMP_TEMP",
    # ── HOT-WATER SIDE (boilers, heating water, domestic hot water; 2026-09-29) ──
    "HW_SUP_TEMP", "HW_RET_TEMP", "BLR_SUP_TEMP", "BLR_RET_TEMP", "DHW_TEMP",
})
# Discrete/status/config roles — handled by rule-based detectors, not z-scores.
STATUS_ROLES = frozenset({
    # water-side setpoints + discrete state (rule-based checks, never z-scored)
    "CW_SUP_TEMP_SP", "CW_DP_SP", "CT_BASIN_LEVEL_SP", "CW_BYPASS_TEMP_SP",
    "CT_FAN_STATUS", "CT_FAN_CMD", "CT_MAKEUP_VALVE", "CT_ISO_VALVE_STATE",
    "CW_PUMP_STATUS", "CW_PUMP_CMD", "CW_SYS_ENABLE", "CT_SYS_ENABLE",
    "CT_STAGING", "CW_STAGING", "CW_PUMP_RUNTIME", "CT_WINTER_MODE",
    "CT_FAN_SPEED_CMD", "CW_PUMP_SPEED_CMD", "CT_ISO_VALVE_CMD", "CW_BYPASS_VALVE_CMD",
    "CT_FAN_ALARM", "CW_PUMP_ALARM", "CT_LEVEL_ALARM",
    "CT_ABORT",                   # a tower taken out of service (its riser valve closed)
    # hot-water setpoints + discrete state. BLR_CMD is the enable the program SENDS a
    # boiler; BLR_CALL is a request that makes the program call one (a manual call, a
    # low-temperature enable, a DHW call) -- different questions, so different roles.
    "HW_SUP_TEMP_SP", "BLR_ON_SP", "BLR_OFF_SP", "BLR_LOW_TEMP_SP",
    "BLR_CMD", "BLR_STATUS", "BLR_ALARM", "BLR_ISO_VALVE_CMD", "BLR_CALL", "BLR_STAGING",
    "HW_PUMP_CMD", "HW_PUMP_STATUS", "HW_PUMP_ALARM",
    "DAT_ACTIVE_SP", "DUCT_PRESS_SP", "SPACE_PRESS_SP", "ROOM_SP",
    "OA_DAMPER_MIN_SP",           # operator-set minimum OA damper position (a setpoint)
    # Limits the BAS itself publishes and enforces, trended per unit. Better than
    # anything hand-typed into this app — see ai/limits.py.
    "DAT_HI_LIMIT", "DAT_LO_LIMIT", "MAT_LO_LIMIT", "MAT_HI_LIMIT",
    "RAT_HI_LIMIT", "RAT_LO_LIMIT",
    # Economizer: the unit's OWN changeover setpoints and the controller's own verdict.
    "ECON_ENTH_SP", "ECON_DB_SP", "ECON_OK", "MECH_COOL_DISABLE",
    # equipment asking for a human, and the command half of a command/feedback pair
    "RESET_REQUIRED", "FAN_CMD",
    "FAN_STATUS", "COOLING_STAGES", "HEATING_STAGES",
    "MODE", "OCCUPANCY", "COMM", "ALARM", "FILTER", "ENTHALPY",
    "CONFIG", "INTERNAL", "OTHER",
})

# Plant-published limit role -> (role it bounds, which side of the band it sets).
LIMIT_ROLES = {
    "DAT_HI_LIMIT": ("DAT", "hi"),
    "DAT_LO_LIMIT": ("DAT", "lo"),
    "MAT_HI_LIMIT": ("MAT", "hi"),
    "MAT_LO_LIMIT": ("MAT", "lo"),
    "RAT_HI_LIMIT": ("RAT", "hi"),
    "RAT_LO_LIMIT": ("RAT", "lo"),
}
ALL_ROLES = ANALYSIS_ROLES | STATUS_ROLES


def _norm(name: str) -> str:
    """Normalize to the semantic base: drop the Trane `|controller` suffix, fold
    separators (~ _ / - . , ( ) #) to spaces, lowercase, collapse whitespace."""
    base = (name or "").split("|", 1)[0]
    n = re.sub(r"[~_/\\\-.,()#]+", " ", base.lower())
    return re.sub(r"\s+", " ", n).strip()


def is_uip(name: str) -> bool:
    """Trane `|dac-N_UIP~` = the physically-wired Universal Input Point (the real
    sensor). Preferred as canonical over a logical twin. See [[trane-uip-is-the-real-sensor]]."""
    suffix = name.split("|", 1)[1] if "|" in (name or "") else ""
    return "uip" in suffix.lower()


# ── water side: condenser loop, cooling towers, CW pumps ─────────────────────
# Whole-word tags only. A substring test is NOT safe here: every one of the 13 AHUs
# trends a point called "Freeze Condition", and `"cond" in name` routes all 13 into
# the condenser-water vocabulary. _norm has already folded dots to spaces, so
# SITE.CHW.HDR.SUP.TEMP -> "site chw hdr sup temp" and word matching is exact.
# "chwsys" / "ctsys" are whole tags on this site (SITE.CHWSYS = the plant's MASTER ENABLE,
# SITE.CTSYS = the tower system enable). Without them here those names never reached the
# water-side rules that already knew them, and the one point that says whether the plant is
# SUPPOSED to be running classified as OTHER (found against the 2026-09-26 PPCL).
_WATER_WORDS = frozenset({"chw", "cw", "cond", "condenser", "twr", "tower", "ct",
                          "ct1", "ct2", "pmp", "pmp1", "pmp2", "cwp", "basin", "sump",
                          "chwsys", "ctsys"})


def _is_water_side(name: str) -> bool:
    """Does the BACnet NAME place this point on the water side?

    Judged from the name, never the operator label or the description: the name is the
    commissioning tag and is reliably plant-scoped (SITE.CHW.*, SITE.CT1.*), whereas a
    friendly label like "Supply Temp" is ambiguous between supply AIR and condenser
    water, and this site's descriptions contain copy-paste errors."""
    return bool(_WATER_WORDS & set(_norm(name).split()))


def _classify_water(n: str) -> Optional[str]:
    """Water-side role from ONE already-normalized string. Returns None if this string
    says nothing useful, so the caller can fall through to the next source."""
    if not n:
        return None
    padded = f" {n} "

    def anyw(*ws: str) -> bool:
        return any(w in n for w in ws)

    def word(*ws: str) -> bool:
        return any(f" {w} " in padded for w in ws)

    is_sp = "setpoint" in n or word("sp", "stpt", "setpt")
    tower = word("ct", "ct1", "ct2", "twr", "tower", "ctsys") or anyw("ct1", "ct2")
    pump = word("pmp", "pmp1", "pmp2", "cwp", "pump") or anyw("pmp1", "pmp2")
    # A COMMANDED position vs the PROVED feedback are different quantities and must not
    # share a role: the panel publishes both for every fan, pump and valve, and
    # canonical_ids keeps exactly one point per (system, role) — collapsing them would
    # silently discard one. Keeping them apart also makes command-vs-feedback
    # (64% asked, 62.8% delivered) a checkable relationship.
    cmd = is_sp or anyw("vso", "vlvo", "spdo", "output", " out")

    # ── makeup / fill valve BEFORE the basin-level test: the operator's label for it is
    # "Basin Fill Valve", which contains "basin" and would otherwise read as a level.
    if anyw("makeup", "mkp", "fill", "city"):
        return "CT_MAKEUP_VALVE"
    # level SENSOR FAILURE is not a fan alarm
    if anyw("level", "lvl", "basin", "sump") and anyw("alarm", "alm", "fail"):
        return "CT_LEVEL_ALARM"
    # A tower in ABORT is taken out of service: the PPCL closes its riser valve and the
    # other tower carries the load. That explains lost capacity; it is not a fan alarm.
    if anyw("abort"):
        return "CT_ABORT" if tower else None
    if anyw("alarm", "alm", "fail"):
        return "CT_FAN_ALARM" if tower else ("CW_PUMP_ALARM" if pump else None)
    if anyw("winter"):
        return "CT_WINTER_MODE"
    if anyw("runtime", "rntm"):
        return "CW_PUMP_RUNTIME"
    if anyw("sequence", "seq", "staging", "lag", "rotat", "alt"):
        return "CT_STAGING" if tower else "CW_STAGING"
    if anyw("master", "sys enable", "sysena") or word("sys", "chwsys", "ctsys"):
        return "CT_SYS_ENABLE" if tower else "CW_SYS_ENABLE"

    # ── level ─────────────────────────────────────────────────────────────────
    if anyw("level", "lvl", "basin", "sump"):
        return "CT_BASIN_LEVEL_SP" if is_sp else "CT_BASIN_LEVEL"

    # ── isolation valve: position vs the open/closed limit contacts ────────────
    if anyw("iso", "riso"):
        if anyw("open", "closed", "clos"):
            return "CT_ISO_VALVE_STATE"
        return "CT_ISO_VALVE_CMD" if cmd else "CT_ISO_VALVE"

    # ── bypass ────────────────────────────────────────────────────────────────
    if anyw("byp"):
        if anyw("flow", "fm"):
            return "CW_BYPASS_FLOW"
        if anyw("temp"):
            return "CW_BYPASS_TEMP_SP" if is_sp else "CW_SUP_TEMP"
        # The bypass valve was the ONE water-side actuator whose command and feedback
        # shared a role. canonical_ids keeps one point per (system, role), so the pair
        # SITE.CHW.BYP.POS / SITE.CHW.BYPO silently lost a member and the valve could not
        # be checked for sticking -- the one failure a bypass valve actually has.
        # `bypo` is spelled out because this panel's output convention is a trailing O
        # (SPDO, VLVO, VSO) which the generic `cmd` test cannot see in a fused token;
        # relying on the operator's "SP" rename alone would break if the label changed.
        return "CW_BYPASS_VALVE_CMD" if (cmd or word("bypo")) else "CW_BYPASS_VALVE"

    # ── start/stop command vs proved status ───────────────────────────────────
    if word("ss", "s s") or anyw("command", "start stop"):
        return "CT_FAN_CMD" if tower else ("CW_PUMP_CMD" if pump else None)
    if word("sts") or anyw("status", "proof", "proved"):
        return "CT_FAN_STATUS" if tower else ("CW_PUMP_STATUS" if pump else None)

    # ── modulating speed ──────────────────────────────────────────────────────
    if anyw("speed", "spd", "vfd", "vso") or (tower and anyw("fan")):
        if tower:
            return "CT_FAN_SPEED_CMD" if cmd else "CT_FAN_SPEED"
        if pump:
            return "CW_PUMP_SPEED_CMD" if cmd else "CW_PUMP_SPEED"
        return None

    # ── analog readings ───────────────────────────────────────────────────────
    if anyw("flow", "fm", "gpm"):
        return "CT_FLOW" if tower else "CW_FLOW"
    if anyw("diff press", "dpte", "press") or word("dp"):
        if is_sp:
            return "CW_DP_SP"
        return "CW_PUMP_DP" if pump else "CW_DP"
    if anyw("temp"):
        if is_sp:
            return "CW_SUP_TEMP_SP"
        if pump:
            return "CW_PUMP_TEMP"
        if tower:
            return "CT_SUP_TEMP"
        if anyw("ret", "return"):
            return "CW_RET_TEMP"
        return "CW_SUP_TEMP"
    return None


# ── hot-water side: boilers, heating water, domestic hot water ───────────────
# The boiler plant arrived 2026-09-29 (its PPCL program and sequence of operations were
# read for this). Before it, every boiler point classified OTHER, or worse:
# "BLR 1 ENABLE" landed in INTERNAL. This site FUSES its boiler tags -- HB3SUP, HB3P1F,
# BLR2ENB.BAC, DHWTEMPBAC -- so the equipment prefix is split off before any word test
# (_split_hot), and, like the condenser side, the vocabulary is chosen by the BACnet name.
# NOT the word "boiler": on this site that names the boiler ROOM (Trane SC-2's program 8:
# "Boiler Rm. Space Temp", "Boiler Room Low Temp SP"), and routing those here turned a
# room temperature into the plant's supply temperature. The plant's own tags are BLR / HB / DHW.
_HOT_TOKEN = re.compile(r"^(blr\w*|hb\d+\w*|dhw\w*|hws|hwr)$")


# A NUMBERED boiler in plain English ("Boiler 1 Supply Water Temp", "Boiler 2 Enable") is the
# plant, unambiguously; "Boiler Room ..." stays out. Many sites name points this way rather than
# with tags, and without it every such point classified OTHER or INTERNAL.
_HOT_ENGLISH = re.compile(r"\bboiler \d+\b")


def _is_hot_water(name: str) -> bool:
    n = _norm(name)
    return any(_HOT_TOKEN.match(t) for t in n.split()) or bool(_HOT_ENGLISH.search(n))


def _split_hot(n: str) -> str:
    """'hb3sup' -> 'hb3 sup', 'blr2enb bac' -> 'blr2 enb bac', 'dhwtempbac' -> 'dhw temp bac'."""
    n = re.sub(r"\b(blr\d*|hb\d+)(?=[a-z])", r"\1 ", n)
    n = re.sub(r"\bdhw(?=[a-z])", "dhw ", n)
    return re.sub(r"(?<=[a-z0-9])bac\b", " bac", n)


def _classify_hot(n: str) -> Optional[str]:
    """Hot-water role from ONE already-normalized string; None when it says nothing useful,
    so the caller falls through to the next source (label, name, description)."""
    if not n:
        return None
    n = _split_hot(n)
    padded = f" {n} "

    def anyw(*ws: str) -> bool:
        return any(w in n for w in ws)

    def word(*ws: str) -> bool:
        return any(f" {w} " in padded for w in ws)

    is_sp = "setpoint" in n or anyw("stpt", "setpt") or word("sp")
    pump = anyw("circ", "pump") or bool(re.search(r"\bp\d+f?\b", n))    # HB3P1, HB3P1F
    # failures first: a flame failure or a pump failure is not a temperature
    if word("ff") or anyw("flame"):
        return "BLR_ALARM"
    if word("alm") or anyw("alarm", "fault"):
        return "HW_PUMP_ALARM" if pump else "BLR_ALARM"
    if anyw("call", "request", "lt enable", "low temp enable"):
        return "BLR_CALL"
    if word("bb1", "bb2") or anyw("lag", "stage", "staging", "sequence"):
        return "BLR_STAGING"
    if word("iso") or anyw("isolation"):
        return "BLR_ISO_VALVE_CMD"
    if anyw("on temp"):
        return "BLR_ON_SP"
    if anyw("off temp"):
        return "BLR_OFF_SP"
    if word("lt") or anyw("low temp"):
        return "BLR_LOW_TEMP_SP"          # 14FLR's "setpoint - 10" backup threshold
    if pump:
        if word("p1f", "p2f") or anyw("status", "proof", "flow", "fdbk"):
            return "HW_PUMP_STATUS"
        return "HW_PUMP_CMD"
    if word("sts") or anyw("status", "proof", "proved", "running"):
        return "BLR_STATUS"
    if word("cmd", "enb") or anyw("command", "enable"):
        return "BLR_CMD"
    if (word("dhw") or anyw("domestic")) and (anyw("temp") or word("st")):
        return "DHW_TEMP"
    if is_sp:
        return "HW_SUP_TEMP_SP"
    if anyw("temp") or word("st", "rt", "sup", "supply", "ret", "return"):
        ret = word("rt", "ret", "return")
        if re.search(r"\b(blr\d+|hb\d+|boiler \d+)\b", n):   # one numbered boiler
            return "BLR_RET_TEMP" if ret else "BLR_SUP_TEMP"
        return "HW_RET_TEMP" if ret else "HW_SUP_TEMP"          # the plant's own header
    return None


def _classify_air(n: str) -> Optional[str]:
    """Air-side role from ONE already-normalized string. This is the historic
    classifier body, unchanged — it is now callable on either the BACnet name or
    the operator's label instead of only the name."""
    if not n:
        return None
    padded = f" {n} "

    def has(*ws: str) -> bool:
        return all(w in n for w in ws)

    def anyw(*ws: str) -> bool:
        return any(w in n for w in ws)

    def word(*ws: str) -> bool:                 # whole-word (avoids 'sp' in 'space')
        return any(f" {w} " in padded for w in ws)

    is_sp = "setpoint" in n or word("sp")
    is_temp = anyw("temp", "temperature")

    # ── setpoints / config FIRST (catch before the bare sensor they name) ──────
    # discharge/supply-air temperature setpoint — the target the discharge temp tracks.
    # 'active' is NOT required: units trend a plain 'Discharge Air Setpoint' that resets
    # like an active target (confirmed against data). Pairing it with DAT is what makes
    # discharge-temp trending accurate (viewer +SP, AI context, correlation).
    if is_sp and (anyw("discharge air", "supply air", "sup air", "dischg air", "disch air")
                  or word("sat")):                # HV1 'ACT_SAT_SP' = supply-air-temp SP
        return "DAT_ACTIVE_SP"
    if is_sp and (anyw("static pressure", "duct static", "duct press") or word("dsp")):
        return "DUCT_PRESS_SP"
    if is_sp and anyw("space pressure", "building pressure"):
        return "SPACE_PRESS_SP"
    if is_sp and anyw("room", "zone", "space temp"):
        return "ROOM_SP"
    # Minimum OA damper position — an operator-set SETPOINT (the required ventilation
    # floor), not a fault signal. Pairing it with the damper is what stops "OA damper
    # is high / trending up / a peer outlier" false alarms when the damper is simply
    # parked at a minimum the operator raised (e.g. to 35%). Must be caught before the
    # bare 'damper' rule below.
    if (has("min", "oa", "damper") or has("minimum", "oa", "damper")
            or has("minimum", "outdoor air") or has("economizer", "minimum")):
        return "OA_DAMPER_MIN_SP"
    # ── economizer: the unit's OWN changeover setpoints + the controller's verdict ──
    # Every unit trends these. Before they were classified, the free-cooling check
    # compared measured OA enthalpy against a HARDCODED ASHRAE 28.0 Btu/lb while this
    # plant is commissioned at 19 — so a report said "enthalpy 27.8, just under the 28
    # changeover" when OA was in fact far ABOVE the real lockout, and correct economizer
    # behaviour was reported as a fleet-wide fault in every prior report. `OK to
    # Economize` is the controller's own answer to the same question; ignoring it meant
    # arguing with the equipment about its own decision.
    if "econ" in n:
        if "enthalpy" in n:
            return "ECON_ENTH_SP"
        if word("db") or anyw("dry bulb", "drybulb"):
            return "ECON_DB_SP"
        if anyw("ok to", "okay to"):
            return "ECON_OK"
    # mechanical-cooling lockout flag — the signal the DAT-vs-setpoint check needs to
    # become cooling-mode-aware (a unit not cooling is SUPPOSED to sit above setpoint).
    if has("disable", "mech") and "cool" in n:
        return "MECH_COOL_DISABLE"
    # A DAMPER is a damper even when the word "modulation" makes it look like a fan
    # speed. The generic fan-speed rule below fires first on "EF Damper Modulation" and
    # classified it EXH_FAN_SPEED — which also dropped it out of _COMMANDED_ROLES, so a
    # driven damper position was z-scored like a sensor.
    if "damper" in n and anyw("mod", "modulation", "position", "command", "output"):
        if anyw("ea ", "exhaust", "ef "):
            return "EF_DAMPER"
        if anyw("oa", "outside air", "outdoor air", "ra ", "return air", "economizer"):
            return "OA_DAMPER"
    # ── operator-action + command/feedback signals ────────────────────────────
    # "SF~-A Man Reset Required" is the equipment saying IT NEEDS A HUMAN — exactly the
    # manual power-cycle the operator has been performing by hand. 24 of these are
    # trended and every one was classified INTERNAL, so nothing ever looked at them.
    if has("reset") and anyw("required", "req", "manual", "man "):
        return "RESET_REQUIRED"
    # The COMMAND half of a command/feedback pair. A fan that is commanded on and whose
    # run-status feedback says otherwise is a failed fan — detectable without any alarm
    # point asserting, which is the case the operator asked for.
    # NB `_norm` folds '/' to a space, so "S/S" arrives as "s s" and "start/stop" as
    # "start stop". Matching the raw punctuation silently never fires — which is how
    # "Exhaust Fan S/S" (a START/STOP COMMAND) ended up sharing the FAN_STATUS role
    # with "Exhaust Fan Status" (its feedback), making the pair uncomparable.
    if (anyw("enable", "start stop", "cmd", "command") or " s s " in padded) and anyw(
            "sf", "supply fan", "fan", "ef", "exhaust"):
        return "FAN_CMD"
    # ── safety / alarms ────────────────────────────────────────────────────────
    if anyw("alarm", "freeze", "freezestat", "smoke", "purge", "failure",
            "emergency", "fault"):
        return "ALARM"
    if "filter" in n:
        return "FILTER"
    # ── plant-published LIMITS (after ALARM, so "Low Limit Alarm" stays an alarm) ──
    # The commissioned thresholds the controller actually enforces, trended per unit.
    # NOT all of them are alarm thresholds — measured breach rates decide which ones
    # alarm; see ai/limits.py ALARM_LIMIT_ROLES.
    if "limit" in n and not is_sp:
        if anyw("mixed air", "mix air"):
            return "MAT_LO_LIMIT" if word("low") else "MAT_HI_LIMIT"
        if anyw("return air", "ret air"):
            return "RAT_LO_LIMIT" if word("low") else "RAT_HI_LIMIT"
        if anyw("discharge air", "supply air", "sup air", "dischg air", "disch air"):
            return "DAT_LO_LIMIT" if word("low") else "DAT_HI_LIMIT"
    # ── temperatures ───────────────────────────────────────────────────────────
    if is_temp:
        if anyw("mixed air", "mix air"):
            return "MAT"
        if anyw("return air", "ret air"):
            return "RAT"
        if anyw("discharge air", "supply air", "sup air", "dischg air", "disch air"):
            return "DAT"
        # 'outdoor temperature' (no 'air') is how the tower panel's own sensor is
        # labelled; without it the one OAT the plant actually trends stays invisible,
        # and the mixed-air check needs an OAT to run at all.
        if anyw("outdoor air", "outside air", "oa temp", "oat",
                "outdoor temp", "outside temp"):
            return "OAT"
        if anyw("mech room", "mechanical room", "mech rm"):
            return "MECH_TEMP"
        if anyw("room", "zone", "space"):
            return "ROOM_TEMP"
    # ── humidity / enthalpy ────────────────────────────────────────────────────
    if anyw("humidity", "humid") or word("rh"):
        return "HUMIDITY"
    if "enthalpy" in n:
        return "ENTHALPY"
    # ── pressure (process, not setpoint — those handled above) ─────────────────
    if anyw("duct static", "static pressure", "duct press"):
        return "DUCT_PRESS"
    if anyw("space pressure", "building pressure"):
        return "SPACE_PRESS"
    # ── flow ───────────────────────────────────────────────────────────────────
    if anyw("oa flow", "outdoor air flow", "airflow", "air flow"):
        return "OA_FLOW"
    # ── fans (speed vs status; exhaust vs supply) ──────────────────────────────
    exhaust = anyw("exhaust", "ef ", "ea ") or word("ef", "ea")
    if anyw("vfd", "speed", "modulation") and anyw("fan", "vfd", "sf", "ef"):
        return "EXH_FAN_SPEED" if exhaust else "FAN_SPEED"
    if anyw("run status", "fan status", "fan proof", "proof", "s s", "start stop") \
            and anyw("fan", "sf", "ef"):
        return "FAN_STATUS"
    # ── dampers ────────────────────────────────────────────────────────────────
    # ...but not a COUNTER of damper strokes. "Damper Position Counter" (one per AC unit)
    # landed here as OA_DAMPER, and the coverage review then offered it as a damper
    # position feedback. It is an internal tally; the catch-all below files it INTERNAL.
    if "damper" in n and "counter" not in n:
        if anyw("oa", "ra", "outdoor", "economiz", "min oa"):
            return "OA_DAMPER"
        if exhaust:
            return "EF_DAMPER"
        return "OA_DAMPER"
    # ── valves ─────────────────────────────────────────────────────────────────
    if anyw("valve", "valv"):
        if anyw("hw", "heat", "reheat", "hot water"):
            return "HTG_VALVE"
        if anyw("cw", "chw", "cool", "chilled"):
            return "CLG_VALVE"
        return "HTG_VALVE"
    if anyw("reheat",):
        return "HTG_VALVE"
    # ── staging / capacity ─────────────────────────────────────────────────────
    if anyw("multistack", "compressor", "comp ", "cooling stage", "cool stage",
            "cooling capacity", "dx ") or has("stage") and anyw("cool"):
        return "COOLING_STAGES"
    if anyw("heating stage", "heat stage", "heating capacity") or (has("stage") and "heat" in n):
        return "HEATING_STAGES"
    # ── mode / occupancy / comm ────────────────────────────────────────────────
    if anyw("heat cool mode", "unit mode", "hvac mode", "operating mode"):
        return "MODE"
    if anyw("occupancy", "occupied"):
        return "OCCUPANCY"
    # "NODE6 COMM TMR" counts seconds since the 3rd-floor DP node last reported; over 300
    # the pump program stops resetting the loop DP (PPCL 2026-09-26).
    if anyw("communication", "comm status", "comm tmr", "comm tm", "comm timer"):
        return "COMM"
    # ── remaining setpoints / config / internals ───────────────────────────────
    if is_sp:
        return "CONFIG"
    if anyw("pid", "gain", "counter", "calc", "reset", "installed", "enable",
            "command", "cmd"):
        return "INTERNAL"
    return "OTHER"


def classify(name: str, display_name: str = None,
             description: str = None) -> Optional[str]:
    """Return the semantic role for a point, or None (treated as OTHER).

    Three sources, in strict precedence: the OPERATOR's display_name, then the BACnet
    name, then the BAS description. That order is not arbitrary — on this site the
    descriptions carry real copy-paste errors that the operator's renames correct:
    SITE.CHW.PMP1.SPD.FBK is described "TWR FAN FDBK" and SITE.BLDG.OAH is described
    "BUILDING OAT". Trusting the description classifies both condenser pumps as tower
    fans and the outdoor humidity sensor as a temperature. The operator renamed them
    "Pump 1 Speed" and "Outdoor Humidity"; they are right and the panel is wrong.

    Which VOCABULARY to use is decided by the BACnet name alone (_is_water_side), never
    by the label: "Supply Temp" is ambiguous between supply air and condenser water,
    while the commissioning tag SITE.CHW.HDR.SUP.TEMP is not."""
    specific = lambda r: r and r not in ("OTHER", "CONFIG", "INTERNAL")
    if _is_hot_water(name):
        for src in (display_name, name, description):
            r = _classify_hot(_norm(src or ""))
            if r:
                return r
    elif _is_water_side(name):
        for src in (display_name, name, description):
            r = _classify_water(_norm(src or ""))
            if r:
                return r
    else:
        for src in (display_name, name, description):
            r = _classify_air(_norm(src or ""))
            if specific(r):
                return r
    # nothing specific matched: keep the historic catch-all on the NAME so unmatched
    # setpoints still land in CONFIG and internals in INTERNAL, and unclassified_report
    # can still see them rather than them vanishing.
    return _classify_air(_norm(name))



# Roles whose meaningful signal is a MODULATING position, not an end-switch. The
# UIP-first rule below is correct for a SENSOR (the physically-wired thermistor beats a
# logical twin) and wrong for an ACTUATOR, where the wired UIP is typically a limit
# switch and the modulating command is the thing worth analysing.
_MODULATING_ROLES = frozenset({
    "OA_DAMPER", "EF_DAMPER", "HTG_VALVE", "CLG_VALVE", "FAN_SPEED", "EXH_FAN_SPEED",
    # same rule on the water side: the tower panel publishes a raw 0-10 V drive, a %
    # feedback AND open/closed limit contacts for the same valve — take the position.
    "CT_FAN_SPEED", "CT_ISO_VALVE", "CW_PUMP_SPEED", "CW_BYPASS_VALVE"})


def canonical_ids(points: list[dict]) -> set[int]:
    """Pick ONE canonical point per (system_id, role).

    Only ENABLED points are eligible — the curated/enabled set is the source of truth,
    so a disabled dead twin (e.g. a 132 F 'Low Outdoor Air Temp' UIP) never wins over
    the kept sensor just because it carries the UIP suffix.

    Preference depends on what KIND of thing the role is:
      · SENSORS — prefer the wired UIP, which is the real transducer.
      · MODULATING ACTUATORS — prefer a point that actually modulates. Preferring the
        UIP here picked `EA Damper 100% Open`, a BINARY END-SWITCH with exactly one
        distinct value ever recorded, as the canonical EF_DAMPER on ALL 13 UNITS, while
        the real `EF Damper Mod` (percent, analog output) was ignored. Every
        exhaust-damper check was therefore running on a contact that never changes —
        silently producing nothing, which is indistinguishable from a healthy plant.
    """
    groups: dict[tuple, list[dict]] = {}
    for p in points:
        role = p.get("role")
        if not role or role in ("OTHER", "INTERNAL", "CONFIG"):
            continue
        if "enabled" in p and not p.get("enabled"):
            continue
        groups.setdefault((p.get("system_id"), role), []).append(p)

    def rank_modulating(p: dict) -> tuple:
        units = (p.get("units_text") or "").strip().lower()
        otype = (p.get("object_type") or "").lower()
        return (
            0 if units == "percent" else 1,          # a % position beats a contact
            0 if "analog" in otype else 1,           # analog beats binary
            0 if not is_uip(p.get("name") or "") else 1,   # command over end-switch
            p.get("id") or 0,                        # deterministic tie-break
        )

    canon: set[int] = set()
    for (_sid, role), grp in groups.items():
        if role in _MODULATING_ROLES:
            chosen = sorted(grp, key=rank_modulating)[0]
        else:
            uips = [p for p in grp if is_uip(p.get("name") or "")]
            chosen = (uips or grp)[0]
        canon.add(chosen["id"])
    return canon


def assign_roles(dal) -> dict:
    """Classify every point's role from its name and persist it, then mark one
    canonical point per (system, role). Idempotent — safe to re-run after discovery.
    Read-only wrt BACnet (pure local DB writes). Returns a summary dict."""
    pts = dal.points_min()
    from collections import Counter
    dist: Counter = Counter()
    wanted: dict = {}
    for p in pts:
        role = classify(p["name"], p.get("display_name"), p.get("description"))
        p["role"] = role
        wanted[p["id"]] = role
        dist[role or "OTHER"] += 1
    # One transaction, changed rows only (M-8). Falls back to the per-point setter so an
    # older DAL -- or a test double -- still works.
    if hasattr(dal, "set_point_roles"):
        n_written = dal.set_point_roles(wanted)
    else:
        n_written = 0
        for pid, role in wanted.items():
            dal.set_point_role(pid, role)
            n_written += 1
    canon = canonical_ids(pts)
    dal.set_role_canonical(canon)
    out = {"total": len(pts), "classified": sum(1 for p in pts if p["role"]),
           "canonical": len(canon), "distribution": dict(dist), "written": n_written}
    ft = unclassified_report(pts)
    out["fell_through"] = ft
    if ft["commissioned_values"]:
        logger.warning(
            "%d ENABLED point name(s) look like COMMISSIONED VALUES but fell through to "
            "CONFIG/OTHER — a detector may be judging the plant against a hardcoded "
            "constant instead of one of these: %s",
            sum(f["count"] for f in ft["commissioned_values"]),
            "; ".join(f"{f['name']} x{f['count']} ({f['role']})"
                      for f in ft["commissioned_values"][:12]))
    if ft["invisible_points"]:
        logger.info(
            "%d enabled point(s) carry no analysis role — invisible to every detector "
            "(often cryptic BAS tags): %s",
            ft["invisible_points"], "; ".join(ft["sample"][:10]))
    return out


# Name fragments that mean "this point carries a VALUE the plant commissioned" — the
# shape of every instance of the bug this report exists to catch.
_SUSPICIOUS = ("setpoint", " sp", "limit", "min ", "minimum", "max ", "maximum",
               "changeover", "enable", "threshold", "deadband", "offset", "reset")


def unclassified_report(pts: list[dict], *, limit: int = 40) -> dict:
    """What is INVISIBLE to every detector, split into the two things that matters.

    WHY THIS IS REPORTED RATHER THAN SILENT: `classify` ends in two catch-alls
    (`if is_sp: return "CONFIG"` and an implicit OTHER), and `canonical_ids` skips
    CONFIG — so a point that falls through is invisible to every detector, permanently
    and quietly. That exact path has now produced FIVE real bugs: the min-OA damper
    position, the discharge-air setpoint, the economizer enthalpy + dry-bulb changeover,
    the commissioned alarm limits, and the controller's own `OK to Economize` flag.
    Each hid for months while a detector compared the plant against a hardcoded
    constant. The point of this function is that instance #6 announces itself on the
    next model rebuild instead of waiting for someone to notice a wrong report.

    Returns:
      commissioned_values — fell through AND the name looks like a setpoint/limit/
        threshold. THIS is the bug class: something the plant publishes that a detector
        is probably approximating with a constant. Logged as a WARNING.
      invisible_points / sample — every enabled point with no analysis role. Usually
        harmless (status bits, cryptic BAS tags like `SITE.HV1.SAS`), but it is the
        honest count of what the program cannot see, and a real sensor hiding behind an
        abbreviation shows up here.
    """
    from collections import Counter
    sus: Counter = Counter()
    roles_by_name: dict[str, str] = {}
    invisible: Counter = Counter()
    for p in pts:
        if not p.get("enabled"):
            continue
        role = p.get("role")
        if role not in (None, "CONFIG", "OTHER"):
            continue
        n = _norm(p.get("name") or "")
        if not n:
            continue
        invisible[n] += 1
        if any(w in f" {n} " for w in _SUSPICIOUS):
            sus[n] += 1
            roles_by_name[n] = role or "unclassified"
    return {
        "commissioned_values": [{"name": n, "count": c, "role": roles_by_name[n]}
                                for n, c in sus.most_common(limit)],
        "invisible_points": sum(invisible.values()),
        "invisible_names": len(invisible),
        "sample": [n for n, _ in invisible.most_common(limit)],
    }
