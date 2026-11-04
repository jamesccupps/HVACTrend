"""Point coverage: which points each monitoring rule needs, unit by unit, and what is logged.

Phase 0.5 of the AI & ML track. 2026-09-29: "make sure the points I have
selected are the actual ones that we would want and all of the ones we should be
monitoring." That is answered by what the RULES need. Every rule below names the point roles
it reads (roles come from ai/roles.py), and for each unit each role is:

    logged     an enabled point has the role, so the rule can use it;
    available  the controller has such a point but it is not logged (one click away);
    missing    the unit has no point with the role at all.

RULES is the single table of what each rule needs. Phase 1 builds its alarms from the same
table, so the review and the rules can never disagree about what a rule reads.

Requirement syntax: "DAT" = that role; "FAN_STATUS|FAN_SPEED" = either role; "OA_DAMPER/in" =
an INPUT object with the role; "OA_DAMPER*2" = at least two DISTINCT points with the role
(a command and its feedback); "OAT@building" = any logged point with the role anywhere in
the building (outdoor conditions are one reading for the whole site, not one per unit).

Some points must never be offered or counted, because they are not what their name says.
2026-09-29: the AC filter switches are not accurate, and many Trane points are BAS or
virtual points with no real I/O behind them. Two things set a point aside:

    site knowledge  the config's `site_knowledge` section: points to
                    ignore, and rules that are not possible on some units, each with a reason;
    the controller  a live read, when one is supplied. A Trane profile point that the unit's
                    program does not drive reports reliability `unreliable-other` (AC-1's
                    Outdoor Air Damper Position read 0.0 that way on 2026-09-29).

A point set aside is listed with its reason, so the review says why it was not offered.
"""

from __future__ import annotations

import fnmatch
from collections import defaultdict
from typing import Any, Iterable

UNUSED_ROLES = {None, "", "OTHER", "CONFIG", "INTERNAL"}
_INPUT_TYPES = {"analog-input", "binary-input", "multi-state-input"}

AIR_KINDS = ("ahu", "hv")
PLANT_KINDS = ("plant",)

RULES: tuple = (
    # ── air handlers: is each unit doing what it's told? ────────────────────────────────
    {"id": "sat-setpoint", "kinds": AIR_KINDS,
     "title": "Discharge air not meeting its setpoint",
     "needs": ("DAT", "DAT_ACTIVE_SP"),
     "gate": ("FAN_STATUS|FAN_SPEED",),
     "evidence": ("COOLING_STAGES", "HTG_VALVE|HEATING_STAGES", "OA_DAMPER", "MAT", "MODE"),
     "note": "tiers like 5 °F for 30 min → ACTION, 10 °F for 10 min → CRITICAL"},
    {"id": "static-setpoint", "kinds": AIR_KINDS,
     "title": "Duct static not meeting its setpoint",
     "needs": ("DUCT_PRESS", "DUCT_PRESS_SP"),
     "gate": ("FAN_STATUS|FAN_SPEED",),
     "evidence": ("FAN_SPEED", "FILTER", "OA_DAMPER", "EF_DAMPER"),
     "note": "the diagnosis asks: fan at 100 %? filter loaded? a damper shut?"},
    {"id": "full-cooling", "kinds": AIR_KINDS,
     "title": "Supply air too warm at full cooling",
     "needs": ("DAT", "DAT_ACTIVE_SP", "COOLING_STAGES"),
     "gate": ("FAN_STATUS|FAN_SPEED",),
     "evidence": ("MAT", "OAT@building"),
     "note": "Guideline 36 FC#13: capacity is maxed and the air is still warm"},
    {"id": "fan-proof", "kinds": AIR_KINDS,
     "title": "Fan commanded on but not proven",
     "needs": ("FAN_CMD", "FAN_STATUS"),
     "gate": (),
     "evidence": ("FAN_SPEED", "ALARM"),
     "note": "command vs feedback"},
    {"id": "mat-range", "kinds": AIR_KINDS,
     "title": "Mixed air outside the outdoor–return range",
     "needs": ("MAT", "RAT", "OAT@building"),
     "gate": ("FAN_STATUS|FAN_SPEED",),
     "evidence": ("OA_DAMPER",),
     "note": "Guideline 36 FC#2/#3: a bad sensor or a damper not where it says"},
    {"id": "economizer", "kinds": AIR_KINDS,
     "title": "Economizer not using free cooling, or stuck open",
     "needs": ("OA_DAMPER", "MAT", "RAT", "OAT@building"),
     "gate": ("FAN_STATUS|FAN_SPEED",),
     "evidence": ("OA_DAMPER_MIN_SP", "ECON_OK", "COOLING_STAGES"),
     "note": "judged against the unit's own changeover setpoints; outdoor enthalpy and wet "
             "bulb come from the weather feed"},
    {"id": "damper-feedback", "kinds": AIR_KINDS,
     "title": "OA damper not following its command",
     "needs": ("OA_DAMPER*2",),
     "gate": ("FAN_STATUS|FAN_SPEED",),
     "evidence": ("MAT", "RAT"),
     "note": "needs TWO points, the command and a real position feedback; on the Trane "
             "units the logged 'OA/RA Damper Mod' is the command echoed back"},
    {"id": "space-pressure", "kinds": AIR_KINDS,
     "title": "Building pressure not meeting its setpoint",
     "needs": ("SPACE_PRESS", "SPACE_PRESS_SP"),
     "gate": ("FAN_STATUS|FAN_SPEED",),
     "evidence": ("EF_DAMPER", "EXH_FAN_SPEED", "OA_DAMPER"),
     "note": ""},
    {"id": "filter", "kinds": AIR_KINDS,
     "title": "Filter loaded",
     "needs": ("FILTER",),
     "gate": (),
     "evidence": ("DUCT_PRESS", "FAN_SPEED"),
     "note": "also evidence for a static alarm"},
    {"id": "operating-state", "kinds": AIR_KINDS,
     "title": "Is the unit running, and in what mode? (gates every other rule)",
     "needs": ("FAN_STATUS|FAN_SPEED", "OCCUPANCY|MODE"),
     "gate": (),
     "evidence": ("MODE",),
     "note": "Phase 0.2: nothing is judged while the unit is scheduled off"},
    # ── condenser water plant ────────────────────────────────────────────────────────────
    # Built against the contractor's PPCL for panel PXCM100300 (towers + pumps, dated
    # 2026-09-26, still being programmed): the plant's sequence of operations. Every rule is
    # gated on the plant's MASTER ENABLE (SITE.CHWSYS): with it off, everything is shut down
    # on purpose and nothing here is a fault.
    {"id": "cw-flow-lost", "kinds": PLANT_KINDS, "applies_if": ("CW_SUP_TEMP",),
     "title": "Condenser water flow lost while the plant is enabled",
     "needs": ("CW_SYS_ENABLE@building", "CW_PUMP_STATUS@building"),
     "gate": (),
     "evidence": ("CW_PUMP_CMD@building", "CW_PUMP_ALARM@building", "CT_FLOW@building",
                  "CW_DP"),
     "note": "critical: the heat pumps and HV1 reject heat into this loop"},
    {"id": "cws-setpoint", "kinds": PLANT_KINDS, "applies_if": ("CW_SUP_TEMP", "CW_SUP_TEMP_SP"),
     "title": "Condenser water supply not meeting its setpoint",
     "needs": ("CW_SUP_TEMP", "CW_SUP_TEMP_SP"),
     "gate": ("CW_SYS_ENABLE@building", "CW_PUMP_STATUS@building"),
     "evidence": ("CT_FAN_SPEED", "CT_FAN_STATUS@building", "CT_ISO_VALVE@building",
                  "CW_BYPASS_VALVE", "CT_ABORT@building", "CT_WINTER_MODE", "OAT@building"),
     "note": "75 °F header setpoint; judged against the wet bulb from the weather, since a "
             "tower cannot cool below wet bulb + approach"},
    {"id": "bypass-when-warm", "kinds": PLANT_KINDS, "applies_if": ("CW_BYPASS_VALVE",),
     "title": "Tower bypass open while the water is warm",
     "needs": ("CW_BYPASS_VALVE", "CW_SUP_TEMP", "CW_BYPASS_TEMP_SP"),
     "gate": ("CW_SYS_ENABLE@building", "CT_WINTER_MODE"),
     "evidence": ("CW_BYPASS_VALVE_CMD", "CT_ISO_VALVE@building"),
     "note": "the program opens it only below 65 °F (setpoint - 10), or when both tower "
             "valves read under 20 %"},
    {"id": "loop-dp", "kinds": PLANT_KINDS, "applies_if": ("CW_DP", "CW_DP_SP"),
     "title": "Condenser loop differential pressure not meeting its setpoint",
     "needs": ("CW_DP", "CW_DP_SP"),
     "gate": ("CW_SYS_ENABLE@building", "CW_PUMP_STATUS@building"),
     "evidence": ("CW_PUMP_SPEED@building", "CW_PUMP_SPEED_CMD@building", "COMM"),
     "note": "header DP is reset to hold the 3rd-floor DP at 9 psi; the reset stops when "
             "the 3rd-floor node's comm timer passes 300 s"},
    {"id": "tower-fan-proof", "kinds": PLANT_KINDS, "applies_if": ("CT_FAN_CMD", "CT_FAN_STATUS"),
     "title": "Tower fan commanded on but not proven",
     "needs": ("CT_FAN_CMD", "CT_FAN_STATUS"),
     "gate": (),
     "evidence": ("CT_FAN_SPEED", "CT_FAN_SPEED_CMD", "CT_FAN_ALARM"),
     "note": ""},
    {"id": "tower-valve", "kinds": PLANT_KINDS, "applies_if": ("CT_ISO_VALVE_CMD",),
     "title": "Tower isolation valve not following its command",
     "needs": ("CT_ISO_VALVE_CMD", "CT_ISO_VALVE"),
     "gate": ("CW_SYS_ENABLE@building",),
     "evidence": ("CT_ABORT", "CT_FLOW"),
     "note": "the program starts a tower's fan only once its valve reads over 50 %"},
    {"id": "tower-basin", "kinds": PLANT_KINDS, "applies_if": ("CT_BASIN_LEVEL",),
     "title": "Tower basin level off its setpoint",
     "needs": ("CT_BASIN_LEVEL", "CT_BASIN_LEVEL_SP"),
     "gate": (),
     "evidence": ("CT_MAKEUP_VALVE@building", "CT_LEVEL_ALARM@building"),
     "note": "the program fills below setpoint - 1 in, and flags a level failure itself "
             "(fill valve on > 20 min, or off 60-92 min)"},
    {"id": "tower-approach", "kinds": PLANT_KINDS, "applies_if": ("CT_SUP_TEMP",),
     "title": "Tower approach creeping up (maintenance)",
     "needs": ("CT_SUP_TEMP",),
     "gate": ("CT_FAN_CMD",),
     "evidence": ("CT_FAN_SPEED_CMD", "CT_FLOW"),
     "note": "leaving water minus the wet bulb, trended over weeks"},
    {"id": "pump-proof", "kinds": PLANT_KINDS, "applies_if": ("CW_PUMP_CMD", "CW_PUMP_STATUS"),
     "title": "Pump commanded on but not proven",
     "needs": ("CW_PUMP_CMD", "CW_PUMP_STATUS"),
     "gate": (),
     "evidence": ("CW_PUMP_SPEED", "CW_PUMP_SPEED_CMD", "CW_PUMP_ALARM"),
     "note": "the program fails over to the standby pump on alarm"},
    # ── heating-water plant (boilers) ────────────────────────────────────────────────────
    # From the PPCL the operator supplied 2026-09-29: the boiler plant's sequence of operations. P11 runs boilers 2
    # and 3 and holds the plant supply; 14FLR (panel 101000) brings boiler 1 in as the
    # backup; HB3 is the first-floor boiler (panel 103000). The plant is forced enabled all
    # year, domestic hot water included, so these rules have no enable gate.
    {"id": "hw-setpoint", "kinds": PLANT_KINDS, "applies_if": ("HW_SUP_TEMP",),
     "title": "Heating-water supply not meeting its setpoint",
     "needs": ("HW_SUP_TEMP", "HW_SUP_TEMP_SP"),
     "gate": (),
     "evidence": ("BLR_CMD@building", "BLR_ALARM@building", "BLR_STAGING", "DHW_TEMP",
                  "OAT@building"),
     "note": "the setpoint is an outdoor reset, raised to 130 °F while domestic hot water is "
             "below 115 °F; boiler 1 is called 10 °F below it"},
    {"id": "boiler-rise", "kinds": PLANT_KINDS, "applies_if": ("BLR_RET_TEMP",),
     "title": "Boiler enabled but not heating (no temperature rise)",
     "needs": ("BLR_CMD", "BLR_SUP_TEMP", "BLR_RET_TEMP"),
     "gate": (),
     "evidence": ("BLR_ALARM", "BLR_ISO_VALVE_CMD", "HW_SUP_TEMP@building"),
     "note": "P11's own test: under 5 °F rise for 50 minutes with the boiler enabled. Its "
             "flag is not visible from here; both valves are always open, so an unfired "
             "boiler still passes water"},
    {"id": "boiler-proof", "kinds": PLANT_KINDS, "applies_if": ("BLR_STATUS",),
     "title": "Boiler enabled but not proven running",
     "needs": ("BLR_CMD", "BLR_STATUS"),
     "gate": (),
     "evidence": ("BLR_SUP_TEMP", "BLR_ALARM", "BLR_ON_SP", "BLR_OFF_SP", "OAT@building"),
     "note": "HB3 is enabled below 60 °F outdoors and cycles on its own on / off temperatures"},
    {"id": "boiler-alarm", "kinds": PLANT_KINDS, "applies_if": ("BLR_ALARM",),
     "title": "Boiler in alarm",
     "needs": ("BLR_ALARM",),
     "gate": (),
     "evidence": ("BLR_CMD", "BLR_SUP_TEMP"),
     "note": "with boiler 2 or 3 in alarm, P11 calls both, and 14FLR may call boiler 1"},
    {"id": "backup-boiler", "kinds": PLANT_KINDS, "applies_if": ("BLR_ISO_VALVE_CMD",),
     "title": "Backup boiler called (the plant fell behind, or a boiler alarmed)",
     "needs": ("BLR_CMD",),
     "gate": (),
     "evidence": ("BLR_CALL", "BLR_ISO_VALVE_CMD", "HW_SUP_TEMP@building",
                  "HW_SUP_TEMP_SP@building", "BLR_ALARM@building"),
     "note": "boiler 1 runs when the plant is 10 °F below setpoint with a boiler enabled, on a "
             "boiler alarm, or on a manual call"},
    {"id": "dhw-temp", "kinds": PLANT_KINDS, "applies_if": ("DHW_TEMP",),
     "title": "Domestic hot water too cool",
     "needs": ("DHW_TEMP",),
     "gate": (),
     "evidence": ("HW_SUP_TEMP", "HW_SUP_TEMP_SP"),
     "note": "below 115 °F the program raises the boiler minimum to 130 °F"},
    {"id": "hw-pump-proof", "kinds": PLANT_KINDS, "applies_if": ("HW_PUMP_CMD",),
     "title": "Heating pump commanded on but not proven",
     "needs": ("HW_PUMP_CMD", "HW_PUMP_STATUS"),
     "gate": (),
     "evidence": ("BLR_STATUS", "BLR_SUP_TEMP"),
     "note": "HB3's circulator"},
)

_ORDER = {"logged": 0, "available": 1, "missing": 2}
_NOT_COUNTED = {"n/a", "not-possible"}      # statuses left out of a unit's readiness count


def _roles_of(req: str) -> list:
    return req.split("@")[0].split("*")[0].replace("/in", "").split("|")


# Every role any rule reads, anywhere. A logged point whose role is in here is "used", even on
# a unit where that particular rule does not apply (the tower rules read the loop's fill valve).
RULE_ROLES = frozenset(r for rule in RULES
                       for req in rule["needs"] + rule["gate"] + rule["evidence"]
                       for r in _roles_of(req))


def _parse(req: str) -> "tuple[list[str], bool, bool, int]":
    """'OA_DAMPER/in' -> (['OA_DAMPER'], inputs_only, building_wide, how_many)."""
    building = req.endswith("@building")
    req = req[: -len("@building")] if building else req
    count = 1
    if "*" in req:
        req, _, n = req.partition("*")
        count = int(n)
    inputs_only = req.endswith("/in")
    req = req[: -len("/in")] if inputs_only else req
    return req.split("|"), inputs_only, building, count


def _applies(rule: dict, kind: str, roles_here: set) -> bool:
    """By unit type, then (for the plant) by what the system holds. HV1 carries condenser
    bypass-valve points of its own, and without the type check the LOOP's bypass rule
    attached itself to an air handler."""
    if kind not in rule.get("kinds", (kind,)):
        return False
    if "applies_if" in rule:
        return any(r in roles_here for r in rule["applies_if"])
    return True


def build(points: Iterable[dict], knowledge: Any = None, live: "dict | None" = None) -> dict:
    """The coverage report from one pass over the points.

    `points`: dicts with id, system_code, system_kind, role, enabled, object_type, name and
    display_name (SQLiteDAL.coverage_points). `knowledge`: the config's `site_knowledge`
    (see site_facts). `live`: optional {point id: live read} from the controllers, as
    api._read_points_live returns it. Pure: no I/O, so it is tested directly."""
    facts = site_facts(knowledge)
    by_sys: dict = defaultdict(list)             # system -> the points the review may use
    building: dict = defaultdict(list)          # role -> every usable point with it, anywhere
    ignored: dict = defaultdict(list)            # system -> points set aside, with the reason
    inventory: dict = defaultdict(lambda: {"kind": "", "points": 0, "logged": 0})
    unassigned_unused = []
    for p in points:
        code = p.get("system_code")
        inv = inventory[code]                    # counts include points set aside
        inv["kind"] = inv["kind"] or p.get("system_kind") or ""
        inv["points"] += 1
        inv["logged"] += 1 if p.get("enabled") else 0
        why = set_aside(p, facts, live)
        if why:
            # listed when the review would otherwise have used it, and always when it is
            # logged: logging a point the site says is not real deserves a second look
            if p.get("role") in RULE_ROLES or p.get("enabled"):
                ignored[code].append(dict(_brief(p), enabled=bool(p.get("enabled")), why=why))
            continue
        by_sys[code].append(p)
        building[p.get("role")].append(p)
        if p.get("enabled"):
            if code is None and p.get("role") in UNUSED_ROLES:
                unassigned_unused.append(_brief(p))

    systems = []
    elsewhere: dict = defaultdict(dict)          # owning system -> building-wide suggestions
    for code in sorted((c for c in inventory if c is not None), key=_natural):
        pts = by_sys[code]
        kind = inventory[code]["kind"]
        roles_here = {p.get("role") for p in pts}
        rules_out, suggest = [], {}
        for rule in RULES:
            if not _applies(rule, kind, roles_here):
                continue
            off = _rule_off(rule["id"], code, facts)
            if off:
                rules_out.append({"id": rule["id"], "title": rule["title"],
                                  "status": "not-possible", "why": off,
                                  "needs": [], "gate": [], "evidence": []})
                continue
            needs = [_state(req, pts, building) for req in rule["needs"]]
            gate = [_state(req, pts, building) for req in rule["gate"]]
            evidence = [_state(req, pts, building) for req in rule["evidence"]]
            worst = max((n["state"] for n in needs + gate), key=_ORDER.get, default="logged")
            status = {"logged": "ready", "available": "needs-logging", "missing": "missing"}[worst]
            if needs and all(n["state"] == "missing" for n in needs):
                status = "n/a"       # the unit has none of it: not a gap, a different unit
            rules_out.append({"id": rule["id"], "title": rule["title"], "status": status,
                              "needs": needs, "gate": gate, "evidence": evidence})
            for item in needs + gate + evidence:
                if item["state"] != "available":
                    continue
                for cand in item["candidates"]:
                    # a building-wide candidate is listed under the system that OWNS it
                    # (the master enable under the condenser loop), not under every
                    # system whose rules would read it
                    home = suggest if not item["building"] else elsewhere[cand["system"]]
                    s = home.setdefault(cand["id"], dict(cand, rules=[]))
                    if rule["id"] not in s["rules"]:
                        s["rules"].append(rule["id"])
        unused = [_brief(p) for p in pts
                  if p.get("enabled") and (p.get("role") in UNUSED_ROLES
                                           or p.get("role") not in RULE_ROLES
                                           and p.get("role") not in _ALWAYS_USEFUL)]
        systems.append({
            "code": code, "kind": kind,
            "logged": inventory[code]["logged"], "points": inventory[code]["points"],
            "rules": rules_out,
            "ready": sum(1 for r in rules_out if r["status"] == "ready"),
            "applicable": sum(1 for r in rules_out if r["status"] not in _NOT_COUNTED),
            "suggest": suggest,
            "unused": unused,
            "ignored": sorted(ignored.get(code, []),
                              key=lambda x: (x["role"] or "", x["name"] or "")),
        })
    for s in systems:                            # fold in what other systems' rules asked for
        for pid, cand in elsewhere.get(s["code"], {}).items():
            mine = s["suggest"].setdefault(pid, dict(cand, rules=[]))
            mine["rules"] += [r for r in cand["rules"] if r not in mine["rules"]]
        s["suggest"] = sorted(s["suggest"].values(), key=lambda x: (x["role"], x["name"]))
    unassigned_suggest = sorted(elsewhere.get(None, {}).values(),
                                key=lambda x: (x["role"], x["name"]))
    return {"rules": [{k: r[k] for k in ("id", "title", "note")} for r in RULES],
            "systems": systems, "unassigned_unused": unassigned_unused,
            "unassigned_suggest": unassigned_suggest,
            "unassigned_ignored": ignored.get(None, []),
            "knowledge": facts}


def site_facts(raw: Any) -> dict:
    """The config's `site_knowledge` section, checked. Three lists, each entry with its reason:

        ignore_points       [{match: <glob on the BACnet name>, systems: <glob(s), optional>,
                              why}]  never offered, never counted towards a rule
        rules_not_possible  [{rule: <a RULES id>, systems: <glob(s)>, why}]  reported as
                              "not possible" on those units, not as a gap
        expected_overrides  [{match, systems (optional), value (optional), why}]  a point
                              held at an override level ON PURPOSE (bacnet/priority.py): shown,
                              but not counted or alarmed on, while it holds that value

    Globs are case-insensitive. A malformed entry raises ValueError naming it: a fact that
    silently failed to apply would put the points the operator ruled out back into the review."""
    raw = raw.as_dict() if hasattr(raw, "as_dict") else (raw or {})
    if not isinstance(raw, dict):
        raise ValueError(f"site_knowledge must be a mapping, not {type(raw).__name__}")
    ids = {r["id"] for r in RULES}
    out: dict = {"ignore_points": [], "rules_not_possible": [], "expected_overrides": []}
    for i, e in enumerate(raw.get("ignore_points") or []):
        where = f"site_knowledge.ignore_points[{i}]"
        if not isinstance(e, dict) or not _text(e.get("match")) or not _text(e.get("why")):
            raise ValueError(f"{where} needs 'match' and 'why': {e!r}")
        out["ignore_points"].append({"match": e["match"].strip(),
                                     "systems": _globs(e.get("systems"), where, required=False),
                                     "why": e["why"].strip()})
    for i, e in enumerate(raw.get("rules_not_possible") or []):
        where = f"site_knowledge.rules_not_possible[{i}]"
        if not isinstance(e, dict) or not _text(e.get("why")):
            raise ValueError(f"{where} needs 'rule', 'systems' and 'why': {e!r}")
        if e.get("rule") not in ids:
            raise ValueError(f"{where}: unknown rule {e.get('rule')!r} (known: {sorted(ids)})")
        out["rules_not_possible"].append({"rule": e["rule"],
                                          "systems": _globs(e.get("systems"), where),
                                          "why": e["why"].strip()})
    for i, e in enumerate(raw.get("expected_overrides") or []):
        where = f"site_knowledge.expected_overrides[{i}]"
        if not isinstance(e, dict) or not _text(e.get("match")) or not _text(e.get("why")):
            raise ValueError(f"{where} needs 'match' and 'why': {e!r}")
        value = e.get("value")
        if isinstance(value, bool):       # YAML reads a bare OFF / ON as false / true
            value = "ON" if value else "OFF"
        if value is not None and not _text(str(value)):
            raise ValueError(f"{where}: 'value' must be text or a number, not {value!r}")
        out["expected_overrides"].append({
            "match": e["match"].strip(),
            "systems": _globs(e.get("systems"), where, required=False),
            "value": None if value is None else str(value).strip(),
            "why": e["why"].strip()})
    return out


def _text(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _globs(v: Any, where: str, required: bool = True) -> "list[str] | None":
    """'AC-*' or ['AC-1', 'AC-2'] -> a list of globs; None (optional, absent) = any system."""
    if v is None and not required:
        return None
    items = [v] if isinstance(v, str) else v
    if not isinstance(items, list) or not items or not all(_text(s) for s in items):
        raise ValueError(f"{where}: 'systems' must be a glob or a list of globs, not {v!r}")
    return [s.strip() for s in items]


def _glob(pattern: str, text: "str | None") -> bool:
    # fnmatchcase on lowered strings: plain fnmatch applies os.path.normcase, which on Windows
    # also turns '/' into '\\' -- and Trane names carry slashes ('OA/RA Damper Mod').
    return fnmatch.fnmatchcase((text or "").lower(), pattern.lower())


def set_aside(p: dict, facts: dict, live: "dict | None") -> "str | None":
    """Why the review must not use this point; None when it may. The alarm rules
    (rules/evaluate.py) ask the same question through this same function, so a point the operator
    ruled out can be neither offered here nor alarmed on there."""
    code = p.get("system_code")
    for f in facts["ignore_points"]:
        if _glob(f["match"], p.get("name")) and (
                f["systems"] is None
                or code is not None and any(_glob(s, code) for s in f["systems"])):
            return f["why"]
    got = (live or {}).get(p.get("id"))
    if got and got.get("quality") == "fault":
        return ("its controller reports it " + (got.get("reliability") or "out of service")
                + " (read live)")
    return None


def _rule_off(rule_id: str, code: str, facts: dict) -> "str | None":
    for f in facts["rules_not_possible"]:
        if f["rule"] == rule_id and any(_glob(s, code) for s in f["systems"]):
            return f["why"]
    return None


# Roles worth logging even though no rule reads them yet: alarms, limits, and the plant's
# own staging -- context a person reads on the chart.
_ALWAYS_USEFUL = {"ALARM", "RESET_REQUIRED", "DAT_HI_LIMIT", "DAT_LO_LIMIT", "MAT_LO_LIMIT",
                  "MAT_HI_LIMIT", "RAT_HI_LIMIT", "RAT_LO_LIMIT", "ECON_ENTH_SP", "ECON_DB_SP",
                  "MECH_COOL_DISABLE", "COMM", "HUMIDITY", "OA_FLOW", "CT_STAGING",
                  "CW_STAGING", "CW_PUMP_RUNTIME", "CT_WINTER_MODE", "CW_SYS_ENABLE",
                  "CT_SYS_ENABLE", "CT_ISO_VALVE", "CT_ISO_VALVE_CMD", "CT_ISO_VALVE_STATE",
                  "CW_BYPASS_FLOW", "CW_BYPASS_TEMP_SP", "CW_BYPASS_VALVE_CMD",
                  "CW_PUMP_SPEED_CMD", "CT_FAN_SPEED_CMD", "CW_PUMP_DP", "CW_PUMP_TEMP",
                  "CW_RET_TEMP", "CW_FLOW", "OAT", "ENTHALPY", "ROOM_TEMP", "ROOM_SP",
                  "MECH_TEMP"}


def _state(req: str, pts: list, building: dict) -> dict:
    roles, inputs_only, wide, count = _parse(req)

    def ok(p):
        return p.get("role") in roles and (not inputs_only
                                            or p.get("object_type") in _INPUT_TYPES)
    if wide:
        pts = [p for r in roles for p in building.get(r, [])]
    here = [p for p in pts if ok(p)]
    logged = [p for p in here if p.get("enabled")]
    if len(logged) >= count:
        state = "logged"
    elif len(here) >= count:
        state = "available"
    else:
        state = "missing"
    return {"req": req, "roles": roles, "building": wide, "state": state,
            "logged": [_brief(p) for p in logged][:3 if wide else None],
            "candidates": [dict(_brief(p), role=p.get("role"), system=p.get("system_code"))
                           for p in here if not p.get("enabled")] if state == "available"
            else []}


def _brief(p: dict) -> dict:
    return {"id": p.get("id"), "name": p.get("display_name") or p.get("name"),
            "object_type": p.get("object_type"), "role": p.get("role")}


def _natural(code: str):
    """AC-2 before AC-10."""
    import re
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", code or "")]
