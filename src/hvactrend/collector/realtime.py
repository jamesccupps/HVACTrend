"""Realtime trends: chosen points read every few seconds, for a set time.

The operator, 2026-11-06: a way to "pick specific points that i temporarily or for like maybe
a set amount of time, want to trend much more quickly ... for testing and diagnosing ...
sort of like desigo online trends". Then: keep the readings, every 5 s is fast enough,
anyone on the viewer may start one, each point can run for its own time, and one point or
all of them can be stopped.

How it reads:
  * Inside the collector's task, on the same BACnet application and event loop (one stack
    per process), through the collector's own `_poll_device`: the same grouped read, the
    same quality rules, the same one-request give-up on a silent controller.
  * One read per controller per slot. After a run's first read (at once), its slots sit on
    a grid (every 5 s at :00, :05, ...), so points on the same rate share timestamps and
    line up in an export. A reading is stamped with the second it was asked for.
  * A controller still answering the last read is not asked again until it has; one the
    collector has found dark is not asked at all. After three silent tries its runs stop
    ("no answer").
  * Readings go to `fast_samples`, not `samples`: the alarm rules, the watchdog's "data
    stopped" check and the rollups keep judging the 2-minute record alone.

Caps (config `realtime`): points running at once in total and per controller, the longest
run, and the rates on offer. The defaults are in DEFAULTS.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULTS = {"max_points": 20, "max_per_controller": 10, "max_hours": 24,
            "intervals_s": [5, 10, 15, 30, 60]}
SILENT_TRIES = 3           # silent reads in a row before a controller's runs stop
MAX_SERIES_ROWS = 3000     # per point per data request; more is thinned (min and max kept)


def limits(config: Any) -> dict:
    """The caps, from config `realtime`, over DEFAULTS."""
    node = config.get("realtime") if config is not None and hasattr(config, "get") else None
    node = node.as_dict() if hasattr(node, "as_dict") else (node or {})
    out = dict(DEFAULTS)
    for k in ("max_points", "max_per_controller", "max_hours"):
        try:
            if node.get(k) is not None:
                out[k] = max(1, int(node[k]))
        except (TypeError, ValueError):
            pass
    iv = node.get("intervals_s")
    if isinstance(iv, (list, tuple)):
        try:
            out["intervals_s"] = sorted({max(1, int(v)) for v in iv}) or DEFAULTS["intervals_s"]
        except (TypeError, ValueError):
            pass
    return out


def plan_start(running: list[dict], wanted: list[dict], readable: dict[int, dict],
               lim: dict, now: int) -> tuple[list[tuple[int, int, int]], Optional[str]]:
    """Check a Start against the caps. `wanted` is [{id, minutes, interval_s}], `readable`
    the points that can be read (DAL.fast_points), `running` the open runs. Returns
    ([(point_id, interval_s, ends_utc)], None), or ([], what is wrong) in plain words.
    A point already running is started again: its new rate and end replace the old."""
    items = []
    for w in wanted:
        pid = int(w["id"])
        if pid not in readable:
            return [], f"point {pid} can't be read live: it is not a BACnet value on a controller"
        iv, mins = int(w["interval_s"]), int(w["minutes"])
        if iv not in lim["intervals_s"]:
            return [], (f"the rate must be one of {', '.join(map(str, lim['intervals_s']))} "
                        "seconds")
        if not 1 <= mins <= lim["max_hours"] * 60:
            return [], f"a run lasts from 1 minute to {lim['max_hours']} hours"
        items.append((pid, iv, now + mins * 60))
    after = {r["point_id"]: r["device_id"] for r in running}
    after.update({pid: readable[pid]["device_id"] for pid, _iv, _e in items})
    if len(after) > lim["max_points"]:
        busy = len({r["point_id"] for r in running})
        return [], (f"that would be {len(after)} points running; the limit is "
                    f"{lim['max_points']} at once ({busy} running now). Stop some first, or "
                    "start fewer.")
    per: dict = defaultdict(int)
    for dev in after.values():
        per[dev] += 1
    names = {p["device_id"]: p.get("device_name") for p in [*running, *readable.values()]}
    for dev, n in per.items():
        if n > lim["max_per_controller"]:
            return [], (f"that would be {n} points on one controller "
                        f"({names.get(dev) or dev}); the limit is {lim['max_per_controller']} "
                        "per controller, so it keeps answering the building's own system.")
    return items, None


def thin(rows: list, n: int = MAX_SERIES_ROWS) -> list:
    """At most about `n` rows, keeping each stretch's lowest and highest reading in time
    order, so a spike survives. Rows are (ts, value, ...); a None value is a gap and kept."""
    if len(rows) <= n:
        return rows
    per = max(1, len(rows) * 2 // n)
    out = []
    for i in range(0, len(rows), per):
        chunk = rows[i:i + per]
        vals = [r for r in chunk if r[1] is not None]
        if len(vals) < len(chunk):
            out.append(next(r for r in chunk if r[1] is None))   # keep the gap visible
        if vals:
            lo = min(vals, key=lambda r: r[1])
            hi = max(vals, key=lambda r: r[1])
            out.extend(sorted({id(lo): lo, id(hi): hi}.values(), key=lambda r: r[0]))
    out.sort(key=lambda r: r[0])
    return out


class Realtime:
    """The realtime trends' reader. One per collector; `run()` is started by Collector.run()
    and ends with it."""

    def __init__(self, collector: Any) -> None:
        self._col = collector
        self._runs: list[dict] = []
        self._next: dict[int, int] = {}          # run id -> its next slot (epoch s)
        self._busy: set[str] = set()             # controllers with a read in flight
        self._silent: dict[str, int] = {}        # controller -> silent reads in a row
        self._tasks: set = set()
        self._dirty = True
        self._wake: Optional[asyncio.Event] = None

    def poke(self) -> None:
        """The runs changed (a Start or a Stop): read them again now."""
        self._dirty = True
        if self._wake is not None:
            self._wake.set()

    async def run(self) -> None:
        dal = self._col._dal
        if not hasattr(dal, "fast_runs_open"):        # a test double without the tables
            return
        self._wake = asyncio.Event()
        try:
            while not self._col._stop.is_set():
                try:
                    await self.tick(int(time.time()))
                except Exception:  # noqa: BLE001 -- never take the collector down with it
                    logger.exception("realtime tick failed")
                self._wake.clear()
                try:                             # to the next whole second, or a poke
                    await asyncio.wait_for(self._wake.wait(), 1.0 - time.time() % 1.0)
                except asyncio.TimeoutError:
                    pass
        finally:
            for t in list(self._tasks):
                t.cancel()

    async def tick(self, now: int) -> None:
        dal = self._col._dal
        if dal.finish_fast_runs(now):
            self._dirty = True
        if self._dirty:
            self._runs = dal.fast_runs_open()
            live = {r["run_id"] for r in self._runs}
            self._next = {k: v for k, v in self._next.items() if k in live}
            self._dirty = False
        due: dict[tuple, list[dict]] = defaultdict(list)
        for r in self._runs:
            iv = int(r["interval_s"])
            nxt = self._next.get(r["run_id"], now)          # a new run reads at once
            if now >= nxt:
                due[(r["device_id"], r["address"])].append(r)
                self._next[r["run_id"]] = (now // iv + 1) * iv
        for (device_id, address), runs in due.items():
            if address in self._busy:
                continue                         # still on the last read: this slot is lost
            if address in getattr(self._col, "_dark", {}):
                self._note_silent(address, now)  # the collector already gets no answer
                continue
            self._busy.add(address)
            t = asyncio.create_task(self._read(device_id, address, runs, now),
                                    name=f"realtime-{address}")
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)

    async def _read(self, device_id: Any, address: str, runs: list[dict], now: int) -> None:
        try:
            pts = [{"point_id": r["point_id"], "object_type": r["object_type"],
                    "object_instance": r["object_instance"],
                    "rpm_supported": r.get("rpm_supported")} for r in runs]
            samples, health = await self._col._poll_device(address, device_id, pts, now)
            self._col._dal.write_fast_samples(samples)
            if health.method == "no-answer":
                self._note_silent(address, now)
            else:
                self._silent.pop(address, None)
        except Exception:  # noqa: BLE001 -- one controller's failure is not the others'
            logger.exception("realtime read of %s failed", address)
        finally:
            self._busy.discard(address)

    def _note_silent(self, address: str, now: int) -> None:
        n = self._silent.get(address, 0) + 1
        self._silent[address] = n
        if n < SILENT_TRIES:
            return
        ids = [r["run_id"] for r in self._runs if r["address"] == address]
        if ids:
            self._col._dal.stop_fast_runs(ids, now, reason="no answer")
            logger.warning("realtime: controller %s gave no answer %d times; its %d run(s) "
                           "stopped", address, n, len(ids))
        self._silent.pop(address, None)
        self._dirty = True
