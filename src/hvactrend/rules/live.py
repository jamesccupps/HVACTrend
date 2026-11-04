"""Keep the incidents table in step with the rules: Phase 1's detection step, run by the watchdog.

Every run (every 5 minutes, scripts/watchdog.py) evaluates the last few hours, SEEDED with the
incidents already open: an ongoing condition continues its incident instead of opening a
second one, however long it lasts. Then it writes what changed. Delivery is a separate step
(notify.run_incidents), so a failure to send never changes what was recorded.

It lives in the watchdog, not the server, for two reasons: the analysis is then off the
polling loop by construction, and a hung server cannot also silence it.
"""

from __future__ import annotations

import time
from typing import Any

from . import catalog
from .engine import TIER_ORDER, Episode
from .evaluate import evaluate
from .group import group

WINDOW_S = 4 * 3600          # covers the longest tier (50 min) + clear (30) with room to spare
FIRST_WINDOW_S = 7 * 86400   # the very first run looks back a week, so a condition that has
                             # been going on for days starts from when it began, not 4 h ago
IDLE_CLOSE_S = 3 * 86400     # an incident nobody can judge for this long closes itself
GROUP_LOOKBACK_S = 2 * 3600


def _enabled(config: Any) -> bool:
    cfg = config.get("rules") if config else None
    cfg = cfg.as_dict() if hasattr(cfg, "as_dict") else (cfg or {})
    return bool(cfg.get("enabled", True))


def _seed(row: dict) -> Episode:
    return Episode(start=int(row["started_utc"]), opened=int(row["opened_utc"]),
                   tier=row["tier"], peak=row.get("peak"),
                   last_breach=row.get("last_breach_utc"),
                   occurrences=int(row.get("occurrences") or 1))


def run(dal: Any, config: Any, now_utc: int) -> dict:
    """Evaluate the rules and record the incidents. Returns what changed."""
    out: dict = {"new": [], "updated": 0, "resolved": 0, "escalated": 0, "skipped": None}
    if not _enabled(config):
        out["skipped"] = "rules.enabled is false"
        return out
    if not dal.has_table("incidents"):
        out["skipped"] = "no incidents table yet (the server creates it on its next start)"
        return out
    now = int(now_utc)
    rows = {r["fingerprint"]: r for r in dal.open_incidents()}
    seeds = {fp: _seed(r) for fp, r in rows.items()}
    # Since the last run as well: after a gap (the watchdog not running) a fixed 4 h left an
    # episode that began and ended inside it unrecorded, so never pushed nor in the check-in
    # (2026-10-27 review; BLR-3's #200 on 10-09 was caught only because the gap was short).
    last = dal.get_meta("rules.last_run_utc")
    window = (FIRST_WINDOW_S if not last
              else min(FIRST_WINDOW_S, max(WINDOW_S, now - int(last) + WINDOW_S)))
    errors: list = []
    found = evaluate(dal, config.get("rules"), now - window, now,
                     knowledge=config.get("site_knowledge"), now=now, seeds=seeds, errors=errors)
    touched = set()
    for f in sorted(found, key=lambda f: f.episode.start):
        e, fp = f.episode, f.fingerprint
        if fp in seeds and e is seeds[fp]:                 # the open incident, continued
            touched.add(fp)
            row = rows[fp]
            # occurrences too: the seed reloads it each run, so a recurrence that was not
            # written back was counted for one run and then forgotten (2026-10-27 review)
            upd: dict = {"detail": f.detail, "last_breach_utc": e.last_breach,
                         "peak": e.peak, "occurrences": e.occurrences, "updated_utc": now}
            if TIER_ORDER[e.tier] > TIER_ORDER[row["tier"]]:
                upd["tier"] = e.tier
                out["escalated"] += 1
            if e.end is not None:
                upd.update(status="resolved", resolved_utc=e.end, resolve_reason=e.reason)
                out["resolved"] += 1
            dal.update_incident(row["id"], **upd)
            out["updated"] += 1
            continue
        if dal.incident_overlaps(fp, e.start, e.end):    # a run before this one recorded it
            continue
        iid = dal.insert_incident({
            "rule": f.rule, "system_code": f.unit, "point_key": f.key,
            "point_label": f.point, "fingerprint": fp, "tier": e.tier,
            "status": "resolved" if e.end is not None else "open",
            "title": f.title, "detail": f.detail, "started_utc": e.start,
            "opened_utc": e.opened, "last_breach_utc": e.last_breach,
            "resolved_utc": e.end, "resolve_reason": e.reason or None, "peak": e.peak,
            "occurrences": e.occurrences, "updated_utc": now})
        out["new"].append(iid)
    # an open incident its rule could not evaluate at all (a point switched off, a unit set
    # vacant) is never continued; after the idle time it closes instead of lingering forever
    for fp, row in rows.items():
        if fp in touched:
            continue
        ref = int(row.get("last_breach_utc") or row["opened_utc"])
        if now - ref > IDLE_CLOSE_S:
            dal.update_incident(row["id"], status="resolved", resolved_utc=ref,
                                resolve_reason="not judged", updated_utc=now)
            out["resolved"] += 1
    _assign_groups(dal, now)
    if errors:                       # the rest ran; say which did not (the check-in reports it)
        out["errors"] = errors
        dal.set_meta("rules.last_error", (f"{now} {errors[0]}" + (
            f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""))[:500])
    dal.set_meta("rules.last_run_utc", now)
    return out


class _Row:
    """A stored incident dressed as a Finding for group()."""
    def __init__(self, r: dict):
        self.id, self.unit, self.tier, self.title = r["id"], r["system_code"], r["tier"], r["title"]
        self.detail = r.get("detail") or ""
        self.episode = Episode(start=r["started_utc"], opened=r["opened_utc"], tier=r["tier"])


def _assign_groups(dal: Any, now: int) -> None:
    """Incidents that opened within half an hour on three or more units share a group_key,
    so the push (and the check-in) carry one cause as one message."""
    recent = [r for r in dal.incidents_since(now - GROUP_LOOKBACK_S)
              if r["opened_utc"] >= now - GROUP_LOOKBACK_S]
    for g in group([_Row(r) for r in recent]):
        if g.kind != "building":
            continue
        key = f"building:{g.opened}"
        for f in g.findings:
            dal.update_incident(f.id, group_key=key)


def describe_open(dal: Any) -> list:
    """Open incidents, worst first, as short lines (the check-in's Equipment section)."""
    rows = dal.open_incidents() if dal.has_table("incidents") else []
    rows.sort(key=lambda r: (-TIER_ORDER.get(r["tier"], 0), r["opened_utc"]))
    return [f"{r['system_code']}: {catalog.name(r)} "
            f"(since {time.strftime('%a %m-%d %H:%M', time.localtime(r['started_utc']))})"
            for r in rows]
