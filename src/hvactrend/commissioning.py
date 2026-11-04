"""Which unit (system) each discovered point belongs to, from the `equipment` map in the config.

Discovery finds devices and their objects; it cannot know that a controller runs "AHU-3" or
that half of a plant controller's points are the cooling tower and half the pumps. The
`equipment` section says so, and scripts/commission.py and the Point Selection page's
rescan (rescan.py) apply it. Three ways, first match wins for each point:

    equipment:
      point_systems:                 # by object-name glob, within one device
        - {device: 2001, match: "*CT1*", system: "CT-1", kind: plant}
        - {device: 2001, match: "*CWP*", system: "CWP", kind: plant}
      device_systems:                # a whole controller is one unit
        1001: "AHU-1"                # kind inferred from the name
        1002: {system: "RTU-2", kind: ahu}
      sc_dac_map:                    # Trane Tracer SC: points carry a |dac-N suffix
        3001: {1: "AHU-3", 2: "AHU-4"}

`kind` decides which alarm rules apply: `ahu` / `hv` (air handlers: discharge air, duct
static, ...), `plant` (boilers, towers, pumps), anything else = no unit rules. When it is not
given it is inferred from the name (AHU-1, RTU-2, AC-3 -> ahu; BLR-1, CT-1, CHWP -> plant).
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any, Optional

AIR = re.compile(r"^(AHU|AC|RTU|MAU|DOAS|ERV|HRU|FCU|UV)\b|^(AHU|AC|RTU|MAU)-?\d", re.I)
HV = re.compile(r"^HV", re.I)
PLANT = re.compile(r"^(BLR|B|CT|CWP|CHWP|HWP|PCHWP|SCHWP|CH|CHLR|HX|P|HB)-?\d*$|boiler|chiller|"
                   r"tower|pump|plant", re.I)


def infer_kind(label: str) -> Optional[str]:
    """ahu | hv | plant from a unit's name, or None when the name says nothing."""
    s = (label or "").strip()
    if HV.match(s):
        return "hv"
    if AIR.match(s):
        return "ahu"
    if PLANT.search(s):
        return "plant"
    return None


def _entry(v: Any) -> tuple[Optional[str], Optional[str]]:
    """A map value: "AHU-1", or {system: "AHU-1", kind: "ahu"} (name: also accepted)."""
    if isinstance(v, dict):
        label = v.get("system") or v.get("name")
        return (str(label) if label else None), (v.get("kind") or None)
    return (str(v) if v not in (None, "") else None), None


def system_for(device_instance: int, object_name: str, equipment: dict,
               dac_number: Optional[int] = None) -> tuple[Optional[str], Optional[str]]:
    """(unit label, kind) for one discovered object, or (None, None) when the map does not
    place it. `dac_number` is the |dac-N suffix of a Trane SC point, when it has one."""
    eq = equipment or {}
    inst = int(device_instance)
    for rule in eq.get("point_systems") or []:
        if not isinstance(rule, dict) or int(rule.get("device", -1)) != inst:
            continue
        if fnmatch.fnmatchcase((object_name or "").lower(), str(rule.get("match", "")).lower()):
            label, kind = _entry(rule)
            if label:
                return label, kind or infer_kind(label)
    devs = {int(k): v for k, v in (eq.get("device_systems") or {}).items()}
    if inst in devs:
        label, kind = _entry(devs[inst])
        if label:
            return label, kind or infer_kind(label)
    scs = {int(k): v for k, v in (eq.get("sc_dac_map") or {}).items()}
    if inst in scs and dac_number is not None:
        dacs = {int(k): v for k, v in (scs[inst] or {}).items()}
        label, kind = _entry(dacs.get(int(dac_number)))
        if label:
            return label, kind or infer_kind(label) or "ahu"   # an SC's DACs are air handlers
    return None, None


def all_systems(equipment: dict) -> dict[str, Optional[str]]:
    """Every unit the map names -> its kind (explicit, else inferred)."""
    eq = equipment or {}
    out: dict[str, Optional[str]] = {}

    def add(label, kind, default=None):
        if label and label not in out:
            out[label] = kind or infer_kind(label) or default
    for rule in eq.get("point_systems") or []:
        if isinstance(rule, dict):
            add(*_entry(rule))
    for v in (eq.get("device_systems") or {}).values():
        add(*_entry(v))
    for dacs in (eq.get("sc_dac_map") or {}).values():
        for v in (dacs or {}).values():
            label, kind = _entry(v)
            add(label, kind, default="ahu")
    return out


def targets(equipment: dict) -> list[int]:
    """The controllers to enumerate: `targets` when given, else every device the map names."""
    eq = equipment or {}
    if eq.get("targets"):
        return [int(t) for t in eq["targets"]]
    found: list[int] = []
    for rule in eq.get("point_systems") or []:
        if isinstance(rule, dict) and rule.get("device") is not None:
            found.append(int(rule["device"]))
    found += [int(k) for k in (eq.get("device_systems") or {})]
    found += [int(k) for k in (eq.get("sc_dac_map") or {})]
    return list(dict.fromkeys(found))
