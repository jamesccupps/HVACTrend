"""The daily check-in: yesterday in one short card, built from the database and the disk.

Sent every morning by the watchdog (notify.run). It has two jobs:

  * a quick read on yesterday: poll cycles, reads, gaps in the record, backups, snapshots,
    rollups, weather, disk;
  * a DEAD-MAN'S SWITCH. When the box is off, hung or asleep, nothing on it can report that,
    but a check-in that fails to arrive does. The operator chose the daily check-in over "only when
    something's wrong" on 2026-09-29 for exactly this reason.

So it must ALWAYS go out: every section is fenced, and a section that fails to read says so
in the card instead of taking the card down with it.

Everything is a read, and every query is bounded by an index -- poll_health by
(device_id, ts_utc), samples by idx_samples_ts. Measured on the live database for one day:
6, 12 and 25 ms.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Optional

from . import coverage, pollrate
from .bacnet import priority
from .notify import incident_name
from .archives import (newest_archive_mtime, newest_db_backup, newest_offsite_copy,
                       offsite_name, snapshot_dests)

# What counts as "to look at". Deliberately not twitchy: a check-in that cries wolf trains
# its reader to skim it, and then it fails at the one job only it can do.
_MIN_CYCLE_SHARE = 0.98          # fewer than 98 % of the expected poll cycles
_MIN_READ_SHARE = 0.995          # a controller answering fewer than 99.5 % of its reads
_MAX_FAILED_CYCLE_SHARE = 0.02   # more than 2 % of a controller's cycles failed outright
_GAP_S = 600                     # a hole in the record longer than 10 minutes
_GRACE_S = 86400                 # an archive more than a day past its due time
_ROLLUP_LAG_S = 3 * 3600         # rollups are built hourly; 3 h behind means they stopped
_WEATHER_LAG_S = 6 * 3600        # weather is fetched hourly
_DISK_FLOOR = 20e9               # used when storage.backup.min_free_bytes is not set


def _get(config: Any, *path: str, default: Any = None) -> Any:
    node = config
    for k in path:
        if node is None or not hasattr(node, "get"):
            return default
        node = node.get(k)
    if node is None:
        return default
    return node.as_dict() if hasattr(node, "as_dict") else node


def yesterday(now_utc: int) -> "tuple[int, int]":
    """[local midnight yesterday, local midnight today) -- 23 or 25 h across a DST change."""
    lt = time.localtime(now_utc)
    t1 = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    t0 = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - 1, 0, 0, 0, 0, 0, -1)))
    return t0, t1


def _ago(s: float) -> str:
    s = max(0, int(s))
    if s < 90:
        return f"{s} s" if s < 60 else "1 min"
    if s < 3600:
        return f"{round(s / 60)} min"
    if s < 48 * 3600:
        h, m = divmod(round(s / 60), 60)
        return f"{h} h {m} min" if m else f"{h} h"
    return f"{round(s / 86400)} days"


def _when(ts: float, now_utc: int) -> str:
    """'today 11:36', 'yesterday 15:52', 'Tue 22 Sep 15:52', 'tomorrow 15:52'."""
    lt, ln = time.localtime(ts), time.localtime(now_utc)
    hm = time.strftime("%H:%M", lt)
    days = (time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 12, 0, 0, 0, 0, -1))
            - time.mktime((ln.tm_year, ln.tm_mon, ln.tm_mday, 12, 0, 0, 0, 0, -1))) / 86400
    word = {0: "today", -1: "yesterday", 1: "tomorrow"}.get(round(days))
    return f"{word} {hm}" if word else time.strftime("%a %d %b %H:%M", lt)


def _gb(b: float) -> str:
    return f"{b / 1e9:.1f} GB"


def _dev(d: dict) -> str:
    return d.get("name") or f"device {d.get('device_instance')}"


def build(dal: Any, config: Any, now_utc: int, *, dests: Optional[list] = None) -> dict:
    """Yesterday's summary: {day, state: ok|attention|critical, attention: [...],
    facts: [(title, value)], built_utc}."""
    t0, t1 = yesterday(now_utc)
    attention: list[str] = []
    facts: list[tuple[str, str]] = []
    critical = False

    def section(title: str, fn) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 -- see the module docstring
            facts.append((title, f"could not be read ({type(exc).__name__})"))

    # ── is data flowing right now? ──────────────────────────────────────────────────
    def data_now() -> None:
        nonlocal critical
        stale_s = float(_get(config, "alerts", "stale_after_s", default=900) or 900)
        newest = dal.samples_time_extent().get("max_ts")
        if newest is None:
            critical = True
            attention.append("No readings have ever been recorded.")
            facts.append(("Data now", "nothing recorded"))
            return
        age = now_utc - int(newest)
        if age > stale_s:
            critical = True
            attention.append(f"Data has stopped: the newest reading is {_ago(age)} old.")
            facts.append(("Data now", f"STOPPED — newest reading {_ago(age)} old"))
        else:
            facts.append(("Data now", f"flowing — newest reading {_ago(age)} old"))
    section("Data now", data_now)

    # ── yesterday's polling ─────────────────────────────────────────────────────────
    def polling() -> None:
        interval = pollrate.interval(config)
        # A day the poll rate changed is counted from the change: at the new rate the hours
        # before it look short on every controller (say, going from 2 minutes to 1).
        c0 = pollrate.counted_from(dal, t0, t1)
        if c0 > t0:
            facts.append(("Poll rate", f"every {pollrate.label(interval)} since "
                                       f"{time.strftime('%H:%M', time.localtime(c0))}; "
                                       "cycles counted from then"))
        expected = max(1, int(round((t1 - c0) / interval)))
        devs = dal.poll_health_between(c0, t1)
        if not devs:
            attention.append("No poll cycles at all were recorded yesterday.")
            facts.append(("Poll cycles", f"0 of {expected:,}"))
            return
        def owed(d: dict) -> int:
            # A controller added yesterday owes cycles only from its first poll: the boiler
            # panel, added at 15:38 on 09-29, was reported as "251 of 720" the next morning.
            start = max(c0, int(d.get("first_poll_utc") or c0))
            return max(1, int(round((t1 - start) / interval)))
        short = [d for d in devs if d["cycles"] < _MIN_CYCLE_SHARE * owed(d)]
        for d in short:
            attention.append(f"{_dev(d)}: {d['cycles']:,} of {owed(d):,} poll cycles.")
        added = [d for d in devs if int(d.get("first_poll_utc") or t0) > t0]
        if added:
            facts.append(("New controllers", "; ".join(
                f"{_dev(d)}, polled since {time.strftime('%H:%M', time.localtime(int(d['first_poll_utc'])))}"
                for d in added)))
        cyc = [int(d["cycles"]) for d in devs]
        facts.append(("Poll cycles",
                      f"{cyc[0]:,} of {expected:,} on all {len(devs)} controllers"
                      if min(cyc) == max(cyc) else
                      f"{min(cyc):,}–{max(cyc):,} of {expected:,} across "
                      f"{len(devs)} controllers"))

        att = sum(int(d["reads_attempted"] or 0) for d in devs)
        okr = sum(int(d["reads_ok"] or 0) for d in devs)
        notes = []
        for d in devs:
            a, o = int(d["reads_attempted"] or 0), int(d["reads_ok"] or 0)
            failed = int(d["cycles"]) - int(d["ok_cycles"] or 0)
            share = o / a if a else 1.0
            if share < _MIN_READ_SHARE or failed > _MAX_FAILED_CYCLE_SHARE * owed(d):
                attention.append(f"{_dev(d)}: {share:.1%} of reads answered, "
                                 f"{failed} failed poll cycle{'s' if failed != 1 else ''}.")
            elif failed:
                notes.append(f"{failed} failed cycle{'s' if failed != 1 else ''} on {_dev(d)}")
        overall = okr / att if att else 1.0
        facts.append(("Reads answered",
                      ("100 %" if okr == att else f"{overall:.2%}")
                      + (f" ({'; '.join(notes)})" if notes else "")))

        stored = dal.samples_count_between(t0, t1)
        facts.append(("Readings stored", f"{stored:,}"))
        if okr and stored < 0.99 * okr:
            attention.append(f"{okr:,} readings were read but only {stored:,} were stored.")

        times = dal.poll_cycle_times(t0, t1)
        gap, at = 0, None
        for a, b in zip(times, times[1:]):
            if b - a > gap:
                gap, at = b - a, a
        if gap > _GAP_S:
            attention.append(f"A {_ago(gap)} hole in the record at "
                             f"{time.strftime('%H:%M', time.localtime(at))}.")
        facts.append(("Longest gap", _ago(gap) + (
            f" at {time.strftime('%H:%M', time.localtime(at))}"
            if at is not None and gap > 2 * interval else "")))
    section("Poll cycles", polling)

    # ── the database backup ─────────────────────────────────────────────────────────
    def backup() -> None:
        every = float(_get(config, "storage", "backup", "interval_s", default=0) or 0)
        bdir = _get(config, "storage", "backup", "dir", default="data/backups") or "data/backups"
        if every <= 0:
            attention.append("Database backups are switched off (storage.backup.interval_s).")
            facts.append(("Database backup", "switched off"))
            return
        path, mt = newest_db_backup(bdir)
        if path is None:
            attention.append("There is no database backup.")
            facts.append(("Database backup", "none"))
            return
        due = mt + every
        facts.append(("Database backup", f"{_when(mt, now_utc)} ({_gb(path.stat().st_size)})"
                                         f" · next due {_when(due, now_utc)}"))
        if now_utc > due + _GRACE_S:
            attention.append(f"The database backup is overdue: the last one is "
                             f"{_ago(now_utc - mt)} old.")
    section("Database backup", backup)

    # ── the off-site copy: the only one that survives this disk ────────────────────
    def offsite() -> None:
        d = str(_get(config, "storage", "backup", "offsite_dir", default="") or "").strip()
        if not d:
            attention.append("The database backups exist only on this disk: no off-site copy "
                             "is configured (storage.backup.offsite_dir).")
            facts.append(("Off-site copy", "not configured"))
            return
        label = "OneDrive" if "onedrive" in d.lower() else Path(d).name
        path, mt = newest_offsite_copy(d)
        if path is None:
            attention.append(f"There is no off-site copy of the database in {label} yet.")
            facts.append(("Off-site copy", "none"))
            return
        facts.append(("Off-site copy", f"{_when(mt, now_utc)} ({_gb(path.stat().st_size)}) "
                                       f"in {label}"))
        bdir = _get(config, "storage", "backup", "dir", default="data/backups") or "data/backups"
        newest, bmt = newest_db_backup(bdir)
        if (newest is not None and not (Path(d) / offsite_name(newest)).exists()
                and now_utc - bmt > _GRACE_S):
            attention.append(f"The newest backup ({_when(bmt, now_utc)}) has not been copied "
                             f"to {label}.")
    section("Off-site copy", offsite)

    # ── snapshots of the code and the written record ───────────────────────────────
    def snapshots() -> None:
        every = float(_get(config, "storage", "snapshot_interval_s", default=0) or 0)
        if every <= 0:
            attention.append("Snapshots are switched off (storage.snapshot_interval_s).")
            facts.append(("Snapshots", "switched off"))
            return
        where = dests if dests is not None else snapshot_dests()
        if not where:
            attention.append("No snapshot destinations could be found.")
            facts.append(("Snapshots", "no destinations"))
            return
        parts = []
        for dest in where:
            label = "OneDrive" if "onedrive" in str(dest).lower() else "local"
            mt = newest_archive_mtime([dest])
            if not mt:
                attention.append(f"There is no snapshot in the {label} copy.")
                parts.append(f"{label}: none")
                continue
            parts.append(f"{label} {_when(mt, now_utc)}")
            if now_utc - mt > every + _GRACE_S:
                attention.append(f"The {label} snapshot is {_ago(now_utc - mt)} old.")
        facts.append(("Snapshots", " · ".join(parts)))
    section("Snapshots", snapshots)

    # ── rollups and weather: the two jobs that fail silently in the viewer ─────────
    def rollups() -> None:
        wm = dal.rollup_watermark()
        if wm is None:
            facts.append(("Rollups", "none built yet"))
            return
        facts.append(("Rollups", f"built to {_when(wm, now_utc)}"))
        if now_utc - wm > _ROLLUP_LAG_S:
            attention.append(f"Rollups have stopped: built only to {_when(wm, now_utc)}, "
                             "so long-range charts are missing recent data.")
    section("Rollups", rollups)

    def weather() -> None:
        last = dal.weather_last_fetched()
        if last is None:
            facts.append(("Weather", "none fetched"))
            return
        facts.append(("Weather", f"fetched {_when(last, now_utc)}"))
        if now_utc - last > _WEATHER_LAG_S:
            attention.append(f"Weather has not been fetched for {_ago(now_utc - last)}.")
    section("Weather", weather)

    # ── disk ────────────────────────────────────────────────────────────────────────
    def disk() -> None:
        db = Path(str(_get(config, "storage", "path", default="data/hvactrend.db")))
        free = shutil.disk_usage(str(db.parent)).free
        size = sum(p.stat().st_size for p in (db, Path(str(db) + "-wal")) if p.exists())
        floor = float(_get(config, "storage", "backup", "min_free_bytes", default=0) or 0)
        warn_at = 2 * floor if floor else _DISK_FLOOR
        facts.append(("Disk", f"{_gb(free)} free · database {_gb(size)}"))
        if free < warn_at:
            attention.append(f"Disk space is low: {_gb(free)} free"
                             + (f"; backups stop below {_gb(floor)}." if floor else "."))
    section("Disk", disk)

    # ── alerts still open ───────────────────────────────────────────────────────────
    def open_alerts() -> None:
        live = [a for a in dal.open_alerts() if a.get("source") == "watchdog"]
        facts.append(("Open alerts", "none" if not live else str(len(live))))
        for a in live:
            if a.get("kind") != "collector-stale":     # already the headline when data stops
                attention.append(f"Open alert: {a.get('message')}")
    section("Open alerts", open_alerts)

    # ── equipment: the rule-based alarms ─────────────────────────────
    def equipment() -> None:
        if not dal.has_table("incidents"):
            facts.append(("Equipment", "alarm rules not running yet"))
            return
        if not bool(_get(config, "rules", "enabled", default=True)):
            facts.append(("Equipment", "alarm rules switched off (rules.enabled)"))
            return
        perr = dal.get_meta("notify.incident_error")         # "<epoch> <Type>" (watchdog)
        if perr:
            attention.append("Equipment alarms are not reaching your phone: the push step "
                             f"fails ({perr.split(' ', 1)[-1][:80]}).")
        else:
            # a push that ntfy REJECTED raises nothing; it is only on the Settings page, which
            # nobody reads for news (2026-10-27 review)
            le, lo = dal.get_meta("notify.last_error_utc"), dal.get_meta("notify.last_ok_utc")
            if le and (not lo or int(le) > int(lo)):
                attention.append("The last phone notification was not delivered: "
                                 + str(dal.get_meta("notify.last_error") or "")[:120])
        last = dal.get_meta("rules.last_run_utc")
        err = dal.get_meta("rules.last_error")       # "<epoch> <Type>: <message>"
        err_t = int(err.split(" ", 1)[0]) if err and err.split(" ", 1)[0].isdigit() else None
        # >=: a run that recorded an error for one rule still finished (rules/live.py)
        if err_t is not None and (last is None or err_t >= int(last)):
            attention.append("The equipment alarm rules are failing: "
                             + err.split(" ", 1)[1][:160])
        elif last is not None and now_utc - int(last) > 1800:
            attention.append(f"The equipment alarm rules last ran {_when(int(last), now_utc)}.")
        elif last is None:
            facts.append(("Equipment", "alarm rules have not run yet"))
            return
        rows = dal.incidents_since(t0)
        live = [r for r in rows if r["status"] == "open"]
        important = ("action", "critical")
        new = [r for r in rows if r["opened_utc"] >= t0 and r["tier"] in important]
        facts.append(("Equipment", f"{len(live)} open incident{'s' if len(live) != 1 else ''}, "
                                   f"{len(new)} new since yesterday"))
        # every important one still on asks for a look, by name, the old ones too, worst
        # first (once per condition). Until 2026-10-26 only new ones did: AC-2's and AC-8's
        # Multistack alarms sat 12 days in the quiet "Still open" line as "controller alarm".
        on: dict = {}
        for r in sorted((r for r in live if r["tier"] in important),
                        key=lambda r: (r["tier"] != "critical", int(r["started_utc"]))):
            on.setdefault(r["fingerprint"], r)
        for r in list(on.values())[:8]:
            attention.append(f"{r['system_code']}: {incident_name(r)} for "
                             f"{_ago(now_utc - int(r['started_utc']))} (since "
                             f"{_when(int(r['started_utc']), now_utc)})")
        if len(on) > 8:
            attention.append(f"And {len(on) - 8} more equipment alarms still on.")
        over = [r for r in new if r["status"] != "open"]
        if over:
            facts.append(("Came and went", "; ".join(
                f"{r['system_code']}: {incident_name(r)}"
                + ("" if (r.get("resolve_reason") or "cleared") == "cleared"
                   else " (closed, NOT cleared: nothing to judge it by)") for r in over[:6])))
        quiet = [r for r in live if r["tier"] not in important]
        if quiet:
            facts.append(("Still open", "; ".join(
                f"{r['system_code']}: {incident_name(r)}" for r in quiet[:6])))
    section("Equipment", equipment)

    # ── overrides: points an operator (or life safety) is commanding (bacnet/priority.py) ──
    def overrides() -> None:
        last = dal.get_meta("priority.last_read_utc") if dal.has_table("point_command") else None
        if last is None:
            facts.append(("Overrides", "not read yet"))
            return
        rows = priority.mark_expected(
            dal.overrides(), coverage.site_facts(config.get("site_knowledge") if config else None))
        held = [r for r in rows if r["expected"]]
        rows = [r for r in rows if not r["expected"]]
        asof = "" if now_utc - int(last) <= 3600 else f" (last read {_when(int(last), now_utc)})"
        if not rows:
            facts.append(("Overrides", "none" + asof))
        else:
            more = f"; and {len(rows) - 8} more" if len(rows) > 8 else ""
            facts.append((f"Overrides ({len(rows)})",
                          "; ".join(priority.describe(r) for r in rows[:8]) + more + asof))
        if held:
            facts.append((f"Held on purpose ({len(held)})", "; ".join(priority.short(r) for r in held)))
    section("Overrides", overrides)

    # ── after-hours use: units run outside their schedule by an override button ─────────
    def after_hours() -> None:
        if not dal.has_table("point_command") or dal.get_meta("priority.last_read_utc") is None:
            return
        rows = dal.after_hours_sessions(t0, t1)
        more = f"; and {len(rows) - 8} more" if len(rows) > 8 else ""
        facts.append(("After-hours use", "none" if not rows else
                      "; ".join(priority.session_text(s) for s in rows[:8]) + more))
    section("After-hours use", after_hours)

    # ── the AI summary: written, or failed? ──────────────────────────
    # In September the reports failed for days on "credit balance is too low" and nothing
    # told anyone. The summary is written at 06:00, so the 07:00 check-in can say.
    def ai_summary() -> None:
        if not bool(_get(config, "ai", "enabled", default=False)):
            return
        daily = [r for r in dal.list_reports(limit=10) if r["kind"] == "daily"
                 and int(r["created_utc"]) >= now_utc - 26 * 3600]
        if not daily:
            attention.append("No AI summary was written in the last day.")
            facts.append(("AI summary", "none in the last day"))
            return
        r = max(daily, key=lambda x: int(x["created_utc"]))
        if r["status"] != "complete":
            attention.append("The AI summary failed: " + (r.get("prose") or "")[:160])
            facts.append(("AI summary", f"failed {_when(int(r['created_utc']), now_utc)}"))
            return
        n = int(r.get("finding_count") or 0)
        facts.append(("AI summary", f"written {_when(int(r['created_utc']), now_utc)}, "
                                    + (f"{n} action{'s' if n != 1 else ''}" if n else "no actions")
                                    + ", on the Reports page"))
    section("AI summary", ai_summary)

    return {"day": time.strftime("%a %d %b", time.localtime(t0)), "t0": t0, "t1": t1,
            "state": "critical" if critical else ("attention" if attention else "ok"),
            "attention": attention, "facts": facts, "built_utc": int(now_utc)}
