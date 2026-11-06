"""SQLite implementation of the DAL.

Uses stdlib sqlite3 only. WAL + foreign_keys come from schema.sql. This is the
default engine; the abstract DAL keeps the door open for TimescaleDB/DuckDB.

Schema apply, sample writes, latest-value, raw/rollup range query, and the
topology upserts (used by Phase 1 discovery caching) are implemented. Rollup
materialization (build_rollups) and retention pruning (enforce_retention) are
implemented and driven periodically from the collector's maintenance loop.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

from ..models import (
    LOGGABLE_OBJECT_TYPES,
    Alert,
    Device,
    Finding,
    Point,
    PointSource,
    PollHealth,
    Quality,
    Report,
    Sample,
)
from .dal import DAL

# Resolve db/schema.sql relative to the repo root (…/HVACTRENDS/db/schema.sql).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_SCHEMA = _REPO_ROOT / "db" / "schema.sql"


class SQLiteDAL(DAL):
    def __init__(self, db_path: str | Path,
                 schema_path: str | Path | None = None, *, read_only: bool = False) -> None:
        self.db_path = Path(db_path)
        self.schema_path = Path(schema_path) if schema_path else _DEFAULT_SCHEMA
        if read_only:
            # For readers that must never write: the rules replay against the live database.
            # mode=ro makes SQLite itself refuse any write, rather than trusting the caller.
            self._conn = sqlite3.connect(f"file:{self.db_path.resolve().as_posix()}?mode=ro",
                                         uri=True, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON;")

    # ── lifecycle / schema ───────────────────────────────────────────────────
    def apply_schema(self) -> None:
        sql = Path(self.schema_path).read_text(encoding="utf-8")
        self._conn.executescript(sql)
        self._ensure_columns()

    def _ensure_columns(self) -> None:
        """Additive migrations for existing DBs. schema.sql uses CREATE TABLE IF NOT
        EXISTS, so new TABLES appear automatically on re-apply, but a new COLUMN on an
        existing table needs an ALTER. Idempotent (checks PRAGMA table_info first) so
        it's safe to run every startup. Never drops/rewrites data."""
        def _add_missing(table: str, additions: list[tuple[str, str]]) -> list[str]:
            cols = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            return [col for col, ddl in additions
                    if col not in cols and (self._conn.execute(ddl) or True)]

        for _roll in ("samples_5m", "samples_1h"):
            _add_missing(_roll, [
                # See the schema comment: distinguishes "faulted all bucket" from "no data".
                ("n_total", f"ALTER TABLE {_roll} ADD COLUMN n_total INTEGER"),
            ])
        added = _add_missing("points", [
            ("role", "ALTER TABLE points ADD COLUMN role TEXT"),
            ("role_canonical",
             "ALTER TABLE points ADD COLUMN role_canonical INTEGER NOT NULL DEFAULT 0"),
            # Operator's own note on a point ("this is the tower's ACTUAL supply temp,
            # not the setpoint"). Deliberately separate from `description`, which holds
            # the BACnet-published text and is overwritten on every re-discovery — an
            # operator note written there would be silently destroyed by the next scan.
            ("operator_note", "ALTER TABLE points ADD COLUMN operator_note TEXT"),
        ])
        added += _add_missing("reports", [
            # Age of the inputs the report was computed FROM, recorded at generation.
            # It cannot be reconstructed later: once the nightly rebuild runs, the old
            # baseline.updated_utc is gone and any after-the-fact check measures today's
            # state instead. Reports have been generated against 49-HOUR-STALE baselines
            # with nothing on the page saying so — every sigma in them was measured
            # against the wrong normals.
            ("inputs_json", "ALTER TABLE reports ADD COLUMN inputs_json TEXT"),
            # The model call itself: stop_reason, token usage, request id, effort. 6 of 31
            # "complete" reports once had no narrative and nothing recorded why (ROADMAP
            # Phase 3); an error report keeps whatever the failed call returned.
            ("call_json", "ALTER TABLE reports ADD COLUMN call_json TEXT"),
        ])
        added += _add_missing("findings", [
            ("timeframe", "ALTER TABLE findings ADD COLUMN timeframe TEXT"),
            # Why the operator acknowledged it ("yes, I raised min OA to 35% to test
            # IAQ"). Without this, acknowledging is a MUTE BUTTON: the finding just
            # drops out of open_findings() and the model is told nothing, so it can
            # rediscover the same condition cold on the next run. With it, the finding
            # is rendered as EXPLAINED instead of vanishing.
            ("operator_note", "ALTER TABLE findings ADD COLUMN operator_note TEXT"),
            ("ack_utc", "ALTER TABLE findings ADD COLUMN ack_utc INTEGER"),
        ])
        added += _add_missing("systems", [
            # operator-set occupancy mode: 'scheduled' (default building schedule),
            # 'vacant' (unoccupied — equipment idle is EXPECTED), 'always' (24/7 occupied).
            ("occupancy_mode", "ALTER TABLE systems ADD COLUMN occupancy_mode TEXT"),
            # per-unit occupancy hours (JSON day-map, minutes from local midnight):
            # {"mon":[300,1080],...,"sat":null}. NULL column = inherit the building
            # default. Captures staggered start times + per-unit weekend hours.
            ("occ_schedule", "ALTER TABLE systems ADD COLUMN occ_schedule TEXT"),
            # operator-written sequence of operations (free prose). The single most
            # valuable piece of equipment context: it lets the AI judge observed behavior
            # against DESIGNED intent instead of inferring intent from the data.
            ("sequence_of_operations",
             "ALTER TABLE systems ADD COLUMN sequence_of_operations TEXT"),
        ])
        added += _add_missing("alerts", [
            # which evaluator owns the alert; each only auto-resolves its own source
            ("source",
             "ALTER TABLE alerts ADD COLUMN source TEXT NOT NULL DEFAULT 'prescreen'"),
        ])
        added += _add_missing("incidents", [
            # the tier an incident was pushed at, so one that gets WORSE (action -> critical)
            # is pushed again as urgent (2026-10-02, when ACTION pushes were switched on)
            ("notified_tier", "ALTER TABLE incidents ADD COLUMN notified_tier TEXT"),
        ])
        if added:
            self._conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_utc, note) "
                "VALUES (?,?,?)",
                (1, int(time.time()),
                 "add points.role/role_canonical, findings.timeframe, systems.occupancy_mode"))

    def close(self) -> None:
        self._conn.close()

    # ── topology (discovery caches the object dictionary here, plan §9.1) ─────
    def ensure_site(self, code: str, name: str,
                    tz: str = "America/New_York") -> int:
        """Insert-or-update a site by code; return its id."""
        row = self._conn.execute(
            "INSERT INTO sites (code, name, tz) VALUES (?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET name=excluded.name, tz=excluded.tz "
            "RETURNING id",
            (code, name, tz),
        ).fetchone()
        return int(row["id"])

    def upsert_device(self, device: Device) -> int:
        """Insert/update a device keyed on (site_id, device_instance); return its id.

        On re-discovery, refreshes discovery-derived fields + last_seen_utc but
        keeps created_utc.

        COALESCE, as upsert_point does: a re-discovery whose identity read failed passed
        None for the name, vendor and model, and discovery never knows the router fields at
        all, so one re-scan blanked a controller's name and reset its router flag
        (2026-11-06 review). The address and its network number are what a Who-Is answers,
        so those two are taken as they come.
        """
        now = int(time.time())
        row = self._conn.execute(
            "INSERT INTO devices (site_id, device_instance, name, address, "
            " network_number, mac_address, router_instance, vendor_id, vendor_name, "
            " model_name, firmware, is_router, rpm_supported, poll_budget_per_min, "
            " segmentation, last_seen_utc, created_utc, notes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(site_id, device_instance) DO UPDATE SET "
            " name=COALESCE(excluded.name, devices.name), address=excluded.address, "
            " network_number=excluded.network_number, "
            " mac_address=COALESCE(excluded.mac_address, devices.mac_address), "
            " router_instance=COALESCE(excluded.router_instance, devices.router_instance), "
            " vendor_id=COALESCE(excluded.vendor_id, devices.vendor_id), "
            " vendor_name=COALESCE(excluded.vendor_name, devices.vendor_name), "
            " model_name=COALESCE(excluded.model_name, devices.model_name), "
            " firmware=COALESCE(excluded.firmware, devices.firmware), "
            " is_router=MAX(excluded.is_router, devices.is_router), "
            " rpm_supported=COALESCE(excluded.rpm_supported, devices.rpm_supported), "
            " segmentation=COALESCE(excluded.segmentation, devices.segmentation), "
            " last_seen_utc=excluded.last_seen_utc "
            "RETURNING id",
            (device.site_id, device.device_instance, device.name, device.address,
             device.network_number, device.mac_address, device.router_instance,
             device.vendor_id, device.vendor_name, device.model_name, device.firmware,
             int(device.is_router), _bool_or_none(device.rpm_supported),
             device.poll_budget_per_min, device.segmentation,
             device.last_seen_utc if device.last_seen_utc is not None else now, now,
             device.notes),
        ).fetchone()
        return int(row["id"])

    def ensure_building(self, site_id: int, code: str, name: str) -> int:
        """Insert-or-update a building by (site_id, code); return its id."""
        row = self._conn.execute(
            "INSERT INTO buildings (site_id, code, name) VALUES (?,?,?) "
            "ON CONFLICT(site_id, code) DO UPDATE SET name=excluded.name "
            "RETURNING id",
            (site_id, code, name),
        ).fetchone()
        return int(row["id"])

    def ensure_system(self, building_id: int, code: str, name: str,
                      kind: Optional[str] = None) -> int:
        """Insert-or-update an equipment system (e.g. an AHU) by (building_id, code)."""
        row = self._conn.execute(
            "INSERT INTO systems (building_id, code, name, kind) VALUES (?,?,?,?) "
            "ON CONFLICT(building_id, code) DO UPDATE SET "
            " name=excluded.name, kind=excluded.kind "
            "RETURNING id",
            (building_id, code, name, kind),
        ).fetchone()
        return int(row["id"])

    def device_address(self, device_instance: int) -> Optional[dict]:
        """Cached (id, address, network_number) for a device instance, or None."""
        row = self._conn.execute(
            "SELECT id, address, network_number FROM devices "
            "WHERE device_instance = ? ORDER BY id LIMIT 1",
            (device_instance,),
        ).fetchone()
        return dict(row) if row else None

    def upsert_point(self, point: Point) -> int:
        """Insert/update a point keyed on (device_id, object_type, object_instance).

        On re-discovery, refreshes discovery-derived fields (name/description/units/
        trendlog_harvest) but PRESERVES operator selections: enabled, poll_interval_s,
        display_name, cov_enabled, and the alarm limits (plan §9.1 — discovery
        doesn't un-select a logged point).

        `system_id` is COMMISSIONED, not discovered: scripts/commission_ahus.py derives
        it from equipment.sc_dac_map, while the generic discovery path
        (discovery_service.run_discovery -> to_model_point) has no idea which AHU an
        object belongs to and always passes None. A plain `system_id=excluded.system_id`
        therefore let one re-scan NULL the AHU mapping on every commissioned point —
        silently, because the points keep `enabled=1` and keep logging, so the collector
        and the raw trend viewer look fine while every per-unit dashboard empties, the
        fleet matrix goes blank, per-unit occupancy schedules stop resolving and
        canonical_ids collapses all 14 units into one. COALESCE keeps an explicit
        assignment working while making it impossible for a discovery NULL to erase one.
        """
        now = int(time.time())
        source = point.source.value if isinstance(point.source, PointSource) else str(point.source)
        row = self._conn.execute(
            "INSERT INTO points (device_id, system_id, source, object_type, "
            " object_instance, name, display_name, description, units, units_text, "
            " poll_interval_s, cov_enabled, trendlog_harvest, enabled, alarm_enabled, "
            " alarm_hi, alarm_lo, alarm_deadband, spec, created_utc) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(device_id, object_type, object_instance) DO UPDATE SET "
            " name=COALESCE(excluded.name, points.name), "
            # COALESCE, not straight assignment. A re-scan whose property reads time
            # out returns None for EVERY property (_read_props swallows the error, so
            # obj.error stays unset and the row is upserted anyway), which erased units
            # and description on points that were being logged. units_text drives the
            # viewer's shared axis, the delta-line unit check and the fleet band, so the
            # damage shows up as charts quietly changing shape. 2026-09-22 audit, M-5.
            # Trade-off accepted: a genuine removal at the BAS no longer propagates as NULL.
            " description=COALESCE(excluded.description, points.description), "
            " units=COALESCE(excluded.units, points.units), "
            " units_text=COALESCE(excluded.units_text, points.units_text), "
            " system_id=COALESCE(excluded.system_id, points.system_id), "
            " trendlog_harvest=excluded.trendlog_harvest "
            "RETURNING id",
            (point.device_id, point.system_id, source, point.object_type,
             point.object_instance, point.name, point.display_name, point.description,
             point.units, point.units_text, point.poll_interval_s,
             int(point.cov_enabled), int(point.trendlog_harvest), int(point.enabled),
             int(point.alarm_enabled), point.alarm_hi, point.alarm_lo,
             point.alarm_deadband, point.spec, now),
        ).fetchone()
        return int(row["id"])

    # ── browser queries ───────────────────────
    def list_systems(self) -> list[dict]:
        """Equipment systems (AHUs, HV1) with point + enabled counts, operator notes,
        and occupancy mode (for the units/occupancy editor + AI context)."""
        rows = self._conn.execute(
            "SELECT s.id, s.code, s.name, s.kind, s.notes, s.occupancy_mode, s.occ_schedule, "
            " s.sequence_of_operations, "
            " COUNT(p.id) AS point_count, "
            " COALESCE(SUM(CASE WHEN p.enabled=1 THEN 1 ELSE 0 END), 0) AS enabled_count "
            "FROM systems s LEFT JOIN points p ON p.system_id = s.id "
            "GROUP BY s.id ORDER BY s.id"
        ).fetchall()
        return [dict(r) for r in rows]

    def set_equipment_meta(self, system_id: int, key: str, *,
                           value_num: Optional[float] = None,
                           value_text: Optional[str] = None,
                           unit: Optional[str] = None,
                           source: str = "operator") -> None:
        """Upsert ONE equipment spec for a system (keyed by system+key), so specs stay
        fully editable — re-saving a key overwrites it, and set_equipment_meta with both
        values None deletes it. equipment_meta has no unique constraint, so this deletes
        then inserts rather than relying on ON CONFLICT."""
        self._conn.execute("DELETE FROM equipment_meta WHERE system_id=? AND key=?",
                           (system_id, key))
        if value_num is None and (value_text is None or value_text == ""):
            return                      # cleared
        self._conn.execute(
            "INSERT INTO equipment_meta (system_id, key, value_num, value_text, unit, source) "
            "VALUES (?,?,?,?,?,?)",
            (system_id, key, value_num, value_text or None, unit, source))

    def equipment_by_system(self) -> dict[Any, dict[str, dict]]:
        """{system_id: {key: {value, unit}}} for every system — one round-trip for the
        editor UI and the per-unit context rendering."""
        out: dict[Any, dict[str, dict]] = {}
        for r in self._conn.execute(
                "SELECT system_id, key, value_num, value_text, unit FROM equipment_meta "
                "WHERE system_id IS NOT NULL").fetchall():
            val = r["value_num"] if r["value_num"] is not None else r["value_text"]
            out.setdefault(r["system_id"], {})[r["key"]] = {"value": val, "unit": r["unit"]}
        return out

    def set_system_context(self, system_id: int, *, notes: Optional[str] = None,
                           occupancy_mode: Optional[str] = None,
                           occ_schedule: Optional[str] = None,
                           sequence_of_operations: Optional[str] = None) -> bool:
        """Set the operator context on a system: free-text notes, occupancy mode (fed to
        the AI so it treats a vacant unit's idle equipment as expected), and/or the
        per-unit occupancy day-map JSON (staggered starts / weekend hours; empty string
        clears it back to the building default). Only provided fields are updated.
        Returns True if the system exists."""
        sets, params = [], []
        if notes is not None:
            sets.append("notes = ?"); params.append(notes or None)
        if occupancy_mode is not None:
            sets.append("occupancy_mode = ?"); params.append(occupancy_mode or None)
        if occ_schedule is not None:
            sets.append("occ_schedule = ?"); params.append(occ_schedule or None)
        if sequence_of_operations is not None:
            sets.append("sequence_of_operations = ?")
            params.append(sequence_of_operations or None)
        if not sets:
            return self._conn.execute(
                "SELECT 1 FROM systems WHERE id = ?", (system_id,)).fetchone() is not None
        params.append(system_id)
        cur = self._conn.execute(
            f"UPDATE systems SET {', '.join(sets)} WHERE id = ?", params)
        return cur.rowcount > 0

    def points_for_system(self, system_id: int, *, q: Optional[str] = None,
                          loggable_only: bool = False, limit: int = 5000) -> list[dict]:
        """Points under a system, optional name search / loggable-type filter."""
        sql = ["SELECT p.id, p.object_type, p.object_instance, p.name, p.description, "
               " p.units, p.units_text, p.enabled, p.device_id "
               "FROM points p WHERE p.system_id = ?"]
        params: list[Any] = [system_id]
        if q:
            sql.append("AND lower(p.name) LIKE ?")
            params.append(f"%{q.lower()}%")
        if loggable_only:
            ph = ",".join("?" * len(LOGGABLE_OBJECT_TYPES))
            sql.append(f"AND p.object_type IN ({ph})")
            params.extend(sorted(LOGGABLE_OBJECT_TYPES))
        sql.append("ORDER BY p.object_type, p.object_instance LIMIT ?")
        params.append(limit)
        out = []
        for r in self._conn.execute(" ".join(sql), params).fetchall():
            d = dict(r)
            d["loggable"] = d["object_type"] in LOGGABLE_OBJECT_TYPES
            d["enabled"] = bool(d["enabled"])
            out.append(d)
        return out

    def points_read_targets(self, ids: Sequence[int]) -> list[dict]:
        """Address + object id for a set of points, for a live ReadProperty/RPM."""
        if not ids:
            return []
        ph = ",".join("?" * len(ids))
        rows = self._conn.execute(
            f"SELECT p.id, p.object_type, p.object_instance, p.units_text, "
            f" d.address FROM points p JOIN devices d ON p.device_id = d.id "
            f"WHERE p.id IN ({ph})",
            list(ids),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_point_enabled(self, point_id: int, enabled: bool) -> None:
        self._conn.execute("UPDATE points SET enabled = ? WHERE id = ?",
                           (1 if enabled else 0, point_id))

    def set_point_labels(self, point_id: int, *,
                         display_name: Optional[str] = None,
                         operator_note: Optional[str] = None) -> bool:
        """Operator's own label + note for a point. BACnet names like
        `SITE.CHW.HDR.SUP.TEMP` vs `SITE.CHW.HDR.SUP.TEMP.SP` differ by two characters, so
        on a chart the reading and its setpoint are near-indistinguishable — this is how
        the operator says which is which.

        Both fields are independently optional: pass None to leave a field alone, pass ""
        to clear it back to the BACnet name. Neither is ever written by discovery, so a
        re-scan cannot destroy them (`description` would have been — it is overwritten on
        every upsert, which is exactly why the note does not live there).

        Returns False if the point id is unknown."""
        sets, params = [], []
        for col, val in (("display_name", display_name), ("operator_note", operator_note)):
            if val is None:
                continue
            val = val.strip()
            sets.append(f"{col} = ?")
            params.append(val or None)          # "" clears -> NULL -> falls back to `name`
        if not sets:
            return self._conn.execute(
                "SELECT 1 FROM points WHERE id = ?", (point_id,)).fetchone() is not None
        params.append(point_id)
        cur = self._conn.execute(
            f"UPDATE points SET {', '.join(sets)} WHERE id = ?", params)
        return cur.rowcount > 0

    # ── settings (DB-backed operator overrides, plan §11) ────────────────────
    def get_setting(self, key: str) -> Any:
        row = self._conn.execute(
            "SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["value_json"])
        except (ValueError, TypeError):
            return None

    def set_setting(self, key: str, value: Any) -> None:
        self._conn.execute(
            "INSERT INTO settings (key, value_json, updated_utc) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
            "updated_utc=excluded.updated_utc",
            (key, json.dumps(value), int(time.time())))

    def delete_setting(self, key: str) -> None:
        self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))

    def all_settings(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for r in self._conn.execute("SELECT key, value_json FROM settings"):
            try:
                out[r["key"]] = json.loads(r["value_json"])
            except (ValueError, TypeError):
                continue
        return out

    # ── consistent online backup ──────────────────────────────
    def backup_to(self, dest_path: str) -> None:
        """SQLite online-backup to dest_path — a consistent snapshot even while the
        collector holds this WAL-mode connection open and is actively writing."""
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(dest)
        try:
            with target:
                self._conn.backup(target)
        finally:
            target.close()

    def poll_targets(self) -> list[dict]:
        """The collector's work-list: every enabled point + its device address /
        object id, grouped-friendly (ordered by device). BACnet objects only."""
        rows = self._conn.execute(
            "SELECT p.id AS point_id, p.object_type, p.object_instance, "
            " p.poll_interval_s, p.units_text, d.id AS device_id, d.address "
            "FROM points p JOIN devices d ON p.device_id = d.id "
            "WHERE p.enabled = 1 AND p.source = 'bacnet' "
            " AND p.object_type IS NOT NULL AND d.address IS NOT NULL "
            "ORDER BY d.id, p.object_type, p.object_instance"
        ).fetchall()
        return [dict(r) for r in rows]

    def list_points(self, *, enabled_only: bool = True) -> list[Point]:
        q = "SELECT * FROM points"
        if enabled_only:
            q += " WHERE enabled = 1"
        rows = self._conn.execute(q).fetchall()
        return [self._row_to_point(r) for r in rows]

    def role_peers(self, point_id: int) -> list[dict]:
        """The canonical point for THIS point's role on every system (for the viewer's
        'add this reading across all AHUs'). Role-based — cleaner than base-name matching.
        Returns [] if the point has no role."""
        row = self._conn.execute(
            "SELECT role FROM points WHERE id = ?", (point_id,)).fetchone()
        if not row or not row["role"]:
            return []
        rows = self._conn.execute(
            "SELECT p.id, p.name, p.units_text, p.role, p.system_id, s.code AS system_code "
            "FROM points p JOIN systems s ON p.system_id = s.id "
            "WHERE p.role = ? AND p.role_canonical = 1 AND p.enabled = 1 "
            "ORDER BY s.id", (row["role"],)).fetchall()
        return [dict(r) for r in rows]

    def point_meta(self, point_id: int) -> Optional[dict]:
        """Labeling metadata for one point (name/units/type + its system), for the
        trend viewer. Returns None if the point id is unknown."""
        row = self._conn.execute(
            "SELECT p.id, p.name, p.display_name, p.operator_note, p.description, "
            " p.object_type, p.object_instance, p.units, "
            " p.units_text, p.enabled, p.role, p.alarm_enabled, p.alarm_hi, p.alarm_lo, "
            " p.system_id, s.code AS system_code, s.name AS system_name "
            "FROM points p LEFT JOIN systems s ON p.system_id = s.id "
            "WHERE p.id = ?",
            (point_id,),
        ).fetchone()
        return dict(row) if row else None

    _HOURLY_SUMS_REFRESH_S = 86400

    def _hourly_sums_below(self, w: int) -> "dict[int, int]":
        """Per point, SUM(n) over samples_1h below the watermark `w`, kept INCREMENTALLY.

        The whole-table `SUM(n) ... GROUP BY point_id` ran on every viewer load, on the
        loop: 290 ms at 1.6 M hourly rows on 2026-10-27, and samples_1h is kept for years,
        so it only grows. Buckets below the watermark are final (build_rollups re-does only
        the newest), so the sums are carried forward and each call adds just the buckets
        the watermark has passed since, per point on the clustered key. A watermark that
        moves BACK (a rebuild), or a day gone by (retention may have trimmed the hourly
        table), recounts from scratch."""
        now = time.monotonic()
        cache = getattr(self, "_hsums", None)
        if (cache is None or w < cache["w"]
                or now - cache["at"] > self._HOURLY_SUMS_REFRESH_S):
            sums = {r["point_id"]: r["n"] for r in self._conn.execute(
                "SELECT point_id, SUM(n) AS n FROM samples_1h WHERE bucket_utc < ? "
                "GROUP BY point_id", (w,)).fetchall()}
            self._hsums = {"w": w, "at": now, "sums": sums}
            return dict(sums)
        if w > cache["w"]:
            for r in self._conn.execute(
                    "SELECT p.id AS pid, (SELECT SUM(n) FROM samples_1h s "
                    "  WHERE s.point_id = p.id AND s.bucket_utc >= ? AND s.bucket_utc < ?) AS n "
                    "FROM points p", (cache["w"], w)):
                if r["n"]:
                    cache["sums"][r["pid"]] = (cache["sums"].get(r["pid"]) or 0) + r["n"]
            cache["w"] = w
        return dict(cache["sums"])

    def logged_points(self) -> list[dict]:
        """The trend viewer's point-picker source: every enabled (being-trended)
        point with its system + per-point sample count / last-seen ts, so the UI
        can show which points actually have data. Ordered by system then name.

        Counts come from the HOURLY rollup up to its watermark plus an exact raw tail
        after it — NOT from `COUNT(*) ... GROUP BY point_id` over raw, which scanned
        all 10M rows and cost ~10 s on EVERY viewer load. That ran on the event loop
        (the DAL connection is thread-bound, so the endpoint can't be offloaded), so
        it also stalled the collector for those ten seconds.

        `sample_count` counts GOOD NUMERIC samples: build_rollups only aggregates
        `quality='good' AND value_num IS NOT NULL`, and the tail applies the same
        filter so the two halves are one consistent population. That is the
        chartable-data count the picker is asking about — a point reporting only
        faults shows 0 samples but a live `last_ts`. `last_ts` spans ALL qualities:
        it answers "when did this point last report anything", and is exact."""
        w = self._table_watermark("samples_1h")
        w = 0 if w is None else int(w)     # no rollups yet -> count it all from raw
        rolled = self._hourly_sums_below(w)
        rows = self._conn.execute(
            "SELECT p.id, p.name, p.display_name, p.operator_note, p.description, "
            " p.object_type, p.object_instance, p.units_text, p.enabled, "
            " p.role, p.role_canonical, p.system_id, s.code AS system_code, "
            " s.name AS system_name, "
            # Correlated subqueries so both ride the (point_id, ts_utc) PRIMARY KEY.
            # `WHERE ts_utc >= ? GROUP BY point_id` picks idx_samples_ts instead and
            # pays a PK lookup per row — 0.95 s vs 0.02 s on this database.
            " (SELECT COUNT(*) FROM samples t WHERE t.point_id = p.id "
            "   AND t.ts_utc >= ? AND t.quality = 'good' "
            "   AND t.value_num IS NOT NULL) AS tail_n, "
            " (SELECT MAX(t.ts_utc) FROM samples t WHERE t.point_id = p.id) AS last_ts "
            "FROM points p LEFT JOIN systems s ON p.system_id = s.id "
            # ⚠️ NOT `enabled = 1` alone. Unticking a point in Point Selection stops the
            # logging -- which is the intent -- but it also removed the point from this
            # list, and the viewer drops any ?points= id it cannot find here. The RECORDED
            # PAST then became unreachable through the UI while still sitting in the
            # database, served to anyone who knew the raw id. On this building that is 389
            # points, including discharge-air setpoints, exhaust fan speeds and mech-room
            # temperatures. Stopping the logging of a dead sensor should not delete the
            # evidence of what it did. 2026-09-22 audit, M-6.
            # EXISTS on the rollup rides its primary key: MEASURED 11 ms across all 12,482
            # points, against the 147 ms this method already spends on SUM(n).
            "WHERE p.enabled = 1 "
            "   OR EXISTS (SELECT 1 FROM samples_1h r WHERE r.point_id = p.id) "
            "ORDER BY s.id, p.name, p.object_instance", (w,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["sample_count"] = (rolled.get(d["id"]) or 0) + (d.pop("tail_n") or 0)
            # `logging` is whether we are still RECORDING it; the row's presence means
            # there is history to read either way. The picker greys the false ones.
            d["logging"] = bool(d.pop("enabled", 1))
            out.append(d)
        return out

    # ── time-series ──────────────────────────────────────────────────────────
    @contextmanager
    def _tx(self):
        """One explicit transaction around a unit of work.

        ⚠️ The connection is opened `isolation_level=None`, i.e. AUTOCOMMIT. Without an
        explicit BEGIN every statement commits on its own -- and `executemany` commits
        PER ROW. A 657-point poll cycle was therefore 657 transactions and, at
        `synchronous=FULL`, 657 WAL fsyncs. Measured 24x slower than one transaction.

        The cost is the lesser half. The real problem for a RECORDER is atomicity: a crash
        mid-cycle left a partial poll cycle committed, with no marker distinguishing it from
        a genuine per-point read failure. A poll cycle is one observation of the building
        and is now written as one.

        Joins an outer transaction rather than nesting, because SQLite has no nested BEGIN.
        """
        if self._conn.in_transaction:
            yield
            return
        self._conn.execute("BEGIN")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def write_samples(self, samples: Iterable[Sample]) -> int:
        rows = [
            (
                s.point_id, s.ts_utc, s.value_num, s.value_text,
                s.status_flags, s.reliability,
                None if s.out_of_service is None else int(s.out_of_service),
                s.quality.value if isinstance(s.quality, Quality) else str(s.quality),
            )
            for s in samples
        ]
        if not rows:
            return 0
        # ⚠️ INSERT OR **IGNORE**, not REPLACE. Both are idempotent on the
        # (point_id, ts_utc) primary key for an identical re-write, but they differ on a
        # COLLISION WITH A DIFFERENT VALUE -- and the way that happens is a wall clock
        # stepping backwards (NTP correction, a VM resuming, a manual change). REPLACE
        # silently overwrote a real earlier observation with a later one wearing the same
        # timestamp; for a recorder, the first observation of a moment is the record and
        # the duplicate is the thing in question. IGNORE keeps it and says so.
        # ONE transaction for the whole cycle -- see _tx. 2026-09-22 audit, M-12.
        before = self._conn.total_changes
        with self._tx():
            self._conn.executemany(
                "INSERT OR IGNORE INTO samples "
                "(point_id, ts_utc, value_num, value_text, status_flags, reliability, "
                " out_of_service, quality) VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )
        written = self._conn.total_changes - before
        if written < len(rows):
            logger.error(
                "SAMPLE COLLISION: %d of %d rows already existed at that (point, second) "
                "and were NOT overwritten. This normally means the clock moved backwards; "
                "the earlier observation is kept. Check the host time source.",
                len(rows) - written, len(rows))
        return len(rows)

    def latest_value(self, point_id: int) -> Optional[Sample]:
        row = self._conn.execute(
            "SELECT * FROM samples WHERE point_id = ? ORDER BY ts_utc DESC LIMIT 1",
            (point_id,),
        ).fetchone()
        return self._row_to_sample(row) if row else None

    def query_series(self, point_id: int, start_utc: int, end_utc: int, *,
                     rollup: Optional[str] = None) -> Sequence[Any]:
        if rollup in (None, "raw"):
            rows = self._conn.execute(
                "SELECT ts_utc, value_num, value_text, quality FROM samples "
                "WHERE point_id = ? AND ts_utc BETWEEN ? AND ? ORDER BY ts_utc",
                (point_id, start_utc, end_utc),
            ).fetchall()
            return [dict(r) for r in rows]
        table = {"5m": "samples_5m", "1h": "samples_1h"}.get(rollup)
        if table is None:
            raise ValueError(f"unknown rollup {rollup!r} (expected None|'5m'|'1h')")
        # n_total travels with the bucket. F-11 added the column so that "recorded, and
        # faulted throughout" (n_total > 0, n = 0) stops being indistinguishable from "never
        # recorded" -- but nothing ever SELECTed it, so the distinction reached storage and
        # stopped there. On screen a faulted sensor still rendered as a hole, which is what
        # a dead collector looks like. 2026-09-22 audit, M-3.
        rows = self._conn.execute(
            f"SELECT bucket_utc, v_min, v_max, v_avg, v_last, n, n_total FROM {table} "
            "WHERE point_id = ? AND bucket_utc BETWEEN ? AND ? ORDER BY bucket_utc",
            (point_id, start_utc, end_utc),
        ).fetchall()
        return [dict(r) for r in rows]

    def rollup_series(self, point_id: int, start_utc: int, end_utc: int,
                      rollup: str) -> list[dict]:
        """A rollup series with its gaps filled from raw: the stored buckets, plus, bucketed
        from the 2-minute readings on the fly (tail_buckets),
          - the stretch AFTER the newest stored bucket, which the hourly maintenance has not
            reached yet (the M-9 stitch, 2026-09-22), and
          - the stretch BEFORE the oldest, where the 5-minute table's retention (365 days)
            has expired while raw's (730) has not. From July 2027 a 2-14 day window across
            that edge would have lost its older part, drawn as a collector outage
            (2026-10-27 review).
        A window with no stored buckets at all is bucketed from raw whole: it stays at the
        resolution asked for, and inside the row budget, instead of becoming raw. ONE place,
        for the viewer's chart and for Export data."""
        secs = self._ROLLUP_SECS.get({"5m": "samples_5m", "1h": "samples_1h"}.get(rollup, ""))
        rows = list(self.query_series(point_id, start_utc, end_utc, rollup=rollup))
        if not rows:
            return list(self.tail_buckets(point_id, start_utc, end_utc, rollup) or [])
        first, last = int(rows[0]["bucket_utc"]), int(rows[-1]["bucket_utc"])
        if secs and first - start_utc > secs:
            # A real outage at the start reads nothing here either: one bounded range read.
            rows = list(self.tail_buckets(point_id, start_utc, first - 1, rollup) or []) + rows
        if secs and last + secs <= end_utc:
            # ONE bucket past the newest stored one: that bucket is complete and stored, and
            # re-deriving it would risk disagreeing with the stored copy.
            rows += list(self.tail_buckets(point_id, last + secs, end_utc, rollup) or [])
        return rows

    def tail_buckets(self, point_id: int, start_utc: int, end_utc: int,
                     rollup: str) -> list[dict]:
        """Bucket raw samples ON THE FLY for the window the rollup tables have not reached.

        ⚠️ The rollups are materialised on the maintenance interval (hourly), and
        `_series_with_fallback` only dropped to raw when the rollup returned NOTHING AT ALL.
        So every window wide enough to use a rollup ended at the last materialised bucket
        and the tail was simply absent: measured 84 minutes short at the live edge. On a
        7-day view that reads as "collection stopped an hour ago", which is exactly the
        alarm this program exists to raise -- a false one, every day. 2026-09-22 audit, M-9.

        The bucket definition is copied from build_rollups deliberately and must stay in
        step with it: n counts CHARTABLE samples (good, non-null), n_total counts every
        sample recorded, and a bucket where everything faulted comes back n=0 / n_total>0
        with NULL aggregates rather than not coming back at all (F-11).

        One bounded range read, constrained by point_id, so it rides the clustered primary
        key rather than scanning -- see tests/test_plan_guard.py.
        """
        secs = self._ROLLUP_SECS.get({"5m": "samples_5m", "1h": "samples_1h"}.get(rollup, ""))
        if not secs:
            return []
        rows = self._conn.execute(
            "SELECT a.b AS bucket_utc, g.v_min, g.v_max, g.v_avg, l.v_last, "
            "       COALESCE(g.n, 0) AS n, a.n_total FROM ("
            "  SELECT (ts_utc / ?) * ? AS b, COUNT(*) AS n_total FROM samples "
            "  WHERE point_id = ? AND ts_utc >= ? AND ts_utc <= ? GROUP BY b) a "
            "LEFT JOIN ("
            "  SELECT (ts_utc / ?) * ? AS b, MIN(value_num) v_min, MAX(value_num) v_max, "
            "         AVG(value_num) v_avg, COUNT(*) n FROM samples "
            "  WHERE point_id = ? AND quality='good' AND value_num IS NOT NULL "
            "        AND ts_utc >= ? AND ts_utc <= ? GROUP BY b) g ON g.b = a.b "
            "LEFT JOIN ("
            # v_last: MAX(ts_utc) is the ONLY aggregate, so the bare value_num column takes
            # the newest row's value -- the same SQLite rule build_rollups relies on.
            "  SELECT (ts_utc / ?) * ? AS b, value_num AS v_last, MAX(ts_utc) AS mx "
            "  FROM samples WHERE point_id = ? AND quality='good' AND value_num IS NOT NULL "
            "        AND ts_utc >= ? AND ts_utc <= ? GROUP BY b) l ON l.b = a.b "
            "ORDER BY a.b",
            (secs, secs, point_id, start_utc, end_utc,
             secs, secs, point_id, start_utc, end_utc,
             secs, secs, point_id, start_utc, end_utc),
        ).fetchall()
        return [dict(r) for r in rows]

    def points_for_analysis(self) -> list[dict]:
        """Enabled points with the fields the AI pre-screen / context builder need:
        identity + units + role + configured alarm thresholds + system code. Ordered
        by system then name (stable grouping)."""
        rows = self._conn.execute(
            "SELECT p.id, p.name, p.display_name, p.operator_note, p.description, "
            " p.object_type, p.object_instance, p.units, "
            " p.units_text, p.system_id, p.role, p.role_canonical, "
            " p.alarm_enabled, p.alarm_hi, p.alarm_lo, p.alarm_deadband, "
            " s.code AS system_code, s.name AS system_name, s.kind, "
            " s.notes AS system_notes, s.occupancy_mode AS system_occupancy, "
            " s.sequence_of_operations AS system_soo "
            "FROM points p LEFT JOIN systems s ON p.system_id = s.id "
            "WHERE p.enabled = 1 ORDER BY s.id, p.name, p.object_instance"
        ).fetchall()
        return [dict(r) for r in rows]

    # ── point roles (ai/roles.py orchestrates; DAL provides primitives so the
    #    storage layer never imports the ai layer) ──────────────────────────────
    def points_min(self) -> list[dict]:
        """Minimal point rows for role classification: id, name, system_id, role,
        enabled (canonical selection must prefer the curated/enabled sensor, not a
        disabled dead twin), plus units_text/object_type — canonical selection for a
        MODULATING role must be able to tell a percent analog position from a binary
        end-switch, or it elects the end-switch (see roles.canonical_ids)."""
        return [dict(r) for r in self._conn.execute(
            # display_name/operator_note/description feed classify(): the operator's own
            # label is the most reliable signal on plant equipment, where the BACnet name
            # is a commissioning tag and the BAS description can be plain wrong.
            "SELECT id, name, display_name, description, system_id, role, enabled, "
            " units_text, object_type FROM points")]

    def set_point_role(self, point_id: int, role: Optional[str]) -> None:
        self._conn.execute("UPDATE points SET role = ? WHERE id = ?", (role, point_id))

    def set_point_roles(self, roles: "dict[int, Optional[str]]") -> int:
        """Persist many point roles as ONE transaction, writing only what changed.

        ⚠️ assign_roles used to call set_point_role in a loop -- 12,482 separate
        autocommit transactions on this building, every model rebuild, on the loop the
        BACnet collector shares. Same defect F-06 fixed for write_samples, in a second
        place. Roles are derived from point NAMES, which change only at discovery, so
        almost every one of those writes was rewriting a value with itself.
        2026-09-22 audit, M-8.
        """
        cur = {r["id"]: r["role"] for r in self._conn.execute(
            "SELECT id, role FROM points")}
        changed = [(v, k) for k, v in roles.items() if k in cur and cur[k] != v]
        if not changed:
            return 0
        with self._tx():
            self._conn.executemany(
                "UPDATE points SET role = ? WHERE id = ?", changed)
        return len(changed)

    def set_point_limits(self, point_id: int, *, alarm_hi: Optional[float] = None,
                         alarm_lo: Optional[float] = None,
                         alarm_enabled: bool = False) -> None:
        """Write the out-of-range thresholds for a point (ai/limits.py copies these
        from the plant's own trended limit points on every model rebuild).

        Deliberately writes NULLs through: if the plant stops publishing a limit, the
        stale threshold must clear rather than keep alarming against a value nothing
        maintains. Never touched by discovery — these are analysis state, not
        discovery-derived fields."""
        self._conn.execute(
            "UPDATE points SET alarm_hi = ?, alarm_lo = ?, alarm_enabled = ? "
            "WHERE id = ?", (alarm_hi, alarm_lo, int(bool(alarm_enabled)), point_id))
        self._conn.commit()

    def set_role_canonical(self, ids: Sequence[int]) -> None:
        """Mark exactly the given point ids as the canonical source for their role
        (clears all others first). Idempotent. ONE transaction: as ~460 separate autocommit
        UPDATEs (each fsynced) a watchdog rules run in between saw units with no canonical
        points and skipped their rules for that run (2026-10-27 review; the sibling of
        set_point_roles, M-8)."""
        with self._tx():
            self._conn.execute("UPDATE points SET role_canonical = 0")
            self._conn.executemany(
                "UPDATE points SET role_canonical = 1 WHERE id = ?", [(pid,) for pid in ids])

    def samples_extent(self, point_id: Optional[int] = None) -> dict:
        """Overall (or per-point) time span of stored samples, so the viewer can
        default its range to where data actually exists instead of an empty window.

        Includes COUNT(*) — a FULL scan of samples. Use samples_time_extent() on hot
        paths (the viewer) that only need min/max; reserve this for the status endpoint
        where the total count is actually wanted.

        The min/max are SEPARATE scalar subqueries, exactly as in samples_time_extent().
        Asking for MIN, MAX and COUNT in ONE select made SQLite give up the index shortcut
        and SCAN the whole covering index: measured 1.563 s against 0.352 s for the same
        three answers split apart, where the remaining 0.352 s is the COUNT alone and both
        min/max become O(log n) SEARCHes. That difference was the whole latency of
        /api/poll-health, then on the shared event loop. The COUNT alone later grew to 10-23 s
        (43.8 M rows, 2026-10-20), so that endpoint now calls samples_extent_readonly() in a
        worker thread."""
        if point_id is None:
            r = self._conn.execute(
                "SELECT (SELECT MIN(ts_utc) FROM samples) AS min_ts, "
                "       (SELECT MAX(ts_utc) FROM samples) AS max_ts, "
                "       (SELECT COUNT(*) FROM samples) AS n"
            ).fetchone()
        else:
            r = self._conn.execute(
                "SELECT MIN(ts_utc) AS min_ts, MAX(ts_utc) AS max_ts, "
                " COUNT(*) AS n FROM samples WHERE point_id = ?",
                (point_id,),
            ).fetchone()
        return {"min_ts": r["min_ts"], "max_ts": r["max_ts"], "n": r["n"]}

    def rollup_watermark(self) -> Optional[int]:
        """Newest materialized 5-minute bucket, or None if nothing is rolled up yet.

        Exists so the collector can decide whether maintenance is DUE from the data
        instead of from process uptime. See _table_watermark for why this is NOT a bare
        MAX() -- the claim that it was "O(log n)" stood in this docstring for weeks and
        was wrong by two orders of magnitude."""
        return self._table_watermark("samples_5m")

    def _table_watermark(self, table: str) -> Optional[int]:
        """Newest bucket in a rollup table, answered through the point list.

        ⚠️ NOT `SELECT MAX(bucket_utc) FROM <rollup>`. The rollups are WITHOUT ROWID on
        (point_id, bucket_utc), so a bare MAX of the SECOND key column walks the entire
        clustered index. MEASURED on the live table: 1,126 ms against 10.7 ms for the
        correlated per-point form -- 105x, for an identical answer.

        The reason this survived two previous passes is that EXPLAIN QUERY PLAN reports the
        bare form as `SEARCH samples_5m USING PRIMARY KEY`, which reads as optimal. Plan
        text alone cannot tell the two apart, so tests/test_plan_guard.py EXPLAINs the SQL
        each DAL method ACTUALLY executes and asserts on the absence of an unconstrained
        line, rather than the presence of a reassuring one. 2026-09-22 audit, M-1/M-2."""
        if table not in self._ROLLUP_SECS:
            raise ValueError(f"not a rollup table: {table!r}")
        # No `WHERE p.enabled = 1` on purpose: a watermark that moved backwards when a
        # point was disabled would make build_rollups re-aggregate history it already has.
        r = self._conn.execute(
            f"SELECT MAX(m) AS m FROM (SELECT (SELECT MAX(bucket_utc) FROM {table} s "
            f"WHERE s.point_id = p.id) AS m FROM points p)").fetchone()
        return None if r is None or r["m"] is None else int(r["m"])

    def newest_sample_ts(self, until: Optional[int] = None) -> Optional[int]:
        """The newest raw sample's timestamp, optionally only those at or before `until`.
        A lone MAX on idx_samples_ts: O(log n) either way."""
        if until is None:
            row = self._conn.execute("SELECT MAX(ts_utc) FROM samples").fetchone()
        else:
            row = self._conn.execute("SELECT MAX(ts_utc) FROM samples WHERE ts_utc <= ?",
                                     (int(until),)).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def samples_time_extent(self) -> dict:
        """Just the min/max sample timestamps (NO count). Each aggregate is a lone
        MIN/MAX so SQLite answers it from idx_samples_ts in O(log n) instead of the
        full-table COUNT(*) scan in samples_extent(). This is what the trend viewer's
        picker + overlay endpoints use, so a growing samples table never stalls a
        chart refresh (or the shared event loop)."""
        # ⚠️ The extent is the UNION of raw and the rollups, not raw alone.
        # Retention keeps hourly buckets far longer than raw samples (730 days vs 3,650), so
        # reading only `samples` means that the moment raw starts being pruned the viewer
        # reports a SHORTER record than the program actually holds -- the overview strip and
        # the "All" button would quietly hide years of history that is still on disk, with
        # nothing to say so. It does not bite today because raw and rollup start together;
        # it bites on the first day retention ever runs, which is the day nobody is looking.
        # Each term is a lone MIN/MAX answered from an index, so this stays O(log n).
        # ⚠️ The rollup terms MUST be correlated per-point subqueries, not a bare
        # MIN/MAX over the table. samples_5m and samples_1h are WITHOUT ROWID with the PK
        # (point_id, bucket_utc), so there is NO index on bucket_utc alone and a plain
        # MIN(bucket_utc) walks the entire clustered index -- MEASURED 1.28 s on 10.4 M
        # rows, against 0.014 s for the correlated form. Identical results, 91x.
        #
        # This is the same lesson the `samples` table already taught: on a clustered PK,
        # per-point work goes through the point list. Getting it wrong here was worse than
        # on samples, because /api/series returns this extent on EVERY fetch -- it put
        # 2.2 s on the hottest path in the program, on the event loop the BACnet stack
        # shares, and left the viewer sitting at "loading...".
        # The rollup MAXIMA only when raw holds nothing: a bucket is the floor of a raw
        # timestamp and raw keeps the newest data, so they can never exceed raw's. Each is a
        # 10-13 ms per-point pass, on every range click and every /api/series and
        # /api/logged (2026-10-27 review: 55 ms where the docstring said about 1).
        r = self._conn.execute(
            "SELECT (SELECT MIN(ts_utc) FROM samples) AS raw_lo, "
            "       (SELECT MAX(ts_utc) FROM samples) AS raw_hi, "
            "       (SELECT MIN(m) FROM (SELECT (SELECT MIN(bucket_utc) FROM samples_1h s "
            "            WHERE s.point_id = p.id) AS m FROM points p WHERE p.enabled = 1)) AS h_lo, "
            "       (SELECT MIN(m) FROM (SELECT (SELECT MIN(bucket_utc) FROM samples_5m s "
            "            WHERE s.point_id = p.id) AS m FROM points p WHERE p.enabled = 1)) AS m_lo"
        ).fetchone()
        his = [r["raw_hi"]] if r["raw_hi"] is not None else [v for v in self._conn.execute(
            "SELECT (SELECT MAX(m) FROM (SELECT (SELECT MAX(bucket_utc) FROM samples_1h s "
            "            WHERE s.point_id = p.id) AS m FROM points p WHERE p.enabled = 1)), "
            "       (SELECT MAX(m) FROM (SELECT (SELECT MAX(bucket_utc) FROM samples_5m s "
            "            WHERE s.point_id = p.id) AS m FROM points p WHERE p.enabled = 1))"
        ).fetchone() if v is not None]
        los = [v for v in (r["raw_lo"], r["h_lo"], r["m_lo"]) if v is not None]
        return {"min_ts": min(los) if los else None,
                "max_ts": max(his) if his else None}

    # ── rollups / retention ─────────────────────────────────
    _ROLLUP_SECS = {"samples_5m": 300, "samples_1h": 3600}

    def build_rollups(self, up_to_utc: int) -> dict[str, int]:
        """Materialize the 5-minute and hourly downsample tables from raw GOOD samples,
        incrementally. Idempotent: re-aggregates from the last built bucket forward
        (INSERT OR REPLACE), and never builds the current in-progress bucket. Returns
        the number of buckets written per table.

        v_last is the value of the newest sample in the bucket — computed in a subquery
        whose only aggregate is MAX(ts_utc), so the bare value_num column reliably takes
        that row's value (SQLite min/max bare-column rule)."""
        built: dict[str, int] = {}
        for tbl, secs in self._ROLLUP_SECS.items():
            last = self._table_watermark(tbl)
            start = 0 if last is None else int(last)   # re-do the last (maybe partial) bucket
            end = (int(up_to_utc) // secs) * secs       # exclude the in-progress bucket
            if end <= start and last is not None:
                built[tbl] = 0
                continue
            # ⚠️ The DRIVING set is every sample regardless of quality, with the
            # good-only aggregates LEFT JOINed onto it. It used to be the other way round,
            # so a bucket in which every sample was faulted produced no row -- and after raw
            # retention expired, a failed sensor was indistinguishable from a period the
            # collector never ran. Now such a bucket lands with n=0, n_total>0 and NULL
            # aggregates: the value is correctly absent, the FACT of the reading is not.
            # Driven by the POINT LIST, so every read is a range on the clustered key
            # (point_id, ts_utc). The three `FROM samples WHERE ts_utc ...` scans this
            # replaced planned on idx_samples_ts, which is not covering: one primary-key
            # lookup per row. MEASURED on the live database, an hour of 5-minute buckets:
            # 0.94-1.14 s against 0.15 s, identical rows, while holding the write lock on
            # the loop the collector shares; a day's backlog was 21 s (2026-10-27 review).
            # `ok` is the good-numeric test the old g/l subqueries applied; a bucket where
            # every sample faulted still lands with n=0 and NULL aggregates (see above).
            cur = self._conn.execute(
                f"INSERT OR REPLACE INTO {tbl} "
                "(point_id, bucket_utc, v_min, v_max, v_avg, v_last, n, n_total) "
                "WITH s AS (SELECT x.point_id, (x.ts_utc / ?1) * ?1 AS b, x.ts_utc, "
                "                  x.value_num, "
                "                  (x.quality = 'good' AND x.value_num IS NOT NULL) AS ok "
                "           FROM points p CROSS JOIN samples x "
                "           ON x.point_id = p.id AND x.ts_utc >= ?2 AND x.ts_utc < ?3) "
                "SELECT a.point_id, a.b, a.v_min, a.v_max, a.v_avg, l.v_last, a.n, a.n_total "
                "FROM (SELECT point_id, b, MIN(CASE WHEN ok THEN value_num END) AS v_min, "
                "             MAX(CASE WHEN ok THEN value_num END) AS v_max, "
                "             AVG(CASE WHEN ok THEN value_num END) AS v_avg, "
                "             COALESCE(SUM(ok), 0) AS n, COUNT(*) AS n_total "
                "      FROM s GROUP BY point_id, b) a "
                "LEFT JOIN ("
                # v_last: MAX(ts_utc) is the ONLY aggregate here, so the bare value_num
                # column takes the value from each group's newest-ts row (SQLite rule).
                "  SELECT point_id, b, value_num AS v_last, MAX(ts_utc) AS mx "
                "  FROM s WHERE ok GROUP BY point_id, b) l "
                "ON l.point_id = a.point_id AND l.b = a.b",
                (secs, start, end),
            )
            built[tbl] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        return built

    # Retention floor and single-pass ceiling. See enforce_retention.
    MIN_RAW_RETENTION_DAYS = 7
    MAX_PRUNE_FRACTION = 0.25
    # The fraction alone is the wrong test on a small database: a fresh deploy, or a test
    # fixture holding two rows, can legitimately prune most of what it has. Both conditions
    # must hold, so the guard only ever fires on a deletion that is big in absolute terms
    # AND most of the record. At the real 427,667 rows/day a steady-state daily prune is
    # ~1.6% of 26 M, so this never trips on normal operation.
    MIN_PRUNE_GUARD_ROWS = 100_000
    # F-05 put a floor under raw only. But the rollups ARE the record once raw has expired
    # -- rollup_1h_days is 3,650 against raw's 730 -- so a typo in rollup_1h_days deleted
    # the long memory with nothing to stop it. 2026-09-22 audit, L-1.
    MIN_ROLLUP_RETENTION_DAYS = 30

    def enforce_retention(self, now_utc: int, policy: Any) -> dict[str, int]:
        """Prune data older than the configured windows. raw_days caps the
        raw samples table (the unbounded grower); the rollup tables have their own,
        longer windows. A null/absent window means keep indefinitely. Returns rows
        deleted per table. Safe to call every maintenance cycle; a no-op when nothing
        is old enough yet."""
        def _days(key: str) -> Optional[float]:
            v = policy.get(key) if hasattr(policy, "get") else None
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        deleted: dict[str, int] = {}
        raw_days = _days("raw_days")
        # FLOOR. A retention window is the one setting whose new value destroys data on the
        # next cycle. A fat-fingered 1 where 730 was meant would delete two years of raw
        # history in a few hundred milliseconds with no confirmation and no undo. Below the
        # floor the prune is REFUSED, loudly, rather than clamped quietly -- a clamp would
        # still delete everything down to the floor.
        if raw_days is not None and 0 < raw_days < self.MIN_RAW_RETENTION_DAYS:
            logger.error(
                "retention REFUSED: raw_days=%s is below the %d-day floor. Nothing was "
                "deleted. Set storage.retention.raw_days in config.yaml if this is "
                "genuinely intended.", raw_days, self.MIN_RAW_RETENTION_DAYS)
            raw_days = None
        if raw_days and raw_days > 0:
            cut = int(now_utc - raw_days * 86400)
            # LARGE-PRUNE GUARD. The floor stops an absurd value; this stops a merely wrong
            # one. Retention is meant to shave the tail, so a pass that would remove a large
            # fraction of the whole record means the window moved, not that time passed.
            doomed = self._conn.execute(
                "SELECT COUNT(*) FROM samples WHERE ts_utc < ?", (cut,)).fetchone()[0]
            # The denominator only when the guard could fire. Counting the whole table EVERY
            # hourly pass, with nothing old enough to delete, held the event loop the BACnet
            # stack shares for 13 s a pass at 47.6 M rows (max 56 s), growing with the
            # table, and the answer was never used (2026-10-27 review).
            total = (self._conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
                     if doomed >= self.MIN_PRUNE_GUARD_ROWS else 0)
            allow_big = bool(policy.get("allow_large_prune")
                             if hasattr(policy, "get") else False)
            if not doomed:
                pass
            elif (total and doomed >= self.MIN_PRUNE_GUARD_ROWS
                    and doomed / total > self.MAX_PRUNE_FRACTION and not allow_big):
                logger.error(
                    "retention REFUSED: raw_days=%s would delete %s of %s rows (%.1f%%), "
                    "over the %.0f%% single-pass limit. Nothing was deleted. If this is "
                    "intended, set storage.retention.allow_large_prune: true for one run.",
                    raw_days, f"{doomed:,}", f"{total:,}", 100 * doomed / total,
                    100 * self.MAX_PRUNE_FRACTION)
            else:
                deleted["samples"] = self._conn.execute(
                    "DELETE FROM samples WHERE ts_utc < ?", (cut,)).rowcount
        for tbl, key in (("samples_5m", "rollup_5m_days"), ("samples_1h", "rollup_1h_days")):
            d = _days(key)
            if d is not None and 0 < d < self.MIN_ROLLUP_RETENTION_DAYS:
                logger.error("retention REFUSED: %s=%s is below the %d-day floor for %s. "
                             "Nothing was deleted.",
                             key, d, self.MIN_ROLLUP_RETENTION_DAYS, tbl)
                continue
            if d and d > 0:
                cut = int(now_utc - d * 86400)
                # Per point, through the clustered key. A bare `WHERE bucket_utc < ?` is a
                # full SCAN of the table on EVERY maintenance pass -- measured 604 ms while
                # deleting nothing at all. Orphan rows (a point_id no longer in `points`)
                # would be skipped by this form, but the DAL opens with foreign_keys=ON and
                # nothing in the program ever deletes a point, so none exist: verified 0 in
                # both rollup tables on the live database.
                deleted[tbl] = self._conn.execute(
                    f"DELETE FROM {tbl} WHERE point_id IN (SELECT id FROM points) "
                    f"AND bucket_utc < ?", (cut,)).rowcount

        # Diagnostic tables. Only the three sample tables above had windows, so these grew
        # forever: poll_health at ~3,450 rows/day (1.26 M/yr) was already the slowest simple
        # endpoint on the box. Nothing needs year-old poll diagnostics.
        d = _days("poll_health_days")
        if d and d > 0:
            cut = int(now_utc - d * 86400)
            deleted["poll_health"] = self._conn.execute(
                "DELETE FROM poll_health WHERE ts_utc < ?", (cut,)).rowcount

        # Alerts: RESOLVED history only. An open alert is a live condition and is never
        # pruned however old it is -- ageing out an unacknowledged fault would be the one
        # way this could lose something that matters. Dated by when it was resolved/last
        # seen, not raised, so a long-running condition is kept while it is still current.
        d = _days("alerts_days")
        if d and d > 0:
            cut = int(now_utc - d * 86400)
            deleted["alerts"] = self._conn.execute(
                "DELETE FROM alerts WHERE status = 'resolved' "
                "AND COALESCE(resolved_utc, last_seen_utc, created_utc) < ?",
                (cut,)).rowcount
        return deleted

    # ── saved views ────────────────────────────────────────────────
    # The one piece of operator configuration that used to live only in a browser.

    def list_views(self, kind: str = "trend") -> list[dict]:
        return [dict(r) for r in self._conn.execute(
            "SELECT name, kind, spec, created_utc, updated_utc FROM views "
            "WHERE kind = ? ORDER BY name", (kind,))]

    def save_view(self, name: str, spec: str, kind: str = "trend") -> None:
        now = int(time.time())
        self._conn.execute(
            "INSERT INTO views (name, kind, spec, created_utc, updated_utc) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET spec=excluded.spec, kind=excluded.kind, "
            " updated_utc=excluded.updated_utc",
            (name, kind, spec, now, now))

    def delete_view(self, name: str) -> bool:
        return self._conn.execute("DELETE FROM views WHERE name = ?", (name,)).rowcount > 0

    # ── logger health ────────────────────────────────────────────────────────
    def record_poll_health(self, health: PollHealth) -> None:
        self._conn.execute(
            "INSERT INTO poll_health "
            "(device_id, ts_utc, ok, latency_ms, reads_attempted, reads_ok, method, error) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (health.device_id, health.ts_utc, int(health.ok), health.latency_ms,
             health.reads_attempted, health.reads_ok, health.method, health.error),
        )

    # ── weather (ai/weather.py writes; context + viewer read) ─────────────────
    def upsert_weather(self, row: dict, *, updated_utc: int) -> None:
        """Insert-or-replace one hour-aligned weather observation (keyed on ts_utc)."""
        self._conn.execute(
            "INSERT INTO weather (ts_utc, oat_f, rh_pct, wet_bulb_f, dewpoint_f, "
            " enthalpy, wind_mph, solar_wm2, pressure_mb, source, created_utc) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(ts_utc) DO UPDATE SET oat_f=excluded.oat_f, rh_pct=excluded.rh_pct, "
            " wet_bulb_f=excluded.wet_bulb_f, dewpoint_f=excluded.dewpoint_f, "
            " enthalpy=excluded.enthalpy, wind_mph=excluded.wind_mph, "
            " solar_wm2=excluded.solar_wm2, pressure_mb=excluded.pressure_mb, "
            " source=excluded.source",
            (row["ts_utc"], row.get("oat_f"), row.get("rh_pct"), row.get("wet_bulb_f"),
             row.get("dewpoint_f"), row.get("enthalpy"), row.get("wind_mph"),
             row.get("solar_wm2"), row.get("pressure_mb"), row.get("source"), updated_utc))

    def weather_range(self, start_utc: int, end_utc: int) -> list[dict]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM weather WHERE ts_utc BETWEEN ? AND ? ORDER BY ts_utc",
            (start_utc, end_utc)).fetchall()]

    def weather_rows(self, start_utc: int, end_utc: int) -> list[dict]:
        """The stored hourly weather rows in [start, end), oldest first: one snapshot per
        hour from the Tempest (or open-meteo before it was set up), for the export."""
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM weather WHERE ts_utc >= ? AND ts_utc < ? ORDER BY ts_utc",
            (int(start_utc), int(end_utc)))]

    def weather_window(self, start_utc: int, end_utc: int) -> Optional[dict]:
        """Compact weather rollup for a period (for the report context): avg/min/max
        of the key drivers. None if no weather stored for the window."""
        r = self._conn.execute(
            "SELECT COUNT(*) n, AVG(oat_f) oat_avg, MIN(oat_f) oat_min, MAX(oat_f) oat_max, "
            " AVG(rh_pct) rh_avg, AVG(wet_bulb_f) wb_avg, MAX(wet_bulb_f) wb_max, "
            " AVG(enthalpy) enth_avg, MAX(enthalpy) enth_max, "
            " MAX(solar_wm2) solar_max, AVG(wind_mph) wind_avg "
            "FROM weather WHERE ts_utc BETWEEN ? AND ?", (start_utc, end_utc)).fetchone()
        if not r or not r["n"]:
            return None
        return {k: (round(r[k], 1) if isinstance(r[k], float) else r[k]) for k in r.keys()}

    # ── learned baselines (ai/baseline.py writes; pre-screen reads) ────────────
    def upsert_baseline(self, point_id: int, mode: str, *, mean: Optional[float],
                        std: Optional[float], n: int, changed_frac: Optional[float],
                        long_slope: Optional[float], window_start_utc: Optional[int],
                        window_end_utc: Optional[int], updated_utc: int) -> None:
        self._conn.execute(
            "INSERT INTO baseline (point_id, mode, mean, std, n, changed_frac, "
            " long_slope, window_start_utc, window_end_utc, updated_utc) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(point_id, mode) DO UPDATE SET mean=excluded.mean, "
            " std=excluded.std, n=excluded.n, changed_frac=excluded.changed_frac, "
            " long_slope=excluded.long_slope, window_start_utc=excluded.window_start_utc, "
            " window_end_utc=excluded.window_end_utc, updated_utc=excluded.updated_utc",
            (point_id, mode, mean, std, n, changed_frac, long_slope,
             window_start_utc, window_end_utc, updated_utc))

    def baselines(self) -> dict:
        """All baselines as {point_id: {'occ':{mean,std,n}, 'unocc':{...},
        'changed_frac':x, 'long_slope':y}}."""
        out: dict = {}
        for r in self._conn.execute("SELECT * FROM baseline").fetchall():
            d = out.setdefault(r["point_id"], {})
            d[r["mode"]] = {"mean": r["mean"], "std": r["std"], "n": r["n"]}
            d["changed_frac"] = r["changed_frac"]
            d["long_slope"] = r["long_slope"]
        return out

    def upsert_corr(self, system_id: Optional[int], kind: str, *,
                    x_point_id: Optional[int], y_point_id: Optional[int],
                    slope: Optional[float], intercept: Optional[float],
                    residual_std: Optional[float], n: int, updated_utc: int) -> None:
        self._conn.execute(
            "INSERT INTO baseline_corr (system_id, kind, x_point_id, y_point_id, "
            " slope, intercept, residual_std, n, updated_utc) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(system_id, kind) DO UPDATE SET x_point_id=excluded.x_point_id, "
            " y_point_id=excluded.y_point_id, slope=excluded.slope, "
            " intercept=excluded.intercept, residual_std=excluded.residual_std, "
            " n=excluded.n, updated_utc=excluded.updated_utc",
            (system_id, kind, x_point_id, y_point_id, slope, intercept,
             residual_std, n, updated_utc))

    def corrs(self, system_id: Optional[int] = None) -> list[dict]:
        if system_id is None:
            rows = self._conn.execute("SELECT * FROM baseline_corr").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM baseline_corr WHERE system_id = ?", (system_id,)).fetchall()
        return [dict(r) for r in rows]

    # ── AI reports & findings ───────────────────────────────────────
    def insert_report(self, report: Report, *, inputs_json: Optional[str] = None,
                      call_json: Optional[str] = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO reports (kind, period_start_utc, period_end_utc, provider, "
            " model, prose, status, created_utc, inputs_json, call_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (report.kind, report.period_start_utc, report.period_end_utc,
             report.provider, report.model, report.prose, report.status,
             report.created_utc, inputs_json, call_json),
        )
        return int(cur.lastrowid)

    def upsert_finding(self, finding: Finding) -> int:
        """Insert a finding, or — if an OPEN finding with the same fingerprint exists
        — update it in place and bump last_seen_utc. Returns
        the finding id. A NULL fingerprint always inserts (untracked)."""
        if finding.fingerprint:
            row = self._conn.execute(
                "SELECT id, first_seen_utc FROM findings "
                "WHERE fingerprint = ? AND status != 'closed' "
                "ORDER BY id DESC LIMIT 1",
                (finding.fingerprint,),
            ).fetchone()
            if row:
                self._conn.execute(
                    "UPDATE findings SET report_id=?, system_id=?, point_id=?, "
                    " severity=?, timeframe=?, equipment=?, metric=?, summary=?, "
                    " recommendation=?, evidence=?, last_seen_utc=? WHERE id=?",
                    (finding.report_id, finding.system_id, finding.point_id,
                     finding.severity, finding.timeframe, finding.equipment,
                     finding.metric, finding.summary, finding.recommendation,
                     finding.evidence, finding.last_seen_utc, row["id"]),
                )
                return int(row["id"])
        cur = self._conn.execute(
            "INSERT INTO findings (report_id, system_id, point_id, fingerprint, "
            " severity, timeframe, equipment, metric, summary, recommendation, "
            " evidence, status, first_seen_utc, last_seen_utc, created_utc) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (finding.report_id, finding.system_id, finding.point_id, finding.fingerprint,
             finding.severity, finding.timeframe, finding.equipment, finding.metric,
             finding.summary, finding.recommendation, finding.evidence, finding.status,
             finding.first_seen_utc, finding.last_seen_utc, finding.created_utc),
        )
        return int(cur.lastrowid)

    def list_reports(self, *, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT r.*, (SELECT COUNT(*) FROM findings f WHERE f.report_id=r.id) "
            " AS finding_count FROM reports r ORDER BY r.period_end_utc DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_report(self, report_id: int) -> Optional[dict]:
        row = self._conn.execute("SELECT * FROM reports WHERE id=?",
                                 (report_id,)).fetchone()
        if not row:
            return None
        rep = dict(row)
        rep["findings"] = self.list_findings(report_id=report_id)
        return rep

    def list_findings(self, *, report_id: Optional[int] = None,
                      status: Optional[str] = None, severity: Optional[str] = None,
                      system_id: Optional[int] = None, limit: int = 500) -> list[dict]:
        sql = ["SELECT f.*, s.code AS system_code FROM findings f "
               "LEFT JOIN systems s ON f.system_id = s.id WHERE 1=1"]
        params: list[Any] = []
        if report_id is not None:
            sql.append("AND f.report_id = ?"); params.append(report_id)
        if status:
            sql.append("AND f.status = ?"); params.append(status)
        if severity:
            sql.append("AND f.severity = ?"); params.append(severity)
        if system_id is not None:
            sql.append("AND f.system_id = ?"); params.append(system_id)
        # newest first, but rank criticals up within the same recency via created desc
        sql.append("ORDER BY f.last_seen_utc DESC, f.id DESC LIMIT ?")
        params.append(limit)
        return [dict(r) for r in self._conn.execute(" ".join(sql), params).fetchall()]

    def open_findings(self, *, limit: int = 100) -> list[dict]:
        """Currently-open findings — fed back to the LLM as prior-findings history
 so it tracks persistence instead of rediscovering cold."""
        return self.list_findings(status="open", limit=limit)

    def set_finding_status(self, finding_id: int, status: str, *,
                           operator_note: Optional[str] = None,
                           now_utc: Optional[int] = None) -> None:
        """Set a finding's status, optionally recording WHY. An acknowledgement with a
        note is the operator saying "this is intentional" — it is rendered back to the
        model as explained rather than silently dropped (see open_findings /
        confirmed_findings). Passing no note leaves any existing one untouched."""
        if operator_note is None:
            self._conn.execute("UPDATE findings SET status=? WHERE id=?",
                               (status, finding_id))
            return
        self._conn.execute(
            "UPDATE findings SET status=?, operator_note=?, ack_utc=? WHERE id=?",
            (status, operator_note or None,
             int(now_utc if now_utc is not None else time.time()), finding_id))

    def confirmed_findings(self, *, limit: int = 50) -> list[dict]:
        """Findings the operator ACKNOWLEDGED with a reason — i.e. "yes, I did that".
        Fed to the model as ground truth so it stops re-reporting them as faults but
        still judges their consequences."""
        return [dict(r) for r in self._conn.execute(
            "SELECT f.*, s.code AS system_code FROM findings f "
            "LEFT JOIN systems s ON f.system_id = s.id "
            "WHERE f.status = 'acknowledged' AND f.operator_note IS NOT NULL "
            "ORDER BY f.ack_utc DESC LIMIT ?", (limit,)).fetchall()]

    # ── operator action log (ranged annotations, schema §10) ─────────────────
    def add_annotation(self, *, ts_utc: int, title: str,
                       ts_end_utc: Optional[int] = None,
                       system_id: Optional[int] = None,
                       point_id: Optional[int] = None,
                       body: Optional[str] = None, author: Optional[str] = None,
                       kind: str = "manual", now_utc: Optional[int] = None) -> int:
        """Record a TIME-BOUNDED operator action ("forced AC-9 occupied 14:00-16:00",
        "raised the DAT setpoint to test"). ts_end_utc NULL = a point-in-time event."""
        cur = self._conn.execute(
            "INSERT INTO annotations (ts_utc, ts_end_utc, system_id, point_id, kind, "
            " author, title, body, created_utc) VALUES (?,?,?,?,?,?,?,?,?)",
            (ts_utc, ts_end_utc, system_id, point_id, kind, author, title, body,
             int(now_utc if now_utc is not None else time.time())))
        return int(cur.lastrowid)

    def annotations_overlapping(self, start_utc: int, end_utc: int) -> list[dict]:
        """Annotations relevant to a report window — THE point of the feature.

        A RANGED note counts when its span overlaps the window at all; a POINT event
        (ts_end_utc NULL) counts only when it falls inside it. Without the second rule a
        one-off event from months ago would ride along in every report forever, which is
        exactly the always-on behaviour the per-unit `notes` field already provides and
        that this is meant to be different from."""
        return [dict(r) for r in self._conn.execute(
            "SELECT a.*, s.code AS system_code, p.name AS point_name FROM annotations a "
            "LEFT JOIN systems s ON a.system_id = s.id "
            "LEFT JOIN points p ON a.point_id = p.id "
            "WHERE (a.ts_end_utc IS NULL AND a.ts_utc >= ? AND a.ts_utc < ?) "
            "   OR (a.ts_end_utc IS NOT NULL AND a.ts_utc < ? AND a.ts_end_utc > ?) "
            "ORDER BY a.ts_utc", (start_utc, end_utc, end_utc, start_utc)).fetchall()]

    def list_annotations(self, *, limit: int = 100) -> list[dict]:
        """Newest-first log for the operator's own review: what did I change, when, why."""
        return [dict(r) for r in self._conn.execute(
            "SELECT a.*, s.code AS system_code, p.name AS point_name FROM annotations a "
            "LEFT JOIN systems s ON a.system_id = s.id "
            "LEFT JOIN points p ON a.point_id = p.id "
            "ORDER BY a.ts_utc DESC LIMIT ?", (limit,)).fetchall()]

    def delete_annotation(self, annotation_id: int) -> bool:
        cur = self._conn.execute("DELETE FROM annotations WHERE id=?", (annotation_id,))
        return cur.rowcount > 0

    def age_out_findings(self, cutoff_utc: int) -> list[dict]:
        """Auto-close OPEN findings not re-seen since `cutoff_utc` (the issue stopped
        being reported = resolved). Returns the closed rows so the caller can note
        "resolved since last report". Only ever runs after a SUCCESSFUL report — a
        provider error must not be read as "everything cleared"."""
        rows = self._conn.execute(
            "SELECT f.id, f.summary, f.severity, f.metric, s.code AS system_code "
            "FROM findings f LEFT JOIN systems s ON f.system_id = s.id "
            "WHERE f.status = 'open' AND f.last_seen_utc IS NOT NULL "
            "AND f.last_seen_utc < ?", (cutoff_utc,)).fetchall()
        closed = [dict(r) for r in rows]
        if closed:
            self._conn.executemany("UPDATE findings SET status='closed' WHERE id=?",
                                   [(r["id"],) for r in closed])
        return closed

    # ── alerts (between-report proactive monitoring) ─────────────────────────
    def upsert_alert(self, alert: Alert) -> tuple[int, bool]:
        """Insert an alert, or — if an OPEN alert with the same fingerprint exists —
        update it in place and bump last_seen_utc (the same live condition, still active).
        Returns (id, is_new): is_new=True only when a fresh alert row was inserted, so the
        caller can notify on genuinely-NEW conditions and not re-notify ongoing ones."""
        row = self._conn.execute(
            "SELECT id FROM alerts WHERE fingerprint=? AND status='open' AND source=? "
            "ORDER BY id DESC LIMIT 1", (alert.fingerprint, alert.source)).fetchone()
        if row:
            self._conn.execute(
                "UPDATE alerts SET severity=?, message=?, point_name=?, last_seen_utc=? "
                "WHERE id=?",
                (alert.severity, alert.message, alert.point_name, alert.last_seen_utc,
                 row["id"]))
            return int(row["id"]), False
        cur = self._conn.execute(
            "INSERT INTO alerts (kind, severity, system_code, point_id, point_name, "
            " message, fingerprint, source, status, created_utc, last_seen_utc) "
            "VALUES (?,?,?,?,?,?,?,?,'open',?,?)",
            (alert.kind, alert.severity, alert.system_code, alert.point_id,
             alert.point_name, alert.message, alert.fingerprint, alert.source,
             alert.created_utc, alert.last_seen_utc))
        return int(cur.lastrowid), True

    def resolve_missing_alerts(self, active_fingerprints: Sequence[str], now_utc: int,
                               *, source: str = "prescreen") -> list[dict]:
        """Resolve OPEN alerts from THIS source whose condition is no longer present
        (fingerprint not in `active_fingerprints` this cycle). Scoped by source so the
        in-app pre-screen check never clears the external watchdog's alerts (and vice
        versa). Returns the resolved rows so the caller can note the recovery."""
        rows = self._conn.execute(
            "SELECT id, kind, severity, system_code, point_name, message, fingerprint "
            "FROM alerts WHERE status='open' AND source=?", (source,)).fetchall()
        active = set(active_fingerprints)
        resolved = [dict(r) for r in rows if r["fingerprint"] not in active]
        if resolved:
            self._conn.executemany(
                "UPDATE alerts SET status='resolved', resolved_utc=? WHERE id=?",
                [(now_utc, r["id"]) for r in resolved])
        return resolved

    def system_overview(self, system_id: int, *, now_utc: int, recent_s: int = 1800,
                        spark_s: int = 86400, spark_rollup: str = "5m") -> dict:
        """Everything the unit dashboard needs about ONE unit's live points, in a couple
        of indexed queries (the page is one round-trip).

        Latest value comes from the RECENT raw window, not a GROUP BY over all of
        `samples` — there is no (point_id, ts_utc) index, so a per-point MAX would scan
        1.6 M rows. The last `recent_s` (indexed by idx_samples_ts) is a tiny slice, and a
        point with NO row in it is exactly the 'no recent data' state the dashboard must
        show, not hide. Sparklines come from samples_5m (PK-indexed on point_id,bucket).

        Per point: identity + role + units + alarm limits, `last` (newest sample, any
        quality — so a faulted reading shows AS faulted), `last_good` (newest good value —
        the headline number), `recent_n`, and `spark` [[bucket_utc, v_avg], ...]."""
        pts = [dict(r) for r in self._conn.execute(
            "SELECT id, name, display_name, object_type, object_instance, units_text, "
            " role, role_canonical, alarm_enabled, alarm_hi, alarm_lo "
            "FROM points WHERE system_id = ? AND enabled = 1 ORDER BY name, object_instance",
            (system_id,)).fetchall()]
        if not pts:
            return {"points": []}
        ids = [p["id"] for p in pts]
        ph = ",".join("?" * len(ids))

        last: dict[int, dict] = {}
        for r in self._conn.execute(
                f"SELECT point_id, ts_utc, value_num, value_text, quality FROM samples "
                f"WHERE ts_utc >= ? AND point_id IN ({ph}) ORDER BY ts_utc",
                [now_utc - recent_s, *ids]):
            d = last.setdefault(r["point_id"], {"n": 0, "last": None, "last_good": None})
            d["n"] += 1
            d["last"] = {"v": r["value_num"], "vt": r["value_text"],
                         "q": r["quality"], "t": r["ts_utc"]}
            if r["quality"] == "good" and r["value_num"] is not None:
                d["last_good"] = {"v": r["value_num"], "t": r["ts_utc"]}

        table = {"5m": "samples_5m", "1h": "samples_1h"}.get(spark_rollup, "samples_5m")
        spark: dict[int, list] = {}
        for r in self._conn.execute(
                f"SELECT point_id, bucket_utc, v_avg FROM {table} "
                f"WHERE bucket_utc >= ? AND point_id IN ({ph}) "
                f"ORDER BY point_id, bucket_utc", [now_utc - spark_s, *ids]):
            spark.setdefault(r["point_id"], []).append([r["bucket_utc"], r["v_avg"]])

        for p in pts:
            d = last.get(p["id"], {})
            p["recent_n"] = d.get("n", 0)
            p["last"] = d.get("last")
            p["last_good"] = d.get("last_good")
            p["spark"] = spark.get(p["id"], [])
        return {"points": pts}

    def canonical_roles(self) -> list[dict]:
        """The roles that HAVE a canonical enabled point somewhere in the plant, with how
        many units carry each — the menu the fleet-matrix column picker offers, so it can
        only propose columns that will actually have data."""
        return [dict(r) for r in self._conn.execute(
            "SELECT role, COUNT(DISTINCT system_id) AS units FROM points "
            "WHERE enabled = 1 AND role_canonical = 1 AND role IS NOT NULL "
            "AND role != 'OTHER' GROUP BY role ORDER BY role")]

    def fleet_overview(self, roles: Sequence[str], *, now_utc: int,
                       recent_s: int = 1800) -> dict:
        """The building-wide comparison matrix: for every system, the CANONICAL point of
        each key role and its latest value/quality. One row per unit, one column per
        role — the operator's daily 'which of my 14 near-identical AHUs is the odd one'.

        Same freshness discipline as system_overview: latest value from the recent raw
        window (indexed), a point with nothing there reads as no-data rather than a stale
        number. Canonical points only — the curated sensor per role, not a dead twin."""
        if not roles:
            return {"units": []}
        rph = ",".join("?" * len(roles))
        prows = [dict(r) for r in self._conn.execute(
            f"SELECT p.id, p.system_id, p.role, p.units_text, p.name, "
            f" s.code AS system_code FROM points p JOIN systems s ON p.system_id = s.id "
            f"WHERE p.enabled = 1 AND p.role_canonical = 1 AND p.role IN ({rph}) "
            f"ORDER BY s.id", list(roles))]
        ids = [p["id"] for p in prows]
        latest: dict[int, dict] = {}
        if ids:
            iph = ",".join("?" * len(ids))
            for r in self._conn.execute(
                    f"SELECT point_id, ts_utc, value_num, value_text, quality FROM samples "
                    f"WHERE ts_utc >= ? AND point_id IN ({iph}) ORDER BY ts_utc",
                    [now_utc - recent_s, *ids]):
                d = latest.setdefault(r["point_id"], {})
                # newest overall = the headline (any quality); track last-good separately
                d["last"] = {"v": r["value_num"], "vt": r["value_text"],
                             "q": r["quality"], "t": r["ts_utc"]}
                if r["quality"] == "good" and r["value_num"] is not None:
                    d["last_good"] = {"v": r["value_num"], "t": r["ts_utc"]}

        by_sys: dict[Any, dict] = {}
        for p in prows:
            cell = latest.get(p["id"], {})
            by_sys.setdefault(p["system_id"], {})[p["role"]] = {
                "point_id": p["id"], "units": p.get("units_text"),
                "last": cell.get("last"), "last_good": cell.get("last_good")}
        return {"cells_by_system": by_sys}

    def open_alerts(self) -> list[dict]:
        """Currently-active alerts, most-severe + newest first (for the banner / API)."""
        rows = self._conn.execute(
            "SELECT * FROM alerts WHERE status='open' "
            "ORDER BY CASE severity WHEN 'critical' THEN 2 WHEN 'high' THEN 1 ELSE 0 END "
            " DESC, last_seen_utc DESC").fetchall()
        return [dict(r) for r in rows]

    def list_alerts(self, *, status: Optional[str] = None, limit: int = 100) -> list[dict]:
        q = "SELECT * FROM alerts"
        params: list[Any] = []
        if status:
            q += " WHERE status=?"; params.append(status)
        q += " ORDER BY last_seen_utc DESC LIMIT ?"; params.append(limit)
        return [dict(r) for r in self._conn.execute(q, params).fetchall()]

    def alerts_needing_notification(self, source: Optional[str] = None) -> list[dict]:
        """OPEN alerts that have not been notified yet (notified_utc IS NULL), optionally
        from ONE source.

        The alert sender (notify.py) passes source='watchdog'. The pre-screen's rows are not
        news: two
        of them sat open from 2026-09-15, orphaned when the pre-screen was switched off, and
        would have gone out as brand-new alerts the moment a webhook was configured."""
        q = "SELECT * FROM alerts WHERE status='open' AND notified_utc IS NULL"
        params: list[Any] = []
        if source is not None:
            q += " AND source=?"
            params.append(source)
        return [dict(r) for r in self._conn.execute(q + " ORDER BY id", params).fetchall()]

    def mark_alerts_notified(self, ids: Sequence[int], now_utc: int) -> None:
        if ids:
            self._conn.executemany("UPDATE alerts SET notified_utc=? WHERE id=?",
                                   [(now_utc, i) for i in ids])

    def resolved_alerts_since(self, source: str, after_utc: int) -> list[dict]:
        """Alerts from `source` that were ANNOUNCED (notified_utc set) and have resolved
        after `after_utc`: the all-clear messages still owed. A condition that cleared
        before it was ever sent (held back by the debounce) is deliberately absent -- no
        alarm went out, so no all-clear is due."""
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM alerts WHERE source=? AND status='resolved' "
            "AND notified_utc IS NOT NULL AND resolved_utc > ? ORDER BY resolved_utc, id",
            (source, int(after_utc))).fetchall()]

    # ── app_meta: small durable key/value state ──────────────────────────────
    # Used for notification bookkeeping (last delivery, today's check-in, the all-clear
    # watermark). It has to be durable: the watchdog is a fresh process every five minutes.
    def get_meta(self, key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row["value"]

    def set_meta(self, key: str, value: Any) -> None:
        self._conn.execute(
            "INSERT INTO app_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, None if value is None else str(value)))

    def meta_with_prefix(self, prefix: str) -> dict[str, Optional[str]]:
        return {r["key"]: r["value"] for r in self._conn.execute(
            "SELECT key, value FROM app_meta WHERE substr(key, 1, ?) = ?",
            (len(prefix), prefix))}

    def recent_reports(self, kind: str, *, before_utc: Optional[int] = None,
                       limit: int = 2) -> list[dict]:
        """The last N COMPLETE reports of a kind — fed back into the next report's
        context so the model builds on its own history instead of starting cold
. Excludes error stubs."""
        sql = ["SELECT id, kind, period_start_utc, period_end_utc, prose, created_utc "
               "FROM reports WHERE kind = ? AND status = 'complete'"]
        params: list[Any] = [kind]
        if before_utc is not None:
            sql.append("AND created_utc < ?"); params.append(before_utc)
        sql.append("ORDER BY created_utc DESC LIMIT ?"); params.append(limit)
        return [dict(r) for r in self._conn.execute(" ".join(sql), params).fetchall()]

    def last_report_utc(self, kind: str) -> Optional[int]:
        """When a report of this kind was last ATTEMPTED, whatever the outcome.

        Deliberately NOT filtered to status='complete' like recent_reports(): this answers
        "has this scheduled slot been served?", and a provider failure has served it. If
        error stubs did not count, a bad API key would make the scheduler retry every poll
        cycle -- a billed call every two minutes."""
        r = self._conn.execute(
            "SELECT MAX(created_utc) AS m FROM reports WHERE kind = ?", (kind,)).fetchone()
        return None if r is None or r["m"] is None else int(r["m"])

    # ── health scores (ai/health.py writes; /api/health + status tab read) ─────
    def insert_health_scores(self, snapshot: dict, *, report_id: Optional[int],
                             ts_utc: int) -> int:
        """Persist one health snapshot: a building row + one row per system. `detail`
        holds the full JSON (deductions) so the score stays explainable. Returns the
        number of rows written."""
        code_to_sysid = {s["code"]: s["id"] for s in self.list_systems()}
        b = snapshot.get("building") or {}
        self._conn.execute(
            "INSERT INTO health_scores (report_id, scope, system_id, system_code, "
            " score, grade, detail, ts_utc) VALUES (?,?,?,?,?,?,?,?)",
            (report_id, "building", None, None,
             float(b.get("score", 100.0)), b.get("grade"), json.dumps(b), ts_utc))
        n = 1
        for r in snapshot.get("systems") or []:
            self._conn.execute(
                "INSERT INTO health_scores (report_id, scope, system_id, system_code, "
                " score, grade, detail, ts_utc) VALUES (?,?,?,?,?,?,?,?)",
                (report_id, "system", code_to_sysid.get(r.get("code")), r.get("code"),
                 float(r.get("score", 100.0)), r.get("grade"), json.dumps(r), ts_utc))
            n += 1
        return n

    def latest_health(self) -> dict:
        """Most recent health snapshot — latest building row + the latest row per
        system (by id, monotonic). Detail JSON is decoded back to the health dicts."""
        b = self._conn.execute(
            "SELECT * FROM health_scores WHERE scope='building' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        sysrows = self._conn.execute(
            "SELECT * FROM health_scores WHERE scope='system' AND id IN "
            " (SELECT MAX(id) FROM health_scores WHERE scope='system' GROUP BY system_code) "
            "ORDER BY score ASC, system_code").fetchall()
        if not b and not sysrows:
            return {}
        building = (json.loads(b["detail"]) if b and b["detail"]
                    else ({"score": b["score"], "grade": b["grade"]} if b else {}))
        systems = [json.loads(r["detail"]) if r["detail"]
                   else {"code": r["system_code"], "score": r["score"], "grade": r["grade"]}
                   for r in sysrows]
        ts = b["ts_utc"] if b else (sysrows[0]["ts_utc"] if sysrows else None)
        return {"building": building, "systems": systems, "ts_utc": ts}

    def health_history(self, system_code: Optional[str] = None, *,
                       scope: str = "building", limit: int = 90) -> list[dict]:
        """Score trend (newest first) for the status tab — building rollup, or one
        system when system_code is given."""
        if system_code:
            rows = self._conn.execute(
                "SELECT ts_utc, score, grade FROM health_scores "
                "WHERE scope='system' AND system_code=? ORDER BY ts_utc DESC LIMIT ?",
                (system_code, limit)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT ts_utc, score, grade FROM health_scores "
                "WHERE scope=? ORDER BY ts_utc DESC LIMIT ?", (scope, limit)).fetchall()
        return [dict(r) for r in rows]

    def equipment_meta(self, system_id: Optional[int] = None) -> list[dict]:
        """Design intent / setpoints. Optionally scoped to one system."""
        if system_id is None:
            rows = self._conn.execute("SELECT * FROM equipment_meta").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM equipment_meta WHERE system_id=? OR system_id IS NULL",
                (system_id,)).fetchall()
        return [dict(r) for r in rows]

    def bad_quality_mix(self, point_id: int, start_utc: int, end_utc: int) -> dict:
        """{quality: count} of one point's readings that were not good, in [start, end)."""
        return {r["quality"]: r["n"] for r in self._conn.execute(
            "SELECT quality, count(*) AS n FROM samples WHERE point_id = ? AND ts_utc >= ? "
            "AND ts_utc < ? AND quality != 'good' GROUP BY quality",
            (int(point_id), int(start_utc), int(end_utc)))}

    def poll_health_history(self, device_instance: int, since_utc: int,
                            until_utc: int) -> list[dict]:
        """One device's poll cycles from `since_utc` to `until_utc`, newest first (the
        watchdog's "how long have some of its points been missing")."""
        rows = self._conn.execute(
            "SELECT p.ts_utc, p.ok, p.reads_attempted, p.reads_ok FROM poll_health p "
            "JOIN devices d ON d.id = p.device_id WHERE d.device_instance = ? "
            "AND p.ts_utc BETWEEN ? AND ? ORDER BY p.ts_utc DESC",
            (int(device_instance), int(since_utc), int(until_utc))).fetchall()
        return [dict(r) for r in rows]

    def unanswered_points(self, device_instance: int, ts_utc: int) -> list[dict]:
        """The device's logged points whose reading in the cycle stamped `ts_utc` got no
        answer (comm-loss)."""
        rows = self._conn.execute(
            "SELECT p.id, p.name, p.display_name, s.code AS system_code "
            "FROM points p JOIN devices d ON d.id = p.device_id "
            "JOIN samples x ON x.point_id = p.id AND x.ts_utc = ? "
            "LEFT JOIN systems s ON s.id = p.system_id "
            "WHERE d.device_instance = ? AND x.quality = 'comm-loss' "
            "ORDER BY p.display_name IS NULL, s.code, p.display_name, p.name", (int(ts_utc), int(device_instance))).fetchall()
        return [dict(r) for r in rows]

    def poll_health_latest(self) -> list[dict]:
        """Most recent poll-cycle result per device (+ device identity) — the
        'do I trust the data' view. One row per device that has ever polled."""
        rows = self._conn.execute(
            "SELECT d.device_instance, d.name, d.address, p.ts_utc, p.ok, "
            " p.latency_ms, p.reads_attempted, p.reads_ok, p.method, p.error "
            "FROM devices d JOIN poll_health p ON p.id = ("
            "  SELECT id FROM poll_health WHERE device_id = d.id "
            "  ORDER BY ts_utc DESC LIMIT 1) "
            "ORDER BY d.device_instance"
        ).fetchall()
        return [dict(r) for r in rows]

    # ── rule-based alarm incidents (rules/live.py) ────────────────────────────
    def has_table(self, name: str) -> bool:
        return self._conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                  (name,)).fetchone() is not None

    def open_incidents(self) -> list[dict]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM incidents WHERE status = 'open' ORDER BY opened_utc")]

    def incident_overlaps(self, fingerprint: str, start_utc: int,
                          end_utc: Optional[int]) -> bool:
        """Is this episode already on record? The live run re-reads the last four hours every
        five minutes, so after an incident closes, the next runs still see its tail -- and
        each one starts it wherever its window now starts. Matching the exact start let every
        one of those in: 263 of the first 313 incidents were such copies (found 2026-10-14).
        An episode that overlaps a recorded one of the same fingerprint IS that one."""
        end = int(end_utc) if end_utc is not None else 2 ** 62
        return self._conn.execute(
            "SELECT 1 FROM incidents WHERE fingerprint = ? AND started_utc <= ? "
            "AND (resolved_utc IS NULL OR resolved_utc >= ?) LIMIT 1",
            (fingerprint, end, int(start_utc))).fetchone() is not None

    def incident_seen(self, fingerprint: str, started_utc: int) -> bool:
        """Is this episode already on record (open or resolved)? The live window overlaps the
        last run's, so an episode that opened and closed between runs arrives twice."""
        return self._conn.execute(
            "SELECT 1 FROM incidents WHERE fingerprint = ? AND started_utc = ?",
            (fingerprint, int(started_utc))).fetchone() is not None

    def insert_incident(self, row: dict) -> int:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        return int(self._conn.execute(
            f"INSERT INTO incidents ({cols}) VALUES ({marks}) RETURNING id",
            tuple(row.values())).fetchone()[0])

    _INCIDENT_FIELDS = frozenset({
        "tier", "status", "detail", "last_breach_utc", "resolved_utc", "resolve_reason",
        "peak", "occurrences", "group_key", "notified_utc", "notified_tier",
        "cleared_notified_utc", "feedback", "feedback_utc", "updated_utc"})

    def update_incident(self, incident_id: int, **fields) -> None:
        bad = set(fields) - self._INCIDENT_FIELDS
        if bad:
            raise ValueError(f"not an updatable incident field: {sorted(bad)}")
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            self._conn.execute(f"UPDATE incidents SET {sets} WHERE id = ?",
                               (*fields.values(), int(incident_id)))

    def incidents_to_push(self) -> list[dict]:
        """Open incidents not yet pushed at their current tier: never pushed, or pushed at a
        lower one (a tier only ever rises). Which tiers go out at all is notify's policy. A row
        pushed before notified_tier existed counts as pushed at its tier (NULL <> tier is not
        true): AC-6's of 2026-10-02 is one."""
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM incidents WHERE status = 'open' "
            "AND (notified_utc IS NULL OR notified_tier <> tier) ORDER BY opened_utc")]

    def incidents_never_pushed(self, resolved_after_utc: int) -> list[dict]:
        """Resolved incidents that were never pushed and ended at or after
        `resolved_after_utc`: an episode that began and ended before the rules next ran
        (notify's "came and went"). The tier filter is notify's policy, as for the others."""
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM incidents WHERE status = 'resolved' AND notified_utc IS NULL "
            "AND resolved_utc >= ? ORDER BY started_utc", (int(resolved_after_utc),))]

    def incident_clears_to_push(self) -> list[dict]:
        """Resolved incidents that WERE pushed and whose all-clear has not been."""
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM incidents WHERE status = 'resolved' AND notified_utc IS NOT NULL "
            "AND cleared_notified_utc IS NULL ORDER BY resolved_utc")]

    def incidents_between(self, t0: int, t1: int) -> list[dict]:
        """Every incident whose breach overlapped [t0, t1], open or not: what a summary of
        that window narrates (ai/summary.py). The incidents table is small (dozens a week)."""
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM incidents WHERE started_utc <= ? "
            "AND (resolved_utc IS NULL OR resolved_utc >= ?) ORDER BY started_utc",
            (int(t1), int(t0)))]

    def watchdog_alerts_between(self, t0: int, t1: int) -> list[dict]:
        """The watchdog's own alerts (data stopped, a controller silent or failing, points
        missing) that were open at any time in [t0, t1]."""
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM alerts WHERE source = 'watchdog' AND created_utc <= ? "
            "AND (resolved_utc IS NULL OR resolved_utc >= ?) ORDER BY created_utc",
            (int(t1), int(t0)))]

    def summary_units(self) -> list[dict]:
        """Every unit with its kind, operator notes and occupancy mode (ai/summary.py)."""
        return [dict(r) for r in self._conn.execute(
            "SELECT id, code, name, kind, notes, occupancy_mode FROM systems ORDER BY id")]

    def incidents_since(self, t0: int) -> list[dict]:
        """Open now, or opened / resolved since t0: what a digest reports."""
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM incidents WHERE status = 'open' OR opened_utc >= ? "
            "OR resolved_utc >= ? ORDER BY opened_utc", (int(t0), int(t0)))]

    # ── who is in control of each commandable point (bacnet/priority.py) ─────
    def command_targets(self, types: Iterable[str]) -> list[dict]:
        """The priority read's work-list: every enabled BACnet point of a commandable type,
        with its device address, except those whose device already said they have no
        priority array (point_command.has_array = 0)."""
        kinds = sorted(set(types))
        marks = ", ".join("?" for _ in kinds)
        rows = self._conn.execute(
            "SELECT p.id AS point_id, p.object_type, p.object_instance, d.id AS device_id, "
            " d.address FROM points p JOIN devices d ON p.device_id = d.id "
            "LEFT JOIN point_command c ON c.point_id = p.id "
            f"WHERE p.enabled = 1 AND p.source = 'bacnet' AND p.object_type IN ({marks}) "
            " AND d.address IS NOT NULL AND (c.has_array IS NULL OR c.has_array = 1) "
            "ORDER BY d.id, p.object_type, p.object_instance", tuple(kinds)).fetchall()
        return [dict(r) for r in rows]

    def record_commands(self, ts_utc: int, readings: list, *,
                        log_values_at_or_below: int = 8) -> dict:
        """Store one priority read (collector/service.py). Each reading is {point_id,
        has_array, level, value_num, value_text}. A point whose read FAILED is simply
        absent: its last known state stands and its checked_utc ages.

        `since_utc` is when the current LEVEL was first seen: the program moving a valve at
        level 16 is not news, an operator taking it over at 8 is. The log keeps every level
        change and every value change at an override level (an operator moving their own
        override from 58 to 60), not the program's own value changes, which are the present
        value's trend already. Returns counts."""
        ts = int(ts_utc)
        out = {"read": 0, "changed": 0, "no_array": 0}
        with self._tx():
            cur = {r["point_id"]: dict(r) for r in self._conn.execute(
                "SELECT * FROM point_command")}
            for r in readings:
                pid = int(r["point_id"])
                old = cur.get(pid)
                if not r.get("has_array", True):
                    out["no_array"] += 1
                    self._conn.execute(
                        "INSERT INTO point_command (point_id, has_array, checked_utc) "
                        "VALUES (?, 0, ?) ON CONFLICT(point_id) DO UPDATE SET has_array = 0, "
                        "level = NULL, value_num = NULL, value_text = NULL, since_utc = NULL, "
                        "checked_utc = excluded.checked_utc", (pid, ts))
                    continue
                out["read"] += 1
                lvl, num, txt = r.get("level"), r.get("value_num"), r.get("value_text")
                if old is None or not old["has_array"]:
                    self._conn.execute(
                        "INSERT INTO point_command (point_id, has_array, level, value_num, "
                        " value_text, since_utc, since_exact, checked_utc) "
                        "VALUES (?, 1, ?, ?, ?, ?, 0, ?) ON CONFLICT(point_id) DO UPDATE SET "
                        " has_array = 1, level = excluded.level, value_num = excluded.value_num, "
                        " value_text = excluded.value_text, since_utc = excluded.since_utc, "
                        " since_exact = 0, checked_utc = excluded.checked_utc",
                        (pid, lvl, num, txt, ts, ts))
                    self._log_command(pid, ts, lvl, num, txt)
                    continue
                level_changed = old["level"] != lvl
                value_changed = (old["value_num"], old["value_text"]) != (num, txt)
                if level_changed:
                    self._conn.execute(
                        "UPDATE point_command SET level = ?, value_num = ?, value_text = ?, "
                        "since_utc = ?, since_exact = 1, checked_utc = ? WHERE point_id = ?",
                        (lvl, num, txt, ts, ts, pid))
                else:
                    self._conn.execute(
                        "UPDATE point_command SET value_num = ?, value_text = ?, "
                        "checked_utc = ? WHERE point_id = ?", (num, txt, ts, pid))
                if level_changed or (value_changed and lvl is not None
                                     and lvl <= log_values_at_or_below):
                    out["changed"] += 1
                    self._log_command(pid, ts, lvl, num, txt)
        return out

    def _log_command(self, pid: int, ts: int, lvl: Any, num: Any, txt: Any) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO point_command_log (point_id, ts_utc, level, value_num, "
            "value_text) VALUES (?, ?, ?, ?, ?)", (pid, ts, lvl, num, txt))

    def command_state(self) -> list[dict]:
        """Every logged commandable point with a priority array: who is in control of it
        (level, value, since) and what it is (name, role, its unit and the unit's kind)."""
        rows = self._conn.execute(
            "SELECT c.point_id, c.level, c.value_num, c.value_text, c.since_utc, "
            " c.since_exact, c.checked_utc, p.name, p.display_name, p.object_type, "
            " p.units_text, p.role, p.role_canonical, p.system_id, s.code AS system_code, "
            " s.kind AS system_kind "
            "FROM point_command c JOIN points p ON p.id = c.point_id "
            "LEFT JOIN systems s ON s.id = p.system_id "
            "WHERE c.has_array = 1 AND p.enabled = 1").fetchall()
        return [dict(r) for r in rows]

    def overrides(self, max_level: int = 8, now_utc: Optional[int] = None) -> list[dict]:
        """Logged points overridden right now, oldest first: at level 1..max_level, more
        important than the same point's usual level on the other units, and not a schedule
        cycling there (bacnet/priority.overrides says why each test is needed). An occupancy
        request held by a thermostat's override button is after-hours use, not an override
        (after_hours_sessions)."""
        from ..bacnet import priority
        rows = self.command_state()
        logs: dict = {}
        for r in rows:
            if priority.is_override(r["level"], max_level):
                logs[r["point_id"]] = [(x["value_num"], x["value_text"]) for x in self._conn.execute(
                    "SELECT value_num, value_text FROM point_command_log WHERE point_id = ? "
                    "AND level = ? AND ts_utc >= ? ORDER BY ts_utc",
                    (r["point_id"], r["level"], int(r["since_utc"] or 0)))]
        now = int(time.time()) if now_utc is None else int(now_utc)
        return [r for r in priority.overrides(rows, logs, max_level)
                if not priority.is_occupancy_button(r, now)]

    def after_hours_sessions(self, since_utc: int, until_utc: int, *,
                             max_level: int = 8) -> list[dict]:
        """Times a unit ran outside its schedule because its occupancy request was held
        occupied at an override level -- a tenant's thermostat override button, most often.
        2026-10-15: "list them separately as after hours use".

        A session runs from the read that saw the request go to 1..max_level occupied to the
        read that saw it go back. It counts when the unit was unoccupied just before it began
        (`started_off`: the press turned the unit on) or when the request fell back to
        unoccupied (`ran_past_schedule`: the press kept it on after the schedule ended).
        Presses during scheduled hours change nothing and are left out. Times are as fine
        as the priority read (bacnet.poll.priority_interval_s). Sessions overlapping
        [since_utc, until_utc), oldest first."""
        reqs = self._conn.execute(
            "SELECT p.id, p.system_id, s.code AS system_code FROM points p "
            "JOIN systems s ON s.id = p.system_id WHERE p.enabled = 1 AND p.role = 'OCCUPANCY' "
            "AND p.object_type IN ('multi-state-output', 'multi-state-value')").fetchall()
        status = {r["system_id"]: r["id"] for r in self._conn.execute(
            "SELECT id, system_id FROM points WHERE enabled = 1 AND role = 'OCCUPANCY' "
            "AND object_type = 'multi-state-input'")}
        out: list[dict] = []
        for p in reqs:
            sessions: list[dict] = []
            cur: Optional[dict] = None
            first = True
            for r in self._conn.execute(
                    "SELECT ts_utc, level, value_num FROM point_command_log WHERE point_id = ? "
                    "AND ts_utc < ? ORDER BY ts_utc", (p["id"], int(until_utc))):
                on = (r["level"] is not None and 1 <= r["level"] <= max_level
                      and r["value_num"] == 1)
                if on and cur is None:
                    cur = {"start": r["ts_utc"], "start_known": not first}
                elif not on and cur is not None:
                    cur.update(end=r["ts_utc"], released_to=r["value_num"])
                    sessions.append(cur)
                    cur = None
                first = False
            if cur is not None:
                cur.update(end=None, released_to=None)
                sessions.append(cur)
            sid = status.get(p["system_id"])
            for s in sessions:
                if s["start"] >= until_utc or (s["end"] is not None and s["end"] <= since_utc):
                    continue
                started_off = sid is not None and self._conn.execute(
                    "SELECT 1 FROM samples WHERE point_id = ? AND ts_utc BETWEEN ? AND ? "
                    "AND value_num = 2 LIMIT 1",
                    (sid, int(s["start"]) - 900, int(s["start"]))).fetchone() is not None
                past = s["released_to"] == 2
                if not (started_off or past):
                    continue                    # during scheduled hours: it changed nothing
                out.append(dict(s, system_code=p["system_code"], point_id=p["id"],
                                started_off=started_off,
                                ran_past_schedule=past and not started_off))
        out.sort(key=lambda s: (s["start"], s["system_code"]))
        return out

    def rule_points(self) -> list[dict]:
        """Every LOGGED point on a unit, with what the alarm rules need to judge it: role,
        canonical flag, type, units, and the unit's code, kind and occupancy mode
        (rules/evaluate.py). One pass over `points`; no sample table is touched."""
        rows = self._conn.execute(
            "SELECT p.id, p.name, p.display_name, p.role, p.role_canonical, p.object_type, "
            " p.units_text, p.enabled, s.code AS system_code, s.kind AS system_kind, "
            " s.occupancy_mode "
            "FROM points p JOIN systems s ON s.id = p.system_id WHERE p.enabled = 1").fetchall()
        return [dict(r) for r in rows]

    def coverage_points(self) -> list[dict]:
        """Every point with its system and role, for the point-coverage review
        (coverage.py). One pass over `points` (~12.5k rows); no sample table is touched."""
        rows = self._conn.execute(
            "SELECT p.id, s.code AS system_code, s.kind AS system_kind, p.role, p.enabled, "
            " p.object_type, p.name, p.display_name "
            "FROM points p LEFT JOIN systems s ON s.id = p.system_id").fetchall()
        return [dict(r) for r in rows]

    def poll_health_between(self, t0: int, t1: int) -> list[dict]:
        """Per-device poll results over [t0, t1): cycles, failed cycles, reads attempted and
        read, and the device's FIRST poll ever (a controller added mid-day is judged from
        then, not charged with the hours before it existed). One device at a time through
        idx_poll_health_device -- measured 6 ms for a day on the live database; the first
        poll is a MIN on the same index. Devices with no row in the window are absent."""
        rows = self._conn.execute(
            "SELECT d.device_instance, d.name, COUNT(p.id) AS cycles, "
            " SUM(p.ok) AS ok_cycles, SUM(p.reads_ok) AS reads_ok, "
            " SUM(p.reads_attempted) AS reads_attempted, "
            " (SELECT MIN(q.ts_utc) FROM poll_health q WHERE q.device_id = d.id) "
            "   AS first_poll_utc "
            "FROM devices d JOIN poll_health p ON p.device_id = d.id "
            " AND p.ts_utc >= ? AND p.ts_utc < ? "
            "GROUP BY d.id ORDER BY d.device_instance", (int(t0), int(t1))).fetchall()
        return [dict(r) for r in rows]

    def poll_cycle_times(self, t0: int, t1: int) -> list[int]:
        """Distinct poll-cycle timestamps in [t0, t1), ascending. The spaces between them are
        the recording gaps: restarts, reboots, a hung collector. (12 ms for a day.)"""
        return [int(r[0]) for r in self._conn.execute(
            "SELECT DISTINCT ts_utc FROM poll_health WHERE ts_utc >= ? AND ts_utc < ? "
            "ORDER BY ts_utc", (int(t0), int(t1)))]

    def samples_count_between(self, t0: int, t1: int) -> int:
        """Raw samples stored in [t0, t1). A range on idx_samples_ts, NOT on the clustered
        key -- 25 ms for a day (478,080 rows) on the live database."""
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM samples WHERE ts_utc >= ? AND ts_utc < ?",
            (int(t0), int(t1))).fetchone()[0])

    def weather_last_fetched(self) -> Optional[int]:
        """When weather was last WRITTEN (created_utc), not the newest observation time --
        a forecast source can hold future ts_utc rows, which would read as fresh forever.
        The table is ~24 rows a day, so the scan is trivial."""
        row = self._conn.execute("SELECT MAX(created_utc) FROM weather").fetchone()
        return None if row is None or row[0] is None else int(row[0])

    # ── row mappers ──────────────────────────────────────────────────────────
    @staticmethod
    def _row_to_sample(r: sqlite3.Row) -> Sample:
        return Sample(
            point_id=r["point_id"], ts_utc=r["ts_utc"],
            value_num=r["value_num"], value_text=r["value_text"],
            status_flags=r["status_flags"], reliability=r["reliability"],
            out_of_service=None if r["out_of_service"] is None else bool(r["out_of_service"]),
            quality=Quality(r["quality"]),
        )

    @staticmethod
    def _row_to_point(r: sqlite3.Row) -> Point:
        return Point(
            id=r["id"], device_id=r["device_id"], system_id=r["system_id"],
            source=PointSource(r["source"]),
            object_type=r["object_type"], object_instance=r["object_instance"],
            name=r["name"], display_name=r["display_name"], description=r["description"],
            units=r["units"], units_text=r["units_text"],
            poll_interval_s=r["poll_interval_s"],
            cov_enabled=bool(r["cov_enabled"]),
            trendlog_harvest=bool(r["trendlog_harvest"]),
            enabled=bool(r["enabled"]),
            alarm_enabled=bool(r["alarm_enabled"]),
            alarm_hi=r["alarm_hi"], alarm_lo=r["alarm_lo"],
            alarm_deadband=r["alarm_deadband"],
            spec=r["spec"],
        )


def samples_extent_readonly(db_path: str | Path) -> dict:
    """samples_extent() on its OWN read-only connection, for a worker thread.

    The exact COUNT(*) walks all of idx_samples_ts. It was judged fine on the event loop at
    16.6 M rows (0.352 s). MEASURED 2026-10-20 at 43.8 M rows: 10.3 s cold and 0.69 s warm in
    a normal process, and 12-23 s from the server, which runs as a low-priority scheduled
    task. /api/poll-health ran it on the loop the BACnet collector polls from. The collector's
    connection is bound to that thread, so the worker opens its own, the same pattern as the
    backup: WAL admits a second reader, and sqlite3 releases the GIL while it steps."""
    dal = SQLiteDAL(db_path, read_only=True)
    try:
        return dal.samples_extent()
    finally:
        dal.close()


def _bool_or_none(v: Optional[bool]) -> Optional[int]:
    return None if v is None else int(v)


def _cli_init_db(argv: Optional[list[str]] = None) -> int:
    """Console entry point (hvactrend-initdb): create a DB from schema.sql."""
    import argparse

    parser = argparse.ArgumentParser(description="Initialize the HVACTrend SQLite DB.")
    parser.add_argument("--db", default="data/hvactrend.db", help="output DB path")
    parser.add_argument("--schema", default=str(_DEFAULT_SCHEMA), help="schema.sql path")
    args = parser.parse_args(argv)

    dal = SQLiteDAL(args.db, args.schema)
    dal.apply_schema()
    dal.close()
    print(f"Initialized {args.db} from {args.schema}")
    return 0
