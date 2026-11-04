"""Reconcile the program's HARDCODED ASSUMPTIONS against what the plant PUBLISHES.

This module exists because three comprehensive code reviews missed the same bug five
times. Every one of them looked like this:

    _ECON_ENTH_CHANGEOVER = 28.0   # Btu/lb dry air; OA above -> lock economizer out

That is impeccable code. It is also a wrong number for this building, which trends its
own `Econ Enable Enthalpy` at 19. A code review cannot see the difference, because the
evidence is not in the source tree — it is in the points table. The program reported
"OA enthalpy 27.8, just under the 28 changeover, missed free cooling" in every report
for months, while the equipment was correctly locked out.

CONFIRMED INSTANCES OF THE PATTERN (all now fixed):
  min OA damper position · discharge-air setpoint · economizer enthalpy changeover ·
  economizer dry-bulb changeover · the commissioned alarm limits · `OK to Economize`

So: every module-level threshold in the analysis code must be DECLARED here, with an
explicit statement of whether the plant publishes its own version of that number. The
test suite asserts the registry is complete, so adding a new constant without answering
the question fails. `scripts/reconcile_plant_truth.py` runs the other half — searching a
real DB for points that look like the plant's own version of a constant we assume.
"""

from __future__ import annotations

import re
from typing import Any, Optional

# ── the registry ─────────────────────────────────────────────────────────────
# name -> (module, status, note)
#   status "reads_plant"  the code prefers a trended point; the constant is only a
#                         fallback for a unit that does not publish one.
#   status "no_plant_equivalent"
#                         the plant does not publish this; it is OUR analysis tuning,
#                         not a commissioned control value. Safe to hardcode.
#   status "UNVERIFIED"   nobody has checked. Each of these is a candidate to be the
#                         next 28-vs-19. Resolve by running scripts/reconcile_plant_truth.py
#                         against the live DB and either wiring the point or recording
#                         "no_plant_equivalent" with the evidence.
CONSTANTS: dict[str, tuple[str, str, str]] = {
    # --- economizer: the one that burned us -----------------------------------
    "_ECON_ENTH_CHANGEOVER": (
        "derived", "reads_plant",
        "ECON_ENTH_SP per unit (this plant: 19 Btu/lb); constant is the fallback."),
    "_ECON_WB_CHANGEOVER": (
        "derived", "no_plant_equivalent",
        "No wet-bulb changeover is published; used only when enthalpy is unavailable."),
    "_ECON_FREE_OAT": (
        "derived", "reads_plant",
        "ECON_DB_SP per unit (this plant: 60 F); constant is the fallback."),
    "_ECON_HOT_OAT": (
        "derived", "UNVERIFIED",
        "Dry-bulb 'too hot to economize' fallback. Plant may express this via "
        "ECON_DB_SP + a deadband; nobody has checked."),
    "_CMD_MIN_PCT": (
        "derived", "no_plant_equivalent",
        "Below this command the drive is at rest and its feedback sits on the VFD's own "
        "minimum-speed floor (33% on this tower) -- comparing them there manufactures a "
        "fault every night. Our gate, not a control value."),
    "_CMD_FBK_FRAC": (
        "derived", "no_plant_equivalent",
        "Fraction of actively-commanded samples that must disagree before reporting. "
        "Our noise gate."),
    # --- cooling tower / condenser water --------------------------------------
    "_CT_APPROACH_HI": (
        "derived", "no_plant_equivalent",
        "Approach (supply - wet-bulb) is a DERIVED performance number; the panel "
        "publishes no approach target. Checked against the live PXCM100300 object "
        "list: no approach, effectiveness or design-approach point exists. 10 F is a "
        "design rule of thumb -- if a commissioning document gives this tower's design "
        "approach, that value belongs here instead."),
    "_CT_APPROACH_MIN_LOAD": (
        "derived", "no_plant_equivalent",
        "Our own gate: the fan speed at which the tower is effectively flat out, so a wide "
        "approach means it cannot keep up rather than that it was never asked to. "
        "Analysis tuning, not a control value. The panel DOES publish per-device "
        "minimum VFD speeds (PMP1MINVSO etc) which are a different quantity."),
    # Applied in ONE direction only (supply ABOVE setpoint). See the comment at the
    # check in derived.py: below-setpoint is a tower doing the only thing it can.
    "_CW_SP_DEV_F": (
        "derived", "reads_plant",
        "CW_SUP_TEMP_SP is trended (this plant: 75.0 F) and is used directly; this is "
        "only the allowed deviation band around whatever the plant publishes."),
    "_CW_DP_DEV_PCT": (
        "derived", "reads_plant",
        "CW_DP_SP is trended (this plant: 50 psi) and is used directly; this is only "
        "the allowed percentage deviation around it."),
    "_CMD_FBK_TOL": (
        "derived", "no_plant_equivalent",
        "Gap allowed between a commanded and a proved position. The plant publishes "
        "both values but no tolerance between them; this is our comparison band."),
    "_WATER_TS_WINDOW_S": (
        "derived", "no_plant_equivalent",
        "Minimum span given to the time-series water checks (6 h). Not a plant value -- "
        "it is set by OUR OWN sample-count gates: the command/feedback comparison needs "
        "30 commanded samples and the approach check needs 2 loaded hours, neither of "
        "which fits the 30-minute alert window. Measured on this plant: 30 min yields "
        "nothing, 2 h yields the approach check, 6 h yields both. Re-derive if the poll "
        "interval or either gate changes."),
    # --- damper ---------------------------------------------------------------
    "_DAMPER_MIN": (
        "derived", "reads_plant",
        "OA_DAMPER_MIN_SP per unit; constant is the fallback when a unit has none."),
    "_DAMPER_WIDE": (
        "derived", "no_plant_equivalent",
        "Our notion of 'meaningfully open'; not a commissioned value."),
    # --- control-error thresholds --------------------------------------------
    "_DAT_DEV": (
        "derived", "UNVERIFIED",
        "5 F discharge-vs-setpoint tolerance. The plant trends DAT high/low LIMITS "
        "(70/55 here) which may be the intended band; check before enabling the "
        "dat_vs_setpoint alarm."),
    "_DSP_ERR": (
        "derived", "UNVERIFIED",
        "0.3 in wc duct-static tolerance. Plant may publish a static deadband."),
    "_MIXED_RESID": (
        "derived", "UNVERIFIED",
        "6 F mixed-air blend residual. Our physics check, but the plant publishes a "
        "Mixed Air Low Limit (52 F) that bounds the same quantity."),
    "_HTG_ON": (
        "derived", "UNVERIFIED",
        "10% heating-valve-open threshold. Likely ours, unconfirmed."),
    # --- fleet / statistical (ours by construction) ---------------------------
    "_FLEET_MIN": ("derived", "no_plant_equivalent", "Statistical: minimum peer count."),
    "_FLEET_Z": ("derived", "no_plant_equivalent", "Statistical: fleet outlier sigma."),
    # --- prescreen: all statistical tuning, nothing the BAS publishes ----------
    "_FAULT_FRACTION": ("prescreen", "no_plant_equivalent", "Our quality tuning."),
    "_FAULT_PERSISTENT": ("prescreen", "no_plant_equivalent", "Our quality tuning."),
    "_COMM_FRACTION": ("prescreen", "no_plant_equivalent", "Our quality tuning."),
    "_MIN_STUCK_SAMPLES": ("prescreen", "no_plant_equivalent", "Our sample-count floor."),
    "_Z_THRESH": ("prescreen", "no_plant_equivalent", "Statistical."),
    "_Z_HIGH": ("prescreen", "no_plant_equivalent", "Statistical."),
    "_MIN_BASELINE_N": ("prescreen", "no_plant_equivalent", "Statistical."),
    "_STUCK_CHANGED_FRAC": ("prescreen", "no_plant_equivalent", "Statistical."),
    "_REL_MIN_PAIRS": ("prescreen", "no_plant_equivalent", "Statistical."),
    "_OCC_SETTLE_MIN": (
        "prescreen", "UNVERIFIED",
        "60 min ramp allowance. Plant staggers real start times per unit and those ARE "
        "detectable from history — a per-unit settle would be better than one number."),
    "_LIMIT_SETTLE_MIN": (
        "prescreen", "UNVERIFIED",
        "30 min duct flush-out allowance after a fan start, before air-side limits are "
        "judged. Physically it is a duct-volume / airflow question, and the plant "
        "publishes design_cfm per unit in equipment_meta — a unit with a big duct and a "
        "low CFM legitimately takes longer to clear. One number for all 14 is a "
        "simplification nobody has checked."),
    "_DRIFT_HORIZON_DAYS": ("prescreen", "no_plant_equivalent", "Our projection window."),
    "_DRIFT_MIN_DAYS": ("prescreen", "no_plant_equivalent", "Our trust gate."),
    "_DRIFT_SIG_FRAC": ("prescreen", "no_plant_equivalent", "Statistical."),
    "_DRIFT_SAMPLES_PER_DAY": ("prescreen", "no_plant_equivalent", "Poll-rate derived."),
    "_DRIFT_MIN_OFFSET_SIG": ("prescreen", "no_plant_equivalent", "Statistical."),
    "_DRIFT_MIN_MOVE_SIG": ("prescreen", "no_plant_equivalent", "Statistical."),
    "_DRIFT_SOON_DAYS": ("prescreen", "no_plant_equivalent", "Our severity cut."),
}

VALID_STATUS = frozenset({"reads_plant", "no_plant_equivalent", "UNVERIFIED"})

# Name fragments that suggest a trended point IS the plant's own version of a constant.
PROBES: dict[str, tuple[str, ...]] = {
    "_ECON_HOT_OAT": ("econ", "changeover", "high limit oa", "oa lockout"),
    "_DAT_DEV": ("discharge air", "supply air", "sat", "deadband", "tolerance"),
    "_DSP_ERR": ("static", "duct press", "deadband"),
    "_MIXED_RESID": ("mixed air", "mix air"),
    "_HTG_ON": ("heat", "hw valve", "htg"),
    "_OCC_SETTLE_MIN": ("start", "warmup", "warm up", "optimal start", "ramp"),
}

_CONST_RE = re.compile(r"^(_[A-Z][A-Z0-9_]*)\s*=\s*[-0-9]")


def declared_constants(source: str) -> set[str]:
    """Module-level UPPER_CASE numeric constants defined in `source`."""
    return {m.group(1) for m in (_CONST_RE.match(ln) for ln in source.splitlines()) if m}


def unregistered(source: str, module: str) -> set[str]:
    """Constants in `source` that nobody has declared in CONSTANTS."""
    return {c for c in declared_constants(source)
            if CONSTANTS.get(c, (None,))[0] != module}


def unverified() -> list[tuple[str, str, str]]:
    """Constants nobody has checked against the plant — the candidate next bug."""
    return [(n, m, note) for n, (m, s, note) in sorted(CONSTANTS.items())
            if s == "UNVERIFIED"]


def probe_plant(dal: Any, *, limit: int = 6) -> dict[str, list[dict]]:
    """For each UNVERIFIED constant, find enabled points whose names suggest the plant
    publishes its own version. Evidence for a human, not an automatic rewire — the
    match is by name and a name is a hint, not a contract."""
    try:
        pts = [p for p in dal.points_for_analysis()]
    except Exception:                                   # noqa: BLE001
        return {}
    out: dict[str, list[dict]] = {}
    for name, _m, _note in unverified():
        probes = PROBES.get(name, ())
        if not probes:
            continue
        hits = []
        for p in pts:
            n = (p.get("name") or "").lower()
            if any(w in n for w in probes) and any(
                    w in n for w in ("setpoint", " sp", "limit", "deadband", "min",
                                     "max", "changeover", "tolerance", "start")):
                hits.append({"point_id": p["id"], "name": p["name"],
                             "role": p.get("role"), "system": p.get("system_code")})
            if len(hits) >= limit:
                break
        if hits:
            out[name] = hits
    return out
