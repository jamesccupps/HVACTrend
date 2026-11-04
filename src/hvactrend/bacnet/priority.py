"""Who is in control of a commandable point: its BACnet priority array, READ (never written).

2026-10-06: "does this program record the priority levels of points, or if I have
something overridden in operator would it know?" It did not. Every reading stores the status
flags, and one of them is "overridden" -- but in 30 days not one of the 722 logged points
raised it, because that flag reports an override LOCAL to the device (a hand switch). A
command from an operator workstation is a write into one of the point's 16 priority slots,
normally 8 ("manual operator"), and the present value is the first slot that is not empty
(or the relinquish default when every slot is). So the collector reads the priority array of
every logged commandable point (collector/service.py, every bacnet.poll.priority_interval_s)
and the database keeps who is in control of each one (point_command, point_command_log).

Reading the array changes nothing on the controller: it is a ReadPropertyMultiple through the
same read-only application as every other read here.
"""

from __future__ import annotations

import fnmatch
import time
from typing import Any, Optional

# Object types that are, or may be, commandable. Outputs always are; a value object is when
# its device gave it a priority array. One without answers "unknown-property" and is then
# not asked again (point_command.has_array = 0).
COMMANDABLE_TYPES = frozenset({"analog-output", "binary-output", "multi-state-output",
                               "analog-value", "binary-value", "multi-state-value"})

# BACnet's standard command priorities (ASHRAE 135, "Standard Command Priorities"). 1-7 are
# life safety and critical equipment control, 8 is a manual operator; programs normally
# command at 9-16.
LEVEL_NAMES = {1: "manual life safety", 2: "automatic life safety",
               5: "critical equipment control", 6: "minimum on/off", 8: "manual operator"}
OVERRIDE_MAX_LEVEL = 8

# But a level of 8 is not by itself an override. The first read at the first building (2026-10-06) found 86
# points at 1-8, and 78 of them were the Trane units' own setpoints and limits, held at 8 on
# every unit alike: that is how those controllers keep a configured setpoint. So a point is
# overridden when its level is 1-8 AND more important than the USUAL level of the same point
# on the building's other units of its kind (the same canonical role, else the same BACnet
# name). A point with no peers counts from 8 down. And a value that keeps cycling at the
# same level (1, 2, 1: occupied by day, unoccupied by night) is a schedule writing there, not
# a person: AC-6's and AC-12's occupancy requests sit at 8 while the others sit at 10-12.

# Not every occupancy request at 8 is an override. On the Trane units a tenant's thermostat
# override button writes the unit's occupancy request at 8, occupied, for about two hours, and
# each press restarts the timer: in the first ten days, sessions of 10 min to 6 h on AC-6,
# AC-12 and AC-13, by day, in the evening and on a Saturday. 2026-10-15: list them
# separately, as after-hours use (DAL.after_hours_sessions). Held occupied for longer than
# this it is an override again: nobody keeps pressing a button all night.
OCCUPANCY_BUTTON_MAX_S = 8 * 3600

_SLOTS = 16
_CHOICES = ("null", "real", "enumerated", "unsigned", "boolean", "integer", "double", "time",
            "characterString", "octetString", "bitString", "date", "objectidentifier",
            "constructedValue", "datetime")


def level_name(level: Optional[int]) -> str:
    if level is None:
        return "nothing commanded"
    return LEVEL_NAMES.get(int(level), f"level {int(level)}")


def is_override(level: Optional[int], max_level: int = OVERRIDE_MAX_LEVEL) -> bool:
    return level is not None and 1 <= int(level) <= int(max_level)


def _slot(pv: Any) -> "tuple[bool, Any]":
    """(empty, value) for one slot: a bacpypes3 PriorityValue choice."""
    if pv is None:
        return True, None
    for name in getattr(type(pv), "_elements", None) or _CHOICES:
        v = getattr(pv, name, None)
        if v is not None:
            return name == "null", (None if name == "null" else v)
    return True, None


def active(priority_array: Any) -> "tuple[Optional[int], Any]":
    """(level, value) of the first slot that is not empty, or (None, None) when every slot
    is (the point sits at its relinquish default). ValueError when the reply is not a
    16-slot priority array."""
    arr = priority_array
    if hasattr(arr, "cast_out"):                 # an undecoded Any
        from bacpypes3.basetypes import PriorityArray
        arr = arr.cast_out(PriorityArray)
    try:
        slots = list(arr)
    except TypeError as exc:
        raise ValueError(f"not a priority array: {type(priority_array).__name__}") from exc
    if len(slots) != _SLOTS:
        raise ValueError(f"a priority array has {_SLOTS} slots, this one {len(slots)}")
    for level, pv in enumerate(slots, start=1):
        empty, v = _slot(pv)
        if not empty:
            return level, v
    return None, None


_UNUSED_ROLES = {None, "", "OTHER", "CONFIG", "INTERNAL"}


def _peer_key(r: dict) -> tuple:
    kind = r.get("system_kind") or ""
    if r.get("role_canonical") and r.get("role") not in _UNUSED_ROLES:
        return kind, "role", r["role"]
    return kind, "name", (r.get("name") or "").split("|")[0].strip().lower()


def _usual(levels: list) -> Optional[int]:
    """The level most of a point's peers sit at, when at least two agree (ties go to the
    less important level, the program's). None: too few peers to say."""
    if len(levels) < 2:
        return None
    counts: dict = {}
    for lv in levels:
        k = 99 if lv is None else int(lv)
        counts[k] = counts.get(k, 0) + 1
    level, n = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
    return level if n >= 2 else None


def _cycling(values: list) -> bool:
    """The values written at one level, oldest first: a value that comes back after another
    (1, 2, 1) is a schedule, not a person."""
    seen: set = set()
    last = object()
    for v in values:
        if v != last and v in seen:
            return True
        seen.add(v)
        last = v
    return False


def overrides(rows: list, logs: Optional[dict] = None,
              max_level: int = OVERRIDE_MAX_LEVEL) -> list:
    """The overridden points among `rows` (every commandable point's state: level, value,
    since, name, role, system), oldest first, each with `usual_level` (its peers'). `logs`
    maps a point id to the values recorded at its CURRENT level since it entered it."""
    groups: dict = {}
    for r in rows:
        groups.setdefault(_peer_key(r), []).append(r.get("level"))
    out = []
    for r in rows:
        lvl = r.get("level")
        if not is_override(lvl, max_level):
            continue
        usual = _usual(groups[_peer_key(r)])
        if usual is not None and int(lvl) >= usual:
            continue                     # where every unit keeps this point: configuration
        if _cycling((logs or {}).get(r["point_id"], [])):
            continue                     # a schedule writing at this level
        out.append(dict(r, usual_level=usual))
    out.sort(key=lambda r: (r.get("since_utc") or 0, r.get("system_code") or "",
                            r.get("name") or ""))
    return out


def _glob(pattern: str, text: Optional[str]) -> bool:
    return fnmatch.fnmatchcase((text or "").lower(), pattern.lower())


def _value_is(row: dict, want: str) -> bool:
    try:
        w = float(want)
    except ValueError:
        return shown_value(row).strip().lower() == want.strip().lower()
    v = row.get("value_num")
    return v is not None and abs(float(v) - w) <= 1e-3 * max(1.0, abs(w))


def mark_expected(rows: list, facts: Optional[dict]) -> list:
    """Set each override's `expected` to the reason when site knowledge says it is held on
    purpose (site_knowledge.expected_overrides, read by coverage.site_facts), else None.
    2026-10-06: HB3's DHW call is held OFF at operator level because that side is no
    longer connected. An entry with a `value` covers that value only: held at anything else,
    the point is an override again."""
    entries = (facts or {}).get("expected_overrides") or []
    for r in rows:
        r["expected"] = None
        for e in entries:
            if not _glob(e["match"], r.get("name")):
                continue
            if e.get("systems") and not any(_glob(s, r.get("system_code")) for s in e["systems"]):
                continue
            if e.get("value") and not _value_is(r, e["value"]):
                continue
            r["expected"] = e["why"]
            break
    return rows


def short(row: dict) -> str:
    """'HB3 HB3 DHW Call = OFF'."""
    name = row.get("display_name") or (row.get("name") or "").split("|")[0].strip() or "?"
    return f"{row.get('system_code') or ''} {name} = {shown_value(row)}".strip()


def is_occupancy_request(row: dict) -> bool:
    return row.get("role") == "OCCUPANCY" and (row.get("object_type") or "") in (
        "multi-state-output", "multi-state-value")


def is_occupancy_button(row: dict, now_utc: int) -> bool:
    """An occupancy request held occupied (1) at an override level for no longer than a
    button press lasts: after-hours use, not an override. Its start must be known."""
    since = row.get("since_utc")
    return (is_occupancy_request(row) and row.get("value_num") == 1
            and bool(row.get("since_exact")) and since is not None
            and int(now_utc) - int(since) <= OCCUPANCY_BUTTON_MAX_S)


def session_text(s: dict) -> str:
    """'AC-12 Tue 10-13 18:47-20:51 (2.1 h)', with why it counts when that is not plain."""
    a = time.localtime(int(s["start"]))
    when = time.strftime("%a %m-%d %H:%M", a)
    if s.get("end") is None:
        span = f"{when}, still on"
    else:
        e = time.localtime(int(s["end"]))
        end = time.strftime("%H:%M" if e[:3] == a[:3] else "%a %H:%M", e)
        mins = (int(s["end"]) - int(s["start"])) / 60
        span = f"{when}-{end} ({mins:.0f} min)" if mins < 90 else f"{when}-{end} ({mins / 60:.1f} h)"
    why = " (ran past its schedule)" if s.get("ran_past_schedule") else ""
    return f"{s.get('system_code') or '?'} {span}{why}"


def no_such_property(err: Any) -> bool:
    """The device's answer "this object has no priority array" (not commandable), as
    opposed to a read that failed and may work next time."""
    return str(getattr(err, "errorCode", "") or "") == "unknown-property"


def shown_value(row: dict) -> str:
    """A commanded value as the operator would say it: ON/OFF for a binary point."""
    num, text = row.get("value_num"), row.get("value_text")
    if (row.get("object_type") or "").startswith("binary") and num is not None:
        return "ON" if num else "OFF"
    if num is None:
        return text or "?"
    unit = {"degrees-fahrenheit": " °F", "percent": " %", "inches-of-water": " in. w.c.",
            "percent-relative-humidity": " %RH"}.get(row.get("units_text") or "", "")
    return f"{num:g}{unit}"


def describe(row: dict) -> str:
    """'AC-6 Disable Mech Cooling = ON (manual operator, since Tue 10-06 14:03)'."""
    name = row.get("display_name") or (row.get("name") or "").split("|")[0].strip() or "?"
    unit = f"{row['system_code']} " if row.get("system_code") else ""
    since = row.get("since_utc")
    when = time.strftime("%a %m-%d %H:%M", time.localtime(int(since))) if since else "?"
    since_txt = f"since {when}" if row.get("since_exact") else f"since at least {when}"
    return f"{unit}{name} = {shown_value(row)} ({level_name(row.get('level'))}, {since_txt})"
