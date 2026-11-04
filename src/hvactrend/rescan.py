"""Rescan one controller from the running server: the Point Selection page's button.

After a programming change (a new point in a Siemens panel's PPCL, a unit added to a
Tracer SC), the controller's object list is read again on the server's OWN BACnet stack -- the one
the collector polls with -- so nothing has to be stopped. The result is merged into the
inventory by these rules:

  * a NEW point gets its unit from the `equipment` map (commissioning.py), or none;
  * a point already known KEEPS its unit, always. Units are sometimes split by hand (a
    water plant's towers and pumps on one panel, say), and a rescan that re-derived them
    would undo that without a word. A known point with NO unit is placed
    only by a rule that names it (point_systems, sc_dac_map), never by a whole-controller
    default (device_systems), so a rescan cannot sweep a panel's diagnostics into a unit;
  * names, descriptions and units are refreshed. Logging choices, notes and history are not
    touched, and nothing is ever deleted: points the controller no longer has are LISTED,
    and only when its object list was read in full. A short read is not a deletion.

BACnet side: reads and Who-Is only, like everything else (bacnet/readonly.py).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import commissioning
from .bacnet.discovery import (
    DiscoveredDevice,
    DiscoveredObject,
    dac_number,
    enumerate_device_fast,
    to_model_point,
    who_is_sweep,
)
from .models import LOGGABLE_OBJECT_TYPES

logger = logging.getLogger(__name__)

LIST_MAX = 200          # longest list a result carries; the counts are always exact
# What goes on a unit's list. Trend-log objects are loggable (their records can be
# harvested) but no unit lists them anywhere in a real inventory, and the first live rescan
# of a real plant panel put 54 of them on its units. They are stored,
# counted with programs and files, and placed nowhere.
POINT_TYPES = LOGGABLE_OBJECT_TYPES - {"trend-log"}


class RescanError(Exception):
    """A rescan that could not be done, in words for the page."""


def _equipment(config: Any) -> dict:
    node = config.get("equipment") if config is not None else None
    return node.as_dict() if hasattr(node, "as_dict") else dict(node or {})


def plan(existing: list[dict], objects: list[DiscoveredObject], *, device_instance: int,
         equipment: dict, complete: bool) -> dict:
    """What a rescan does, decided without touching anything.

    `existing`: this controller's points as {id, object_type, object_instance, name, system,
    enabled}. `objects`: what the controller has now. `complete`: whether its object list
    was read in full (discovery._read_object_list)."""
    known = {(r["object_type"], int(r["object_instance"])): r for r in existing}
    explicit = dict(equipment or {}, device_systems={})    # rules that name points
    seen: set = set()
    new: list[dict] = []
    other_new = 0
    renamed: list[dict] = []
    placed: list[dict] = []
    for o in objects:
        key = (o.object_type, int(o.object_instance))
        if key in seen:
            continue
        seen.add(key)
        point = o.object_type in POINT_TYPES
        r = known.get(key)
        if r is None:
            label = kind = None
            if point:
                label, kind = commissioning.system_for(device_instance, o.name or "",
                                                       equipment, dac_number(o.name))
            else:
                other_new += 1      # trend logs, programs, files, notification classes
            new.append({"object": o, "name": o.name, "object_type": o.object_type,
                        "object_instance": int(o.object_instance), "point": point,
                        "system": label, "kind": kind})
            continue
        if o.name and r.get("name") and o.name != r["name"]:
            renamed.append({"id": r["id"], "old": r["name"], "new": o.name,
                            "system": r.get("system"), "logged": bool(r.get("enabled"))})
        if r.get("system") is None and point:
            label, kind = commissioning.system_for(device_instance, o.name or r.get("name") or "",
                                                   explicit, dac_number(o.name or r.get("name")))
            if label:
                placed.append({"id": r["id"], "name": o.name or r.get("name"),
                               "system": label, "kind": kind})
    gone = []
    if complete:
        gone = [{"id": r["id"], "name": r.get("name"), "object_type": k[0],
                 "object_instance": k[1], "system": r.get("system"),
                 "logged": bool(r.get("enabled"))}
                for k, r in known.items() if k not in seen and k[0] != "device"]
    return {"new": new, "other_new": other_new, "renamed": renamed, "placed": placed,
            "gone": gone, "known": len(known), "complete": bool(complete)}


# ── the database side: runs in a worker thread, on its own connection ────────────────────
def _device_row(db_path: str, device_instance: int) -> Optional[dict]:
    from .storage.sqlite_dal import SQLiteDAL
    dal = SQLiteDAL(db_path, read_only=True)
    try:
        row = dal._conn.execute(
            "SELECT id, site_id, device_instance, name, address, network_number "
            "FROM devices WHERE device_instance = ? ORDER BY id LIMIT 1",
            (int(device_instance),)).fetchone()
        return dict(row) if row else None
    finally:
        dal.close()


def _existing(db_path: str, device_id: int) -> list[dict]:
    from .storage.sqlite_dal import SQLiteDAL
    dal = SQLiteDAL(db_path, read_only=True)
    try:
        return [dict(r) for r in dal._conn.execute(
            "SELECT p.id, p.object_type, p.object_instance, p.name, p.enabled, "
            "       s.code AS system "
            "FROM points p LEFT JOIN systems s ON s.id = p.system_id "
            "WHERE p.device_id = ?", (int(device_id),))]
    finally:
        dal.close()


def _apply(db_path: str, schema_path: Optional[str], row: dict, dd: DiscoveredDevice,
           objects: list[DiscoveredObject], p: dict, config: Any) -> Optional[dict]:
    """Write the plan in one transaction; then re-classify roles when anything changed.
    Returns the rebuild's summary, or None when there was nothing to re-classify."""
    from .storage.sqlite_dal import SQLiteDAL
    dal = SQLiteDAL(db_path, schema_path)
    c = dal._conn
    try:
        c.execute("BEGIN IMMEDIATE")
        try:
            sys_ids = {code: sid for sid, code in c.execute("SELECT id, code FROM systems")}

            def unit(label: Optional[str], kind: Optional[str]) -> Optional[int]:
                if not label:
                    return None
                if label not in sys_ids:         # a unit the map names for the first time
                    b = _equipment(config).get("building") or {}
                    hit = c.execute("SELECT id FROM buildings WHERE code = ? ORDER BY id",
                                    (str(b.get("code", "MAIN")),)).fetchone() \
                        or c.execute("SELECT id FROM buildings ORDER BY id").fetchone()
                    bid = int(hit[0]) if hit else dal.ensure_building(
                        int(row["site_id"]), str(b.get("code", "MAIN")),
                        str(b.get("name", "Main building")))
                    sys_ids[label] = dal.ensure_system(bid, label, label, kind)
                return sys_ids[label]

            dev_id = int(row["id"])
            for n in p["new"]:
                dal.upsert_point(to_model_point(n["object"], device_id=dev_id,
                                                system_id=unit(n["system"], n["kind"])))
            new_keys = {(n["object_type"], n["object_instance"]) for n in p["new"]}
            for o in objects:                    # refresh names; the unit is COALESCEd, kept
                if (o.object_type, int(o.object_instance)) not in new_keys:
                    dal.upsert_point(to_model_point(o, device_id=dev_id, system_id=None))
            for pl in p["placed"]:
                c.execute("UPDATE points SET system_id = ? WHERE id = ? AND system_id IS NULL",
                          (unit(pl["system"], pl["kind"]), pl["id"]))
            # NOT upsert_device: it rewrites the router flags and identity with whatever this
            # scan read, blanks included. Only what a rescan can vouch for.
            c.execute("UPDATE devices SET address = ?, "
                      " network_number = COALESCE(?, network_number), "
                      " name = COALESCE(?, name), vendor_name = COALESCE(?, vendor_name), "
                      " model_name = COALESCE(?, model_name), firmware = COALESCE(?, firmware), "
                      " last_seen_utc = ? WHERE id = ?",
                      (dd.address, dd.network_number, dd.object_name, dd.vendor_name,
                       dd.model_name, dd.firmware, int(time.time()), dev_id))
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        if p["new"] or p["renamed"] or p["placed"]:
            from .ai import model
            return model.rebuild(dal, config)
        return None
    finally:
        dal.close()


def _brief(items: list[dict], keys: tuple) -> list[dict]:
    return [{k: i.get(k) for k in keys} for i in items[:LIST_MAX]]


async def rescan_device(bacnet: Any, db_path: str, schema_path: Optional[str], config: Any,
                        device_instance: int, *,
                        on_progress: Optional[Callable[[int, int], None]] = None,
                        who_is_timeout: float = 3.0) -> dict:
    """Read one controller again and merge it (module docstring). Raises RescanError."""
    t0 = time.monotonic()
    inst = int(device_instance)
    row = await asyncio.to_thread(_device_row, db_path, inst)
    if row is None:
        raise RescanError(f"controller {inst} is not in the inventory yet; "
                          "find it with discovery first")
    dd = DiscoveredDevice(device_instance=inst, address=row["address"],
                          network_number=row.get("network_number"))
    stats: dict = {}
    objects: list[DiscoveredObject] = []
    if row.get("address"):
        objects = await enumerate_device_fast(bacnet, dd, stats=stats, on_progress=on_progress)
    if not objects:
        # Nothing at the address on record: ask where it is now (an address can change).
        found = await who_is_sweep(bacnet, low_limit=inst, high_limit=inst,
                                   timeout=who_is_timeout)
        hit = next((d for d in found if d.device_instance == inst), None)
        if hit is None:
            raise RescanError(f"controller {inst} did not answer at {row.get('address')}, "
                              "nor to a Who-Is")
        dd, stats = hit, {}
        objects = await enumerate_device_fast(bacnet, dd, stats=stats, on_progress=on_progress)
        if not objects:
            raise RescanError(f"controller {inst} answered a Who-Is at {dd.address}, "
                              "but its object list could not be read")

    existing = await asyncio.to_thread(_existing, db_path, int(row["id"]))
    p = plan(existing, objects, device_instance=inst, equipment=_equipment(config),
             complete=bool(stats.get("complete")))
    rebuilt = await asyncio.to_thread(_apply, db_path, schema_path, row, dd, objects, p, config)

    offered = [n for n in p["new"] if n["point"]]
    roles = (rebuilt or {}).get("roles") or {}
    return {
        "device": inst, "name": dd.object_name or row.get("name"), "address": dd.address,
        "address_changed": dd.address != row.get("address"),
        "objects": len(objects), "declared": stats.get("declared"),
        "complete": p["complete"], "known_before": p["known"],
        "new_count": len(offered), "other_new": p["other_new"],
        "new": _brief(offered, ("name", "object_type", "object_instance", "system")),
        "renamed_count": len(p["renamed"]),
        "renamed": _brief(p["renamed"], ("old", "new", "system", "logged")),
        "placed_count": len(p["placed"]),
        "placed": _brief(p["placed"], ("name", "system")),
        "gone_count": len(p["gone"]),
        "gone_logged": sum(1 for g in p["gone"] if g["logged"]),
        "gone": _brief(sorted(p["gone"], key=lambda g: (not g["logged"], g["name"] or "")),
                       ("name", "object_type", "object_instance", "system", "logged")),
        "roles_classified": roles.get("classified"),
        "seconds": round(time.monotonic() - t0, 1),
    }


def controllers(db_path: str, config: Any) -> list[dict]:
    """The controllers a rescan is offered for: every one with a point on a unit, plus every
    one the equipment map names. Runs in a worker thread (a join over every point)."""
    from .storage.sqlite_dal import SQLiteDAL
    eq = _equipment(config)
    named = set(commissioning.targets(eq))
    dal = SQLiteDAL(db_path, read_only=True)
    try:
        rows = [dict(r) for r in dal._conn.execute(
            "SELECT d.device_instance, d.name, d.address, d.model_name, d.last_seen_utc, "
            "       COUNT(p.id) AS points, "
            "       GROUP_CONCAT(DISTINCT s.code) AS units "
            "FROM devices d LEFT JOIN points p ON p.device_id = d.id "
            "LEFT JOIN systems s ON s.id = p.system_id "
            "GROUP BY d.id")]
    finally:
        dal.close()
    out = []
    for r in rows:
        units = sorted(set((r.pop("units") or "").split(",")) - {""}, key=_natural)
        if units or r["device_instance"] in named:
            out.append(dict(r, units=units))
    return sorted(out, key=lambda r: (_natural(r["units"][0]) if r["units"] else (9, ""),
                                      r["device_instance"]))


def _natural(s: str) -> tuple:
    """AC-2 before AC-10."""
    m = re.match(r"^(.*?)(\d+)$", s or "")
    return (0, m.group(1), int(m.group(2))) if m else (0, s or "", -1)


def default_schema(config: Any) -> Optional[str]:
    st = config.get("storage") if config is not None else None
    sf = st.get("schema_file") if st is not None and hasattr(st, "get") else None
    return str(Path(sf)) if sf else None
