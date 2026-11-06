"""Headless collector daemon.

The collector runs as an asyncio task INSIDE the same process/event loop as the
API, sharing the one ReadOnlyBACnetApplication. It never creates its
own BACnet stack and never runs as a second OS process against the same NIC.

Each interval it polls every ENABLED point (the operator's selection), batched via
ReadPropertyMultiple per device with per-object fallback, capturing status/quality
on EVERY sample so a faulted/stuck/comm-lost point is recorded as such,
never as a phantom value. Reads across the two Trane SCs are lightly staggered
. All reads go through the shared read-only application — no writes.

bacpypes3 error discipline: read_property* RETURN an ErrorRejectAbortNack (a
BaseException) on failure — the app wrapper normalizes that to a returned object,
and `_is_error` catches it. An unread point becomes a comm-loss sample.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import defaultdict
from typing import Any, Optional

from ..bacnet import priority
from ..bacnet.application import ReadOnlyBACnetApplication
from ..bacnet.discovery import _is_error, _present_value
from ..models import PollHealth, Quality, Sample
# The one definition of "newest archive", shared with the watchdog's daily check-in (which
# must not import this module). newest_archive_mtime is re-exported here for old callers.
from ..archives import (make_offsite_copy, newest_archive_mtime,  # noqa: F401
                        newest_db_backup, offsite_name)
from ..storage.dal import DAL

logger = logging.getLogger(__name__)

# Read present-value plus the status trio every sample.
_SAMPLE_PROPS = ["present-value", "status-flags", "reliability", "out-of-service"]


class Collector:
    """Interval-driven, read-only trend collector.

    Args:
        app:    the SHARED read-only BACnet application (not created here).
        dal:    storage interface.
        config: loaded Config.
    """

    def __init__(self, app: ReadOnlyBACnetApplication, dal: DAL, config: Any) -> None:
        self._app = app
        self._dal = dal
        self._config = config
        self._stop = asyncio.Event()      # kill switch
        poll = _get(_get(config, "bacnet", default={}), "poll", default={})
        self._interval_s = float(_get(poll, "interval_s", default=120))
        self._stagger_s = float(_get(poll, "stagger_s", default=0) or 0)
        self._batch = int(_get(poll, "rpm_batch", default=10) or 10)
        # A device that refuses grouped reads is read one point at a time, and a point it does
        # not answer costs a full timeout per property. 2026-10-14 14:25, the tower panel was
        # being reloaded: that fallback took 28 minutes and no other controller was polled
        # meanwhile. So each device gets this long per cycle; points not reached in time are
        # recorded as comm-loss and the cycle moves on.
        self._device_budget_s = float(_get(poll, "device_budget_s", default=45) or 45)
        # Who is in control of each commandable point (bacnet/priority.py): its priority
        # array, READ every priority_interval_s (2026-10-06: would it know if they had
        # something overridden?). 0 switches it off. Small batches: sixteen slots a point
        # fill an unsegmented reply quickly.
        self._priority_interval_s = float(_get(poll, "priority_interval_s", default=600) or 0)
        self._priority_batch = int(_get(poll, "priority_batch", default=5) or 5)
        self._next_priority: Optional[float] = None     # None = due now
        # periodic housekeeping: materialize rollups + prune per the retention policy so
        # the raw samples table (the unbounded grower) stays bounded and the downsample
        # tables stay current. Runs in the same loop, between polls.
        storage = _get(config, "storage", default={})
        self._rollups_enabled = bool(_get(_get(storage, "rollups", default={}),
                                          "enabled", default=False))
        self._retention = _get(storage, "retention", default=None)
        self._pending: list = []          # samples a failed write left for the next cycle
        self._maint_interval_s = float(_get(storage, "maintenance_interval_s",
                                            default=3600) or 3600)
        self._last_maint = 0.0
        # between-report alerting: run the deterministic pre-screen over a recent window
        # every check_interval_s so a hard fault is caught in minutes, not at the next report.
        alerts = _get(config, "alerts", default={})
        self._alerts_enabled = bool(_get(alerts, "enabled", default=False))
        self._alert_interval_s = float(_get(alerts, "check_interval_s", default=300) or 300)
        self._last_alert_check = 0.0
        # Periodic weather pull + learned-model rebuild. These were documented as
        # external Task Scheduler jobs (fetch_weather.py hourly, build_model.py nightly)
        # that were NEVER ACTUALLY CREATED — so on the live box the weather table and the
        # baselines silently went 67 h stale and the economizer analysis (enthalpy /
        # wet-bulb, read from the weather table) had nothing current to read. Running
        # them in this loop means they inherit the server's boot persistence and can't be
        # forgotten again. Both are cheap: a full roles+baselines rebuild measures ~2.8 s,
        # a weather pull is one HTTP call.
        self._weather_interval_s = float(
            _get(_get(config, "weather", default={}),
                 "refresh_interval_s", default=3600) or 0)
        self._model_interval_s = float(
            _get(_get(_get(config, "analysis", default={}), "baseline", default={}),
                 "rebuild_interval_s", default=86400) or 0)
        # Start DUE (not "one interval from now") so a restart always brings both
        # current — after a deploy the operator should not wait an hour for weather.
        #
        # None, NOT 0.0. `time.monotonic()` is measured from an arbitrary epoch — on
        # Windows it is time since BOOT — so `monotonic() - 0.0 < interval` is TRUE for
        # the first `interval` seconds of machine uptime, and the job is skipped instead
        # of run. Live proof on this box: uptime 4.8 h, so the 24 h model rebuild had
        # been silently deferred since boot and the baselines were 49 HOURS STALE while
        # reports were being generated against them. That is the exact silent-staleness
        # failure this loop was created to fix on 2026-07-23 — the fix was correct and
        # the sentinel defeated it. `None` means "never run", which is unambiguous
        # regardless of what monotonic() counts from.
        self._last_weather: Optional[float] = None
        self._last_model: Optional[float] = None
        # Versioned snapshot of the SETUP (config/). Deliberately on the
        # collector loop rather than a Task Scheduler job: the loop is boot-persistent, so
        # it inherits that and cannot be quietly forgotten. Two jobs WERE forgotten that
        # way (build_model + fetch_weather, documented as scheduled tasks for a week and
        # never actually created), and a stale manual zip is what made a truncated
        # SESSION_STATE.md only partly recoverable on 2026-08-18.
        self._snapshot_interval_s = float(
            _get(_get(config, "storage", default={}) or {},
                 "snapshot_interval_s", default=86400) or 0)
        # Monotonic deadline for the next due-ness CHECK (None = check now). Not a 'last
        # ran' stamp -- that design doubled the effective interval; see _next_check.
        self._next_snapshot_check: Optional[float] = None
        from pathlib import Path as _P
        # overridable so tests can point it at a fake without touching real archives
        self._snapshot_script = _P(__file__).resolve().parents[3] / 'scripts' / 'snapshot.py'
        # DATABASE backup (storage.backup.interval_s, default weekly). Separate from the
        # snapshot above and NOT interchangeable with it: the snapshot carries the code and
        # the written record (~470 KB), while this carries the trend history -- the one
        # thing in the system that cannot be re-derived from the plant. Before this, every
        # database backup on this box had been taken by hand.
        _bk = _get(_get(config, "storage", default={}) or {}, "backup", default={}) or {}
        # `enabled` predates this job and appears in config.example.yaml as false. Honour
        # it when it is explicitly set, so an existing config means what it says; absent,
        # the interval alone decides.
        self._dbbackup_interval_s = (
            0.0 if _get(_bk, "enabled", default=True) is False
            else float(_get(_bk, "interval_s", default=604800) or 0))
        self._next_dbbackup_check: Optional[float] = None   # see _next_check
        self._dbbackup_task: Optional["asyncio.Task"] = None
        # OFF-SITE copy of the newest verified backup (storage.backup.offsite_dir; the operator chose
        # OneDrive, 2026-09-29). Until then every backup sat on the same disk as the database
        # it protects, so one disk failure would have taken all of them together. Due-ness is
        # read from the files, like everything else here: a verified archive with no copy in
        # offsite_dir is due, so a restart, a failure or a new weekly backup all converge.
        self._offsite_dir = str(_get(_bk, "offsite_dir", default="") or "").strip() or None
        self._offsite_keep = int(_get(_bk, "offsite_keep", default=2) or 2)
        self._bk_dir = str(_get(_bk, "dir", default="data/backups") or "data/backups")
        self._bk_min_free = int(_get(_bk, "min_free_bytes", default=0) or 0)
        self._next_offsite_check: Optional[float] = None
        self._offsite_task: Optional["asyncio.Task"] = None
        # Newest sample timestamp seen by this process; seeded from the DAL on the first
        # tick. Compared in memory so the clock check costs nothing per cycle (M-12).
        self._last_tick_ts: Optional[int] = None
        # AI reports. Unlike every other job here this is a WALL-CLOCK schedule, not an
        # interval -- ai.schedule.daily_time / weekly_day / weekly_time. It ran as neither:
        # `daily_time: "06:00"` carried the comment "wire to Task Scheduler" and never was,
        # so in one month 18 reports were produced with 5-9 day gaps. Same shape as the
        # build_model and fetch_weather jobs that were "scheduled" for a week and never
        # created, and the same way the OLD program died -- unnoticed for a fortnight.
        self._report_task: Optional["asyncio.Task"] = None

    async def run(self) -> None:
        """Main loop. Robust: one failed cycle never kills the collector. Cancels
        cleanly on stop() or task cancellation."""
        logger.info("Collector started (interval=%ss).", self._interval_s)
        self._last_maint = self._seed_maint_clock()
        self._last_alert_check = time.monotonic()   # first alert check after one interval
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                try:
                    await self._tick()
                except Exception:  # never let one bad cycle stop a 24/7 service
                    logger.exception("poll cycle failed")
                try:
                    await self._maybe_read_priorities()
                except Exception:  # who is in control must never stop collection
                    logger.exception("priority read failed")
                try:
                    self._maybe_maintain()
                except Exception:  # housekeeping must never take down the collector
                    logger.exception("maintenance cycle failed")
                try:
                    self._maybe_check_alerts()
                except Exception:  # alerting must never take down the collector
                    logger.exception("alert check failed")
                try:
                    await self._maybe_refresh_weather()
                except Exception:  # a weather outage must never stop collection
                    logger.exception("weather refresh failed")
                try:
                    self._maybe_rebuild_model()
                except Exception:  # a model rebuild must never stop collection
                    logger.exception("model rebuild failed")
                try:
                    self._maybe_snapshot()
                except Exception:  # a backup failure must never stop collection
                    logger.exception("snapshot failed")
                try:
                    await self._maybe_backup_db()
                except Exception:  # a backup failure must never stop collection
                    logger.exception("database backup failed")
                try:
                    self._maybe_offsite_copy()
                except Exception:  # an off-site copy failure must never stop collection
                    logger.exception("off-site copy failed")
                try:
                    self._maybe_generate_report()
                except Exception:  # a report failure must never stop collection
                    logger.exception("report scheduling failed")
                elapsed = time.monotonic() - t0
                await self._sleep(max(1.0, self._interval_s - elapsed))
        except asyncio.CancelledError:
            logger.info("Collector cancelled; shutting down.")
            raise
        finally:
            # Detached jobs are cancelled with the collector -- but cancelling a task that
            # awaits to_thread does NOT stop its worker thread. A database copy in flight
            # finishes and renames itself into place (verified, unpruned); only a hard kill
            # leaves a `.partial`, which the next start sweeps. _BACKUP_LOCK keeps a
            # relaunched collector from starting a second copy meanwhile. See _run_db_backup.
            for t in (self._dbbackup_task, self._offsite_task, self._report_task):
                if t is not None and not t.done():
                    t.cancel()
            logger.info("Collector stopped.")

    async def _tick(self) -> None:
        """One poll cycle across all enabled points, grouped by device."""
        targets = self._dal.poll_targets()
        if not targets:
            logger.debug("no enabled points to poll")
            return

        by_device: dict[tuple, list[dict]] = defaultdict(list)
        for t in targets:
            by_device[(t["device_id"], t["address"])].append(t)

        ts = int(time.time())     # UTC epoch seconds
        # ⚠️ WALL-CLOCK SANITY. Every timestamp in this database comes from this line, and
        # nothing checked it. A backward step (NTP correction, a VM resuming from suspend)
        # writes samples BEHIND the rollup watermark, where build_rollups will never pick
        # them up, and collides with rows already stored -- which write_samples now refuses
        # to overwrite rather than silently replacing. Compared in memory against the
        # previous cycle, seeded once from the DAL, so this costs nothing per cycle.
        # 2026-09-22 audit, M-12.
        if self._last_tick_ts is None:
            try:
                self._last_tick_ts = self._dal.samples_time_extent().get("max_ts")
            except Exception:
                self._last_tick_ts = None
        if self._last_tick_ts is not None and ts < int(self._last_tick_ts):
            logger.error(
                "CLOCK WENT BACKWARDS: this cycle is stamped %s, %d s BEFORE the newest "
                "sample already stored (%s). These samples land behind the rollup "
                "watermark and will never be aggregated. Check the host time source.",
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
                int(self._last_tick_ts) - ts,
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(self._last_tick_ts))))
        self._last_tick_ts = max(ts, int(self._last_tick_ts or ts))
        total = 0
        for i, ((device_id, address), pts) in enumerate(by_device.items()):
            if self._stop.is_set():
                break
            if i and self._stagger_s:
                await self._sleep(self._stagger_s)   # don't fire all SC trunks at t=0
            try:
                samples, health = await self._poll_device(address, device_id, pts, ts)
                if samples:
                    total += self._store(samples, device_id)
                self._dal.record_poll_health(health)
            except Exception as exc:  # isolate a flaky controller — poll the rest
                logger.exception("poll failed for device %s (%s); continuing", device_id, address)
                try:
                    self._dal.record_poll_health(PollHealth(
                        device_id=device_id, ts_utc=ts, ok=False,
                        reads_attempted=len(pts), reads_ok=0, method="error",
                        error=str(exc)[:200]))
                except Exception:
                    pass
        logger.info("poll cycle: %d samples from %d device(s)", total, len(by_device))

    _PENDING_MAX = 50_000     # about 60 cycles of the whole building: then the oldest go

    def _store(self, samples: list, device_id: Any) -> int:
        """Write a device's samples, with any an earlier cycle could not. A write that fails
        (the database locked by a backup or another process) used to lose them: device 74's
        46 on 2026-09-15 (2026-10-27 review). They wait, bounded, for the next cycle."""
        batch = self._pending + list(samples)
        try:
            self._dal.write_samples(batch)
        except Exception:  # noqa: BLE001 -- polling the rest of the building matters more
            self._pending = batch[-self._PENDING_MAX:]
            logger.exception("could not store %d sample(s) (device %s); kept for the next "
                             "cycle", len(batch), device_id)
            return 0
        self._pending = []
        return len(batch)

    async def _poll_device(self, address: str, device_id: int, pts: list[dict],
                           ts: int) -> tuple[list[Sample], PollHealth]:
        """Read one device's enabled points (batched RPM, per-object fallback)."""
        t0 = time.monotonic()
        samples: list[Sample] = []
        reads_ok = 0
        method = "rpm"

        skipped = 0

        def over_budget() -> bool:
            return time.monotonic() - t0 > self._device_budget_s

        for start in range(0, len(pts), self._batch):
            chunk = pts[start:start + self._batch]
            if over_budget():                         # out of time: unread this cycle
                samples += [_sample_from(p, {}, ts) for p in chunk]
                skipped += len(chunk)
                continue
            params: list[Any] = []
            oid_pt: dict[str, dict] = {}
            for p in chunk:
                oid = f"{p['object_type']},{p['object_instance']}"
                oid_pt[oid] = p
                params.append(oid)
                params.append(list(_SAMPLE_PROPS))

            try:
                rows = await self._app.read_property_multiple(address, params)
            except Exception:  # noqa: BLE001 -- a reply that would not decode, raised
                rows = None
            # None is what bacpypes3 returns for a reply that is not an RPM-ACK. It used to
            # reach the loop below as a TypeError, and the whole device's cycle went, the
            # chunks already read included (2026-10-27 review). The other readers guard it.
            if rows is None or _is_error(rows):
                method = "single"
                for p in chunk:
                    if over_budget():
                        samples.append(_sample_from(p, {}, ts))
                        skipped += 1
                        continue
                    s = await self._read_single(address, p, ts)
                    samples.append(s)
                    reads_ok += s.quality != Quality.COMM_LOSS
            else:
                grouped: dict[str, dict[str, Any]] = {}
                for objid, propid, _idx, value in rows:
                    grouped.setdefault(str(objid), {})[str(propid)] = value
                for oid, p in oid_pt.items():
                    s = _sample_from(p, grouped.get(oid, {}), ts)
                    samples.append(s)
                    reads_ok += s.quality != Quality.COMM_LOSS

        if skipped:
            logger.warning("device %s (%s): %d point(s) not read, the %.0f s budget ran out",
                           device_id, address, skipped, self._device_budget_s)
        health = PollHealth(
            device_id=device_id, ts_utc=ts, ok=reads_ok > 0,
            latency_ms=int((time.monotonic() - t0) * 1000),
            reads_attempted=len(pts), reads_ok=reads_ok, method=method,
            error=(f"{skipped} not read: the {self._device_budget_s:.0f} s budget ran out"
                   if skipped else (None if reads_ok else "no successful reads")),
        )
        return samples, health

    async def _read_single(self, address: str, p: dict, ts: int) -> Sample:
        """Per-object fallback for devices that reject RPM (e.g. some Trane UCs). The present
        value first: a point that does not give that is comm-loss, and asking it for three
        more properties would only cost three more timeouts."""
        props: dict[str, Any] = {}
        for prop in _SAMPLE_PROPS:
            try:
                props[prop] = await self._app.read_property(
                    address, f"{p['object_type']},{p['object_instance']}", prop)
            except Exception:  # a raised, non-normalized error -> treat as missing
                pass
            if prop == "present-value" and (prop not in props or _is_error(props[prop])):
                break
        return _sample_from(p, props, ts)

    async def _maybe_read_priorities(self) -> None:
        """Every priority_interval_s, read the priority array of every logged commandable
        point and record who is in control of it (bacnet/priority.py). Reads only, through
        the same application as the poll."""
        if self._priority_interval_s <= 0:
            return
        now_m = time.monotonic()
        if self._next_priority is not None and now_m < self._next_priority:
            return
        # From the DEADLINE, not from now: checked once per 120 s cycle, "now + 600" slipped to
        # 720 s about half the time (2026-10-27 review). A deadline long past (a stall)
        # restarts from now rather than firing back to back.
        nxt = (self._next_priority or now_m) + self._priority_interval_s
        self._next_priority = nxt if nxt > now_m else now_m + self._priority_interval_s
        targets = self._dal.command_targets(priority.COMMANDABLE_TYPES)
        if not targets:
            return
        by_device: dict[str, list[dict]] = defaultdict(list)
        for t in targets:
            by_device[t["address"]].append(t)
        ts = int(time.time())
        readings: list[dict] = []
        for address, pts in by_device.items():
            if self._stop.is_set():
                return
            readings += await self._read_priorities(address, pts)
        out = self._dal.record_commands(ts, readings,
                                        log_values_at_or_below=priority.OVERRIDE_MAX_LEVEL)
        self._dal.set_meta("priority.last_read_utc", ts)
        logger.info("priority read: %d point(s) read, %d without a priority array, %d "
                    "changed, %d under an override", out["read"], out["no_array"],
                    out["changed"], sum(priority.is_override(r.get("level")) for r in readings))

    async def _read_priorities(self, address: str, pts: list[dict]) -> list[dict]:
        out: list[dict] = []
        t0 = time.monotonic()                            # the same per-device budget as a poll
        for start in range(0, len(pts), self._priority_batch):
            chunk = pts[start:start + self._priority_batch]
            params: list[Any] = []
            for p in chunk:
                params += [f"{p['object_type']},{p['object_instance']}", ["priority-array"]]
            if time.monotonic() - t0 > self._device_budget_s:
                break                                    # the rest wait for the next read
            try:
                rows = await self._app.read_property_multiple(address, params)
            except Exception:  # noqa: BLE001 -- a reply that will not decode: one at a time.
                # It used to escape the whole pass after _next_priority had moved on, and
                # every controller's readings from that pass were lost (2026-11-06 review, M8).
                rows = None
            got: dict[str, Any] = {}
            if rows is None or _is_error(rows):          # no RPM: one at a time, as the poll
                for p in chunk:
                    if time.monotonic() - t0 > self._device_budget_s:
                        break
                    oid = f"{p['object_type']},{p['object_instance']}"
                    try:
                        got[oid] = await self._app.read_property(address, oid, "priority-array")
                    except Exception:  # a raised, non-normalized error -> unread this time
                        pass
            else:
                got = {str(objid): value for objid, _prop, _idx, value in rows}
            for p in chunk:
                r = _command_reading(p, got.get(f"{p['object_type']},{p['object_instance']}"))
                if r is not None:
                    out.append(r)
        return out

    def _maybe_generate_report(self) -> None:
        """Generate the daily / weekly AI report at its configured local time.

        WALL-CLOCK, not an interval, so it needs a different due-ness test from every other
        job on this loop -- and by the same rule as those, the test reads DURABLE STATE
        rather than process uptime: a slot is served when a report of that kind exists that
        was created at or after it. That makes the scheduler idempotent across restarts,
        immune to the watchdog, and self-limiting when the provider is failing (an error
        stub still counts as served, so a bad key cannot bill a call every two minutes).

        It also means a box that was OFF at 06:00 still produces the day's report when it
        comes back, and produces exactly ONE -- catch-up to the most recent slot, never a
        backfill of every missed day.

        Runs as a BACKGROUND TASK. The provider call can take a minute, and awaiting it
        inline would hold up the poll loop for that whole time -- a real hole in the trend
        record to produce a report about the trend record. Only one runs at a time.
        """
        cfg = _get(self._config, "ai", default={}) or {}
        if not _get(cfg, "enabled", default=False):
            return
        if self._report_task is not None and not self._report_task.done():
            return                              # still generating; never overlap a billed call

        sched = _get(cfg, "schedule", default={}) or {}
        now = time.time()
        for kind, hhmm, wday in (
                ("daily", _get(sched, "daily_time", default=None), None),
                ("weekly", _get(sched, "weekly_time", default=None),
                 _weekday_num(_get(sched, "weekly_day", default=None)))):
            if not hhmm:
                continue                        # null/absent time disables that kind
            if kind == "weekly" and wday is None:
                continue
            slot = _last_scheduled_slot(now, hhmm, wday)
            if slot is None:
                continue
            try:
                last = self._dal.last_report_utc(kind)
            except Exception:                   # older DAL -> do nothing rather than spam
                return
            if last is not None and last >= slot:
                continue                        # this slot has already been served
            # An attempt that RAISED (outside the provider path, which leaves a stub) served
            # nothing, so this slot stayed due and was tried again every 2 minutes: the
            # inputs rebuilt each time, and billed if the call succeeded and the store
            # failed (2026-10-27 review). At most 3 tries a slot, 30 minutes apart.
            key = f"report.{kind}.tried"
            t_slot, t_n, t_at = ((self._dal.get_meta(key) or "").split() + ["0", "0", "0"])[:3]
            same = int(t_slot) == int(slot)
            if same and (int(t_n) >= 3 or now - int(t_at) < 1800):
                continue
            self._dal.set_meta(key, f"{int(slot)} {int(t_n) + 1 if same else 1} {int(now)}")
            logger.info("%s report is due (scheduled %s, last report %s) — generating",
                        kind, hhmm,
                        "never" if last is None else time.strftime(
                            "%Y-%m-%d %H:%M", time.localtime(last)))
            self._report_task = asyncio.create_task(
                self._run_report(kind), name=f"hvactrend-report-{kind}")
            self._report_task.add_done_callback(_log_report_result)
            return                              # one at a time; weekly gets the next cycle

    async def _run_report(self, kind: str) -> dict:
        """Build the summary's inputs (in a worker thread), call the provider, store the
        report. Errors are RECORDED as a stub by generate_report rather than swallowed, so a
        failing provider is visible in the UI and the check-in instead of looking like a
        quiet plant."""
        from ..ai.reports import REPORT_LOCK, generate_report   # lazy: a light import
        t0 = time.monotonic()
        async with REPORT_LOCK:                 # never alongside one started by hand
            result = await generate_report(self._dal, self._config, kind)
        logger.info("%s report stored (id=%s, status=%s) in %.1fs",
                    kind, result.get("report_id"), result.get("status"),
                    time.monotonic() - t0)
        return result

    def _seed_maint_clock(self) -> float:
        """How long ago maintenance EFFECTIVELY last ran, judged from the rollups.

        This timer used to start at `time.monotonic()` on every loop start, meaning the
        first housekeeping came one full interval after boot. That is fine once, but the
        timer resets on every restart and THE WATCHDOG RESTARTS THIS SERVICE -- so a
        service restarting more often than `maintenance_interval_s` would never build a
        rollup or prune a row again, silently. Observed for real: three restarts inside an
        hour left the 5-minute rollups 57 minutes stale and the hourly ones 147 minutes
        stale, which put 20,254 raw rows into the trend picker's correlated-subquery tail
        and took /api/logged from 0.18 s to 1.6 s.

        Deliberately still not "always run at startup": that would rebuild rollups on every
        flap. The DATA decides. Same lesson as the database backup, which judges due-ness
        from the newest archive on disk rather than from uptime.
        """
        now_m = time.monotonic()
        try:
            wm = self._dal.rollup_watermark()
        except Exception:                     # older DAL / unreadable DB -> old behaviour
            return now_m
        if wm is None:                        # nothing rolled up yet: due immediately
            return now_m - (self._maint_interval_s + 1.0)
        behind = max(0.0, time.time() - float(wm))
        if behind >= self._maint_interval_s:
            logger.info("rollups are %.0f min stale at startup; maintenance is due now",
                        behind / 60.0)
            # One second PAST the interval, not exactly on it. Seeding the boundary exactly
            # leaves `monotonic() - seeded < interval` true by a float ulp, which defers the
            # very check this is meant to force.
            return now_m - (self._maint_interval_s + 1.0)
        return now_m - behind

    def _maybe_maintain(self) -> None:
        """Every maintenance interval, build rollups + prune old data. Synchronous DAL
        work (a couple of INSERT/DELETEs) run between polls; guarded by the caller so a
        failure is logged, never fatal. No-op until the interval elapses."""
        if time.monotonic() - self._last_maint < self._maint_interval_s:
            return
        self._last_maint = time.monotonic()
        now = int(time.time())
        if self._rollups_enabled and hasattr(self._dal, "build_rollups"):
            built = self._dal.build_rollups(now)
            if any(built.values()):
                logger.info("rollups built: %s", built)
        if self._retention and hasattr(self._dal, "enforce_retention"):
            pruned = self._dal.enforce_retention(now, self._retention)
            if any(pruned.values()):
                logger.info("retention pruned: %s", pruned)

    def _maybe_check_alerts(self) -> None:
        """Every alert interval, run the between-report deterministic alert evaluation
        (raise/track/resolve high+critical conditions). Synchronous DAL work between
        polls; guarded by the caller so a failure is logged, never fatal. No-op until
        the interval elapses or when alerts are disabled."""
        if not self._alerts_enabled:
            return
        if time.monotonic() - self._last_alert_check < self._alert_interval_s:
            return
        self._last_alert_check = time.monotonic()
        from ..ai.alerts import evaluate_alerts  # lazy: keep the collector import light
        res = evaluate_alerts(self._dal, self._config, int(time.time()))
        if res.get("raised") or res.get("resolved"):
            logger.info("alert check: %d raised, %d resolved, %d open",
                        res.get("raised", 0), res.get("resolved", 0), res.get("open", 0))

    async def _maybe_refresh_weather(self) -> None:
        """Pull current outdoor conditions on an interval (default hourly, matching the
        hour-floored weather rows). The economizer checks and the report weather line
        read the `weather` table, so a stale table degrades the analysis SILENTLY rather
        than failing loudly — a daily report simply runs with no weather at all.

        The HTTP GET runs in a worker thread; only the store (a few rows) runs here, on
        the loop the DAL's connection is bound to. It used to be the whole thing on the
        loop: on a BAS network without internet, DNS plus the connect timeout froze polling,
        live reads and every page for 15-25 s an hour (2026-11-06 review, M4). No-op when
        weather.refresh_interval_s is 0/null."""
        if self._weather_interval_s <= 0:
            return
        if (self._last_weather is not None
                and time.monotonic() - self._last_weather < self._weather_interval_s):
            return
        self._last_weather = time.monotonic()   # stamp BEFORE, so a failure backs off
        from ..ai import weather                      # lazy: keeps the import light
        fetched = await asyncio.to_thread(weather.fetch_weather, self._config)
        res = weather.store_weather(self._dal, fetched)
        if res.get("error"):
            logger.warning("weather refresh failed (will retry): %s", res["error"])
        elif res.get("stored"):
            logger.info("weather refreshed: %d row(s) from %s",
                        res["stored"], res.get("source"))

    def _maybe_rebuild_model(self) -> None:
        """Re-classify point roles + rebuild the learned baselines on an interval
        (default daily). Without this the normal bands freeze at whenever someone last
        ran the CLI: newly enabled points never get a band at all, and `long_slope` /
        `changed_frac` — which the drift and stuck detectors read — stop advancing.
        Measured ~2.8 s on 1.6 M samples. No-op when
        analysis.baseline.rebuild_interval_s is 0/null."""
        if self._model_interval_s <= 0:
            return
        if (self._last_model is not None
                and time.monotonic() - self._last_model < self._model_interval_s):
            return
        self._last_model = time.monotonic()     # stamp BEFORE, so a failure backs off
        from ..ai import model
        t0 = time.monotonic()

        # ⚠️ THREE JOBS, NOT ONE, AND ONLY TWO OF THEM HAVE A CONSUMER HERE.
        #   assign_roles       -> points.role. The VIEWER reads it ~27 times: +SP pairing,
        #                         By-metric grouping, canonical point selection. KEEP.
        #   apply_plant_limits -> points.alarm_hi/lo, which drawLimits renders. KEEP.
        #   build_baselines    -> the `baseline` / `baseline_corr` tables, which are read
        #                         ONLY by ai/context.py and ai/prescreen.py. With
        #                         ai.enabled false those are dead writes -- MEASURED 21 s
        #                         of poll-loop time, at EVERY service start, producing rows
        #                         nothing will read.
        # The written record said "roles/baselines still rebuild because the VIEWER uses
        # them", which conflated all three and is what kept this running. 2026-09-22 audit,
        # M-8. Since 2026-10-20 the AI summary (ai/summary.py) narrates the alarm rules'
        # incidents and reads no baselines either, so they have their own switch,
        # `ai.baselines` (default off): turning the summary on must not bring back 21 s of
        # poll-loop time at every start.
        out = model.rebuild(self._dal, self._config)    # the button runs the same one
        roles, lim, b = out["roles"], out["limits"], out["baselines"] or {}
        ai_on = out["baselines"] is not None
        logger.info("model rebuilt in %.1fs: %s/%s roles classified (%s row(s) written), "
                    "%d points limited from the plant (%d alarming); baselines %s",
                    time.monotonic() - t0,
                    roles.get("classified"), roles.get("total"), roles.get("written"),
                    lim.get("applied"), lim.get("alarming"),
                    ("%d bands, %d correlations (window %sd)"
                     % (b.get("points", 0), b.get("corrs", 0), b.get("window_days")))
                    if ai_on else "SKIPPED (ai.baselines=false; nothing reads them)")

    def _maybe_snapshot(self) -> None:
        """Versioned snapshot of this installation's setup, config/ (default daily).

        Small and cheap, and it covers what the database backup does not: the site's own
        configuration and report prompts (never config/secrets.env). Written to data/snapshots
        and, when storage.backup.offsite_dir is set, to that synced folder too. Each archive
        is verified after writing and discarded if it fails, because an unverified backup is
        not a backup. No-op when storage.snapshot_interval_s is 0/null."""
        if self._snapshot_interval_s <= 0:
            return
        if (self._next_snapshot_check is not None
                and time.monotonic() < self._next_snapshot_check):
            return
        # Pessimistic first: anything below that raises backs off rather than retrying every
        # two-minute cycle. Overwritten with the real schedule once the outcome is known.
        self._next_snapshot_check = time.monotonic() + _RETRY_BACKOFF_S
        import importlib.util
        sp = self._snapshot_script
        if not sp.exists():
            logger.warning("snapshot script not found at %s", sp)
            return
        spec = importlib.util.spec_from_file_location("occ_snapshot", sp)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # ⚠️ DUE-NESS READS THE FILES ON DISK, NOT PROCESS UPTIME.
        # `_next_snapshot_check` starts as None so a fresh deploy is protected immediately -- but
        # it ALSO resets on every restart, and the watchdog restarts this service. Judging
        # from uptime alone meant a snapshot on every process start: a restart loop, or an
        # operator taking several in an afternoon, walked the whole keep window forward and
        # evicted a fortnight of genuine daily archives. The file count stayed at KEEP and
        # every survivor verified clean, so nothing reported a problem. Observed for real on
        # 2026-09-15 -- seven runs in one hour took the oldest surviving snapshot from 14
        # days back to 6. The same lesson as _maybe_backup_db below: the files are the state.
        dests = getattr(mod, "DESTS", [])
        newest = newest_archive_mtime(dests)
        due, nxt = _next_check(newest, self._snapshot_interval_s,
                               now_wall=time.time(), now_mono=time.monotonic())
        if not due:
            self._next_snapshot_check = nxt
            logger.debug("snapshot not due (newest is %.1f h old); re-check in %.1f h",
                         (time.time() - newest) / 3600, (nxt - time.monotonic()) / 3600)
            return
        for line in mod.snapshot():
            (logger.warning if "FAILED" in line else logger.info)("snapshot: %s", line)
        # Judge the outcome from the DISK, not from the log lines: a new archive means the
        # next one is due one interval from it; no new archive means it failed, so retry on
        # the backoff rather than every cycle.
        due, nxt = _next_check(newest_archive_mtime(dests), self._snapshot_interval_s,
                               now_wall=time.time(), now_mono=time.monotonic())
        self._next_snapshot_check = (time.monotonic() + _RETRY_BACKOFF_S) if due else nxt

    async def _maybe_backup_db(self) -> None:
        """Online backup of the trend database (default weekly).

        Runs as a BACKGROUND TASK, off the event loop AND off the poll loop.

        ⚠️ `to_thread` alone was NOT enough, and the docstring here used to claim it was.
        The copy ran in a worker thread, so HTTP stayed responsive -- but this coroutine
        AWAITED it, so the poll loop sat still for the entire copy and simply did not record
        those cycles. MEASURED on the live box:

            2026-08-26     991 MB   140.8 s   ->  190 s between poll cycles
            2026-09-22   1,750 MB   304.6 s   ->  338 s, 2-3 cycles lost outright

        It is linear in database size: at the ~17 GB a 730-day policy implies, that is close
        to an hour of missing record per weekly backup, with no poll_health row to show for
        it and nothing that reports it. Neither external audit found this; it surfaced only
        because a restart made a backup come due while the gap was being watched.

        `_maybe_generate_report` already solved this exact problem -- its docstring calls an
        inline await "a real hole in the trend record to produce a report about the trend
        record" -- and the backup never inherited the pattern. Third time in this codebase
        that a lesson stopped at one function (see F-03 drawOverview, M-11 drawWeather).

        The worker opens its OWN connection rather than borrowing the collector's, which is
        bound to this thread. That is safe here: WAL admits a second reader, and the SQLite
        online-backup API yields a consistent copy even while the collector is writing.

        Each archive is verified by READING IT BACK and discarded if it fails, because an
        unverified backup is not a backup. No-op when storage.backup.interval_s is 0/null.
        """
        if self._dbbackup_task is not None and not self._dbbackup_task.done():
            return                       # still copying; never overlap two backups
        if self._dbbackup_interval_s <= 0:
            return
        if _BACKUP_LOCK.locked():
            # A copy started by a PREVIOUS Collector instance is still running in its worker
            # thread. Don't start another, and don't sweep its .partial out from under it.
            self._next_dbbackup_check = time.monotonic() + 300
            return
        if (self._next_dbbackup_check is not None
                and time.monotonic() < self._next_dbbackup_check):
            return
        # Pessimistic first: a skip (no free space, no path) or anything raised backs off on
        # the retry interval rather than re-trying every cycle -- or waiting a whole week.
        self._next_dbbackup_check = time.monotonic() + _RETRY_BACKOFF_S
        from pathlib import Path
        store = _get(self._config, "storage", default={}) or {}
        bcfg = _get(store, "backup", default={}) or {}
        bdir = Path(_get(bcfg, "dir", default="data/backups") or "data/backups")
        keep = int(_get(bcfg, "keep", default=7) or 0)

        # Due-ness is judged from the newest archive ON DISK, not from process uptime.
        # `_next_dbbackup_check` starts as None so a fresh deploy is protected immediately, but
        # it also resets on every restart -- and the watchdog restarts this service. A
        # restart loop would otherwise take a full ~1 GB copy every time and prune the
        # real history out of the keep window within minutes. The files are the state.
        # A copy interrupted by a crash or a power cut leaves a .partial behind. It is
        # not a backup, it must not be mistaken for one by due-ness or by a human, and it
        # can be large -- so clear it before anything else looks at this directory.
        for stale in (bdir.glob("hvactrend.*.db.partial*") if bdir.exists() else []):
            try:
                stale.unlink()
                logger.warning("removed a stale partial backup: %s", stale.name)
            except OSError:
                pass
        newest = newest_db_backup(bdir)[1]      # archives.py: shared with the daily check-in
        due, nxt = _next_check(newest, self._dbbackup_interval_s,
                               now_wall=time.time(), now_mono=time.monotonic())
        if not due:
            self._next_dbbackup_check = nxt     # when it WILL be due -- see _next_check
            logger.debug("database backup not due (newest is %.1f h old); re-check in %.1f h",
                         (time.time() - newest) / 3600, (nxt - time.monotonic()) / 3600)
            return

        # ⚠️ FREE-SPACE PRECONDITION. Every copy lives on the same volume as the live
        # database, so the backup is the fastest way to fill the disk that the database
        # itself needs. MEASURED 2026-09-15: a raw sample costs ~44 bytes once
        # idx_samples_ts is counted, not the 24 used for the original 730-day sizing, so the
        # database grows toward ~16 GB rather than ~8. With keep copies beside it the volume
        # runs out BEFORE retention ever prunes. Refusing a backup is recoverable; filling
        # the disk under the collector is not -- SQLite starts failing writes and samples are
        # simply lost.
        import shutil
        try:
            free = shutil.disk_usage(str(bdir if bdir.exists() else bdir.parent)).free
        except OSError:
            free = None
        min_free = int(_get(bcfg, "min_free_bytes", default=0) or 0)
        src_probe = getattr(self._dal, "db_path", None) or _get(store, "path", default=None)
        try:
            need = int(Path(str(src_probe)).stat().st_size * 1.1)
        except (OSError, TypeError):
            need = 0
        if free is not None and (free < need or (min_free and free - need < min_free)):
            logger.error(
                "database backup SKIPPED: %.1f GB free, the copy needs %.1f GB and the "
                "configured floor is %.1f GB. Move backups off this volume or lower "
                "storage.backup.keep.",
                free / 1e9, need / 1e9, min_free / 1e9)
            return
        src = getattr(self._dal, "db_path", None) or _get(store, "path", default=None)
        if not src:
            logger.warning("database backup skipped: no storage.path configured")
            return
        bdir.mkdir(parents=True, exist_ok=True)
        # second-resolution names collide if two backups land in the same second. The
        # interval makes that unreachable in normal operation, but a silently overwritten
        # backup is a bad way to find out otherwise.
        stem = "hvactrend." + time.strftime("%Y%m%d_%H%M%S")
        dest = bdir / (stem + ".db")
        for i in range(1, 100):
            if not dest.exists():
                break
            dest = bdir / ("{0}_{1}.db".format(stem, i))

        logger.info("database backup starting: %s (polling continues)", dest.name)
        # Next check one interval out; _run_db_backup shortens it if the copy fails.
        self._next_dbbackup_check = time.monotonic() + self._dbbackup_interval_s
        self._dbbackup_task = asyncio.create_task(
            self._run_db_backup(str(src), dest, bdir, keep),
            name="hvactrend-db-backup")
        self._dbbackup_task.add_done_callback(_log_backup_result)

    def _maybe_offsite_copy(self) -> None:
        """Copy the newest verified database archive OFF THIS DISK, compressed (to the
        OneDrive folder, which syncs it off-site). No-op when storage.backup.offsite_dir is
        empty. The check is two directory listings, made at most every five minutes; the
        compression (~33 s for 1.75 GB) runs detached in a worker thread, so polling never
        waits for it -- the lesson of the 2026-09-22 backup stall."""
        if not self._offsite_dir:
            return
        if self._offsite_task is not None and not self._offsite_task.done():
            return
        now = time.monotonic()
        if self._next_offsite_check is not None and now < self._next_offsite_check:
            return
        self._next_offsite_check = now + 300
        if _BACKUP_LOCK.locked():            # a database copy is being written right now
            return
        from pathlib import Path
        newest, _ = newest_db_backup(self._bk_dir)
        if newest is None or (Path(self._offsite_dir) / offsite_name(newest)).exists():
            return                           # nothing to copy, or already copied
        logger.info("off-site copy starting: %s (polling continues)", newest.name)
        self._offsite_task = asyncio.create_task(self._run_offsite_copy(newest),
                                                 name="hvactrend-offsite-copy")

    async def _run_offsite_copy(self, archive: "Path") -> None:
        from pathlib import Path
        try:
            r = await asyncio.to_thread(
                make_offsite_copy, archive, self._offsite_dir,
                staging_dir=Path(self._bk_dir) / ".offsite_staging",
                keep=self._offsite_keep, min_free_bytes=self._bk_min_free)
        except Exception as exc:  # noqa: BLE001 -- logged and retried, never raised
            logger.error("off-site copy FAILED for %s, retrying in %.0f h: %r",
                         archive.name, _RETRY_BACKOFF_S / 3600, exc)
            self._next_offsite_check = time.monotonic() + _RETRY_BACKOFF_S
            return
        if not r.get("skipped"):
            logger.info("off-site copy %s verified (%.0f MB, %.0f%% of the archive, %.1fs)%s",
                        r["copy"].name, r["bytes"] / 1e6,
                        100.0 * r["bytes"] / max(1, r["archive_bytes"]), r["seconds"],
                        (", pruned " + ", ".join(r["pruned"])) if r["pruned"] else "")

    async def _run_db_backup(self, src: str, dest: "Path", bdir: "Path",
                             keep: int) -> None:
        """Copy, verify and rotate. Runs detached so the poll loop keeps recording.

        ⚠️ Cancelling this task does NOT stop the copy. The work runs in a worker thread,
        and the rename into place happens INSIDE that thread (_copy_and_verify), so on a soft
        cancel the copy runs to completion and renames itself in: a verified archive, just
        unpruned and unlogged -- the next backup's prune handles it. Only a HARD kill (Task
        Scheduler stop, a reboot) leaves a `.partial`, which never takes a name the backup
        glob matches and is swept on the next start. _BACKUP_LOCK stops a relaunched
        collector from starting a second copy while that thread is still writing. (This
        docstring used to claim a cancel meant "never renamed into place"; the 2026-09-29
        audit, A8, read the code and found otherwise.)"""
        t0 = time.monotonic()
        rows = await asyncio.to_thread(_backup_db_file, src, str(dest))
        took = time.monotonic() - t0
        if rows is None:
            # _backup_db_file cleans up after itself, so there is nothing to unlink:
            # a failed copy never reaches a name the glob can see.
            logger.error("database backup FAILED (copy or verification), discarded: %s",
                         dest.name)
            # retry on the backoff, not a week from now
            self._next_dbbackup_check = time.monotonic() + _RETRY_BACKOFF_S
            return
        size = dest.stat().st_size
        # Also threaded: the shrink guard reads the PREVIOUS archive to count its samples,
        # which is seconds of I/O -- 2.97 s on a 0.92 GB archive and growing.
        removed = await asyncio.to_thread(_prune_backups, bdir, keep, dest, rows)
        logger.info("database backup %s verified (%.0f MB, %d samples, %.1fs)%s",
                    dest.name, size / 1e6, rows, took,
                    (", pruned " + ", ".join(removed)) if removed else "")

    async def _sleep(self, secs: float) -> None:
        """Sleep, but wake immediately on stop() (responsive kill switch)."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=secs)
        except asyncio.TimeoutError:
            pass

    def stop(self) -> None:
        """Signal the loop to exit at the next checkpoint (kill switch)."""
        self._stop.set()


# ── sample construction / quality ──────────────────────────────────
def _sample_from(p: dict, props: dict, ts: int) -> Sample:
    pv = props.get("present-value")
    rel = props.get("reliability")
    oos = props.get("out-of-service")
    sf = props.get("status-flags")

    if not props:
        quality = Quality.COMM_LOSS          # device returned nothing for this object
        num = text = None
    elif _is_error(pv):
        quality = Quality.COMM_LOSS          # read errored/rejected/aborted
        num = text = None
    else:
        num, text = _present_value(pv)
        rel_s = None if rel is None or _is_error(rel) else str(rel)
        if _truthy(oos) or (rel_s is not None and rel_s != "no-fault-detected"):
            quality = Quality.FAULT          # out-of-service or unreliable sensor
        elif num is None and text in ("inf", "-inf", "nan"):
            quality = Quality.FAULT          # a Real the sensor could not give (M9)
        else:
            quality = Quality.GOOD

    return Sample(
        point_id=p["point_id"], ts_utc=ts, value_num=num, value_text=text,
        status_flags=_str_or_none(sf), reliability=_int_or_none(rel),
        out_of_service=_bool_or_none(oos), quality=quality,
    )


def _command_reading(p: dict, value: Any) -> Optional[dict]:
    """One point's priority-array reply as a reading for record_commands, or None when the
    read failed (its last known state then stands)."""
    if value is None:
        return None
    if _is_error(value):
        if priority.no_such_property(value):
            return {"point_id": p["point_id"], "has_array": False}
        return None
    try:
        level, v = priority.active(value)
    except ValueError:
        return None
    num, text = _present_value(v) if level is not None else (None, None)
    return {"point_id": p["point_id"], "has_array": True, "level": level,
            "value_num": num, "value_text": text}


def _truthy(v: Any) -> bool:
    if v is None or _is_error(v):
        return False
    try:
        return bool(int(v))
    except (TypeError, ValueError):
        return bool(v)


def _int_or_none(v: Any) -> Optional[int]:
    if v is None or _is_error(v):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _str_or_none(v: Any) -> Optional[str]:
    """StatusFlags is a bacpypes3 BitString — int(v) raises, so the readable string
    ('in-alarm;fault') is the right thing to keep."""
    if v is None or _is_error(v):
        return None
    s = str(v)
    return s or None


def _bool_or_none(v: Any) -> Optional[bool]:
    if v is None or _is_error(v):
        return None
    return _truthy(v)


_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _weekday_num(name: Any) -> Optional[int]:
    """'Mon' -> 0. None for anything unrecognised, which disables the weekly schedule
    rather than silently picking a day nobody asked for."""
    if name is None:
        return None
    if isinstance(name, int) and 0 <= name <= 6:
        return name
    key = str(name).strip().lower()[:3]
    return _WEEKDAYS.get(key)


def _last_scheduled_slot(now_ts: float, hhmm: Any,
                         weekday: Optional[int] = None,
                         localtime: Any = None) -> Optional[int]:
    """Most recent LOCAL wall-clock occurrence of HH:MM (on `weekday` if given) at or
    before now_ts, or None if hhmm is unusable.

    Local time via the OS, matching how occupancy hours are judged elsewhere, so the
    building's own DST is handled by the platform rather than re-implemented here.
    Walks back day by day, which is what makes catch-up land on exactly ONE slot: the
    most recent, never every missed day since the box was last on."""
    localtime = localtime or time.localtime
    try:
        parts = str(hhmm).strip().split(":")
        hh, mm = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
    except (TypeError, ValueError, IndexError):
        return None

    for back in range(0, 8):
        lt = localtime(now_ts - back * 86400)
        if weekday is not None and lt.tm_wday != weekday:
            continue
        slot = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
        if slot <= now_ts:
            return int(slot)
    return None


def _log_backup_result(task: "asyncio.Task") -> None:
    """Retrieve the backup task's outcome, for the same reason _log_report_result exists:
    an un-retrieved exception surfaces only as asyncio noise, which would make a failed
    backup invisible -- the worst thing on this list to lose quietly."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("database backup failed: %r", exc)


def _log_report_result(task: "asyncio.Task") -> None:
    """Retrieve the task's outcome. Without this a failure surfaces only as asyncio's
    "Task exception was never retrieved" -- the exact noise the wire-decode filter exists
    to suppress, which would make a genuinely failed report invisible."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("scheduled report failed: %r", exc)


# How long a FAILED or SKIPPED archive job waits before trying again (no free space, script
# missing, copy failed, anything raised). Deliberately far shorter than the jobs' own
# intervals: a failed weekly backup that waits a week to retry is two weeks without one.
_RETRY_BACKOFF_S = 6 * 3600


def _next_check(newest_mtime: float, interval_s: float, *, now_wall: float,
                now_mono: float) -> "tuple[bool, float]":
    """(due_now, next_check_monotonic) for an archive job, from the newest file on disk.

    ⚠️ When the job is NOT due, the next check is scheduled for the moment the newest
    archive WILL be due -- never one full interval from now. Both jobs used to stamp a
    "last ran" clock BEFORE the disk check, so a check that landed shortly before due-ness
    re-armed the gate for a whole interval and the effective interval doubled:
      - no snapshot at all on 2026-09-23 -- a manual snapshot at 09-22 16:41 landed three
        minutes after the automatic check had stamped its clock, so the next check found it
        23 h 57 m old, "not due", and re-armed for another 24 h;
      - the weekly DB backup due 2026-09-29 15:52 would have slipped to ~10-01 13:47,
        because the 09-24 reboot's first check re-armed it for a full week.
    That is the "12 days between backups instead of 7" seen before it was understood.
    2026-09-29 audit, A2.

    A family with no archives at all is due now. The 60 s floor stops a clock skew or an
    mtime a hair in the future from turning into a tight loop.
    """
    due_in = (newest_mtime + interval_s - now_wall) if newest_mtime else 0.0
    if due_in > 0:
        return False, now_mono + max(60.0, due_in)
    return True, now_mono


# ⚠️ Held by the WORKER THREAD for the whole database copy, and module-level rather than
# per-Collector on purpose. Cancelling the asyncio task that awaits a copy does NOT stop the
# thread (see _run_db_backup). If the collector task died mid-backup, the lifespan relaunches
# a NEW Collector 30 s later whose own `_dbbackup_task` is None -- and it used to start a
# second 1.9 GB copy beside the first, after which `keep: 2` could hold two copies of the
# same afternoon and prune last week's. The manual backup route takes the same lock.
# 2026-09-29 audit, A8.
_BACKUP_LOCK = threading.Lock()


# A backup may never rotate the history out on the strength of a copy that holds materially
# less of it than the one before. At steady state a weekly copy holds about the same count
# (retention removes the oldest week as the newest arrives), so 10% is a wide, safe margin.
MAX_BACKUP_SHRINK = 0.10


def _backup_db_file(src_path: str, dest_path: str) -> "Optional[int]":
    """Copy + verify under _BACKUP_LOCK, so two copies can never run at once in this
    process -- whichever Collector instance, or the manual route, asked for it."""
    if not _BACKUP_LOCK.acquire(blocking=False):
        logger.error("database backup refused: another copy is still running in a worker "
                     "thread (%s)", dest_path)
        return None
    try:
        return _copy_and_verify(src_path, dest_path)
    finally:
        _BACKUP_LOCK.release()


def _copy_and_verify(src_path: str, dest_path: str) -> "Optional[int]":
    """Copy the live database and verify the copy. Returns the verified sample count, or
    None if the result is unusable. Runs in a worker thread -- opens its own connections
    and touches no shared state.

    ⚠️ The copy is written as `<dest>.partial`, a name the backup glob does NOT match, and
    is renamed into place only after it verifies. Before this, a copy that failed by RAISING
    -- disk full, an I/O error, an antivirus lock, the process killed mid-copy -- was left
    behind as `hvactrend.<stamp>.db`. The caller swallows the exception ("a backup failure
    must never stop collection"), so that corpse then: counted as the newest archive for
    due-ness, blocking real backups for a full interval; AND, because the prune sorted by
    NAME, sorted last and survived while the verified copies were deleted. Two good backups
    in, one good backup and one unopenable file out. 2026-09-22 audit, H-1.

    Also opens the SOURCE `mode=ro`, so a backup can never CREATE the database it is supposed
    to be copying -- a missing storage.path is otherwise silently re-created as an empty file.
    """
    import os
    import sqlite3
    from pathlib import Path
    part = dest_path + ".partial"

    def _cleanup() -> None:
        for suffix in ("", "-journal", "-wal", "-shm"):
            try:
                os.unlink(part + suffix)
            except OSError:
                pass

    rows: "Optional[int]" = None
    try:
        src = sqlite3.connect(Path(src_path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            dest = sqlite3.connect(part)
            try:
                with dest:
                    src.backup(dest)
                # An archive should be ONE self-contained file. A copy of a WAL database is
                # itself WAL-mode, so every later read-only open of it -- verification, the
                # shrink check, a human restoring it -- strews -wal/-shm siblings beside it
                # that travel badly and confuse the directory. (audit L-7)
                dest.execute("PRAGMA journal_mode=DELETE")
            finally:
                dest.close()
        finally:
            src.close()
        check = sqlite3.connect(
            Path(part).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            if check.execute("PRAGMA integrity_check").fetchone()[0] == "ok":
                rows = int(check.execute("SELECT COUNT(*) FROM samples").fetchone()[0])
        finally:
            check.close()
    except Exception:                    # any failure at all: leave nothing behind
        logger.exception("database backup copy failed: %s", dest_path)
        _cleanup()
        return None
    if rows is None:
        _cleanup()
        return None
    os.replace(part, dest_path)
    _cleanup()
    return rows


def _count_samples(path: Any) -> "Optional[int]":
    """Sample count of an archive, or None if it cannot be read. Worker-thread only --
    MEASURED 2.97 s on a 0.92 GB / 16.6 M-row archive, and it grows with the record."""
    import sqlite3
    from pathlib import Path
    try:
        c = sqlite3.connect(
            Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            return int(c.execute("SELECT COUNT(*) FROM samples").fetchone()[0])
        finally:
            c.close()
    except Exception:
        return None


def _prune_backups(bdir: Any, keep: int, new_dest: Any, new_rows: int) -> list:
    """Keep the newest `keep` archives -- but never rotate on a copy that lost history.

    Verification proves a copy is a DATABASE, not that it is the RECORD. `_backup_db_file`
    returns a row COUNT and the caller only rejected None, so a backup of an empty database
    verified with 0 samples and pruned happily; at keep:2 two cycles would rotate out every
    real copy. SQLiteDAL silently creates an empty database whenever storage.path is missing
    -- a restore in progress, a moved file, a relative path from the wrong CWD -- so this is
    reachable without anything else going wrong.

    Ordered by MTIME, not by name, so neither a hand-named copy nor a stale-named one can
    sort itself to the front. Runs in a worker thread: the shrink check reads the previous
    archive, which is seconds of I/O the shared event loop must not absorb.
    """
    from pathlib import Path
    new_dest = Path(new_dest)
    files = sorted(Path(bdir).glob("hvactrend.*.db"), key=lambda f: f.stat().st_mtime)
    older = [f for f in files if f != new_dest]
    if older:
        prev = _count_samples(older[-1])
        if prev and new_rows < prev * (1 - MAX_BACKUP_SHRINK):
            logger.error(
                "database backup KEPT BUT NOT ROTATED: the new copy holds %s samples "
                "against %s in %s (-%.1f%%). Something has shortened the record; refusing "
                "to prune older archives on the strength of it.",
                format(new_rows, ","), format(prev, ","), older[-1].name,
                100.0 * (1 - new_rows / prev))
            return []
    removed = []
    if keep > 0:
        for old in files[:-keep]:
            try:
                old.unlink()
                removed.append(old.name)
            except OSError:
                continue
            # Take the siblings with it. Archives written before journal_mode=DELETE are
            # WAL-mode files, so each has -wal/-shm beside it; unlinking only the .db left
            # those behind forever. Found on the live box: 10 orphaned siblings, several
            # belonging to archives pruned weeks earlier.
            for suffix in ("-wal", "-shm", "-journal"):
                try:
                    old.with_name(old.name + suffix).unlink()
                except OSError:
                    pass
    return removed


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)
