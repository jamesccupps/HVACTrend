#!/usr/bin/env python
"""External collector watchdog — the safety net the in-app check CANNOT provide.

If the collector process hangs, dies, or the machine sleeps, it cannot alert on its own
(a suspended process reports nothing — this actually happened: a ~2.3 h overnight data
gap when the dev laptop slept). So this runs as a SEPARATE Task Scheduler job every few
minutes, reads the database DIRECTLY (no dependency on the API/collector being up), and
raises an alert when data has stopped flowing or a device has gone dark.

    python scripts/watchdog.py             # one check, prints a summary
    python scripts/watchdog.py --quiet     # print only when something is wrong

Exit code 0 = healthy, 1 = alert(s) active, 2 = config/DB error.

Read-only wrt BACnet: it only reads stored samples/poll-health and writes alert rows.
Alerts are written with source='watchdog' so the in-app pre-screen check never clears
them (and vice versa).

Delivery: after detecting, notify.run() sends new alerts, their all-clears and the daily
check-in, and notify.run_incidents() / run_reminders() the equipment alarms and a daily
reminder of those still on, to the operator's phone through ntfy -- once a topic has been generated on the Settings
page. Until then it sends nothing and writes nothing. It lives HERE, not in the server, so that a
dead server cannot also silence the message saying so (src/hvactrend/notify.py).
"""

import argparse
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
import os  # noqa: E402
os.chdir(_REPO)   # anchor to repo root (Task Scheduler cwd defaults to System32)

from hvactrend import notify, secrets_store  # noqa: E402
from hvactrend.config import ConfigError, load_config  # noqa: E402
from hvactrend.models import Alert  # noqa: E402
from hvactrend.storage.sqlite_dal import SQLiteDAL  # noqa: E402

_SOURCE = "watchdog"
_DEFAULT_STALE_S = 900          # no new samples for 15 min (poll is 120 s) -> stalled
_DEFAULT_DEVICE_STALE_S = 1800  # a device silent this long -> that trunk is dark
# A controller that answers, but not for some of its points. 2026-10-13: the tower panel reset
# and reloaded an old database; 45 of its 84 logged points (both basin level setpoints, the
# pump status points, the loop DP and the master enables) did not exist for 27 hours, and
# nothing said so, because the panel itself kept answering.
_DEFAULT_POINTS_MISSING_MIN = 3     # points not answering...
_DEFAULT_POINTS_MISSING_CYCLES = 3  # ...in this many consecutive cycles (6 min at 120 s)


def _cfg_num(config, key: str, default: float) -> float:
    try:
        node = config.get("alerts") if config else None
        if node:
            d = node.as_dict() if hasattr(node, "as_dict") else dict(node)
            return float(d.get(key, default))
    except (AttributeError, TypeError, ValueError):
        pass
    return default


def check(dal, config, now_utc: int) -> dict:
    """Evaluate collector liveness. Raises/resolves `watchdog`-source alerts. Returns a
    summary dict: {raised, resolved, open, problems:[str]}."""
    stale_s = _cfg_num(config, "stale_after_s", _DEFAULT_STALE_S)
    dev_stale_s = _cfg_num(config, "device_stale_after_s", _DEFAULT_DEVICE_STALE_S)
    miss_min = int(_cfg_num(config, "points_missing_min", _DEFAULT_POINTS_MISSING_MIN))
    miss_cycles = int(_cfg_num(config, "points_missing_cycles", _DEFAULT_POINTS_MISSING_CYCLES))

    active: list[str] = []
    problems: list[str] = []
    raised = 0

    def raise_alert(kind, severity, message, system_code=None):
        nonlocal raised
        fp = f"{kind}:{system_code or '-'}"
        active.append(fp)
        _id, is_new = dal.upsert_alert(Alert(
            kind=kind, severity=severity, message=message, fingerprint=fp,
            created_utc=now_utc, last_seen_utc=now_utc, system_code=system_code,
            source=_SOURCE))
        problems.append(message)
        if is_new:
            raised += 1

    # 1) is data flowing at all? (the headline check — covers dead/hung/asleep collector)
    # Only samples up to now count: after a VM restore or an NTP jump, readings stamped in the
    # future made the age negative and switched this check off until real time caught up
    # (2026-11-06 review, M14). Those are an alert of their own.
    newest = dal.newest_sample_ts(until=now_utc + 300)
    ahead = dal.newest_sample_ts()
    if ahead is not None and ahead > now_utc + 300:
        raise_alert("clock-ahead", "high",
                    f"Readings are stamped up to {(ahead - now_utc) // 60} min AHEAD of this "
                    f"computer's clock (newest {time.strftime('%Y-%m-%d %H:%M', time.localtime(ahead))})."
                    " Was the clock changed, or a virtual machine restored? The data-stopped "
                    "check ignores them.")
    if newest is None:
        raise_alert("collector-stale", "critical",
                    "No samples have EVER been recorded — the collector has not run.")
    else:
        age = now_utc - int(newest)
        if age > stale_s:
            raise_alert("collector-stale", "critical",
                        f"No new samples for {age // 60} min (threshold "
                        f"{int(stale_s) // 60} min) — collector down, hung, or the host "
                        f"is asleep. Newest sample "
                        f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(int(newest)))}.")

    # 2) per-device: a trunk that stopped reporting or whose last cycle failed
    try:
        # A controller with no point left to log is not polled, so its silence is expected:
        # it used to raise "not responding" for ever (2026-10-27 review).
        polled = {t["address"] for t in dal.poll_targets()}
        for d in dal.poll_health_latest():
            if d.get("address") not in polled:
                continue
            label = d.get("name") or f"device {d.get('device_instance')}"
            ts = d.get("ts_utc")
            if ts is not None and (now_utc - int(ts)) > dev_stale_s:
                raise_alert("device-stale", "high",
                            f"{label}: no poll result for "
                            f"{(now_utc - int(ts)) // 60} min — controller/trunk dark.",
                            system_code=str(d.get("device_instance")))
            elif not d.get("ok"):
                raise_alert("device-poll-failed", "high",
                            f"{label}: last poll cycle FAILED "
                            f"({d.get('reads_ok')}/{d.get('reads_attempted')} reads ok"
                            + (f"; {d.get('error')}" if d.get("error") else "") + ").",
                            system_code=str(d.get("device_instance")))
            elif ((d.get("reads_attempted") or 0) - (d.get("reads_ok") or 0)) >= miss_min:
                msg = _points_missing(dal, d, label, now_utc, miss_min, miss_cycles)
                if msg:
                    raise_alert("device-points-missing", "high", msg,
                                system_code=str(d.get("device_instance")))
    except Exception as exc:  # noqa: BLE001 — never let a secondary check break the primary
        print(f"watchdog: device check skipped ({exc})", file=sys.stderr)
        # An unfinished check proves nothing about the devices it did not reach: keep their
        # alerts open. resolve_missing_alerts below used to close them all and send an
        # all-clear for controllers nobody had looked at (2026-10-27 review).
        active.extend(a["fingerprint"] for a in dal.open_alerts()
                      if a.get("source") == _SOURCE
                      and str(a.get("kind") or "").startswith("device-"))

    resolved = dal.resolve_missing_alerts(active, now_utc, source=_SOURCE)
    return {"raised": raised, "resolved": len(resolved),
            "open": len([a for a in dal.open_alerts() if a.get("source") == _SOURCE]),
            "problems": problems}


def _points_missing(dal, d: dict, label: str, now_utc: int, miss_min: int,
                    miss_cycles: int):
    """The alert text when `d` has had at least `miss_min` points unanswered in each of its
    last `miss_cycles` cycles, else None. "Since" is the first of the unbroken run of short
    cycles (a cycle with no answer at all, the reset itself, ends the run: that one is the
    device-poll-failed alert's)."""
    hist = dal.poll_health_history(d["device_instance"], now_utc - 3 * 86400, int(d["ts_utc"]))
    run = []
    for h in hist:                                  # newest first
        if not h.get("ok") or ((h.get("reads_attempted") or 0) - (h.get("reads_ok") or 0)) < miss_min:
            break
        run.append(h)
    if len(run) < miss_cycles:
        return None
    since = run[-1]["ts_utc"]
    names = [f"{p.get('system_code') or ''} "
             f"{p.get('display_name') or (p.get('name') or '').split('|')[0]}".strip()
             for p in dal.unanswered_points(d["device_instance"], d["ts_utc"])]
    n, att = (d.get("reads_attempted") or 0) - (d.get("reads_ok") or 0), d.get("reads_attempted")
    shown = ", ".join(names[:6]) + (f" and {len(names) - 6} more" if len(names) > 6 else "")
    started = ("at least " if len(run) == len(hist) else "") + time.strftime(
        "%a %m-%d %H:%M", time.localtime(int(since)))
    return (f"{label}: {n} of {att} logged points not answering since {started}, while the "
            f"controller itself answers (a reset or a reloaded database?)"
            + (f": {shown}." if shown else "."))


_FAILING_MARK = _REPO / "data" / ".watchdog-failing"


def _last_resort(what: str, config=None) -> None:
    """The watchdog cannot do its job (no config, no database, its checks crashed): say so
    on the phone with nothing but the topic from secrets.env. Without this, one bad indent in
    config.yaml stopped every alarm and the morning check-in, and the only sign was something
    NOT arriving (2026-11-06 review, H3). At most once an hour; never raises."""
    try:
        if _FAILING_MARK.exists() and time.time() - _FAILING_MARK.stat().st_mtime < 3600:
            return
        tgt = notify.target(config)        # config None: the topic and the default server
        if not tgt:
            return
        ok, _detail = notify.post(tgt, notify.Message(
            "HVACTrend watchdog cannot run", 5,
            [what[:400], "Until this is fixed, no data-stopped or equipment alarm reaches "
                         "this phone, and there is no morning check-in."]))
        if ok:
            _FAILING_MARK.parent.mkdir(parents=True, exist_ok=True)
            _FAILING_MARK.touch()
    except Exception:  # noqa: BLE001 -- the last resort must not raise
        pass


def _recovered(config) -> None:
    """A run got through after _last_resort spoke: say it is back."""
    try:
        if not _FAILING_MARK.exists():
            return
        _FAILING_MARK.unlink()
        tgt = notify.target(config)
        if tgt:
            notify.post(tgt, notify.Message("HVACTrend watchdog is running again", 3,
                                            ["Alarms and the check-in are back to normal."]))
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    p = argparse.ArgumentParser(description="External collector watchdog.")
    p.add_argument("--config", default="config/config.yaml")
    p.add_argument("--quiet", action="store_true", help="print only when unhealthy")
    args = p.parse_args()

    try:
        secrets_store.load_into_env()     # the ntfy topic lives in config/secrets.env
    except Exception as exc:  # noqa: BLE001 -- nothing to push with; still say it here
        print(f"cannot read the secrets file: {type(exc).__name__}", file=sys.stderr)
    try:
        cfg = load_config(args.config)
    except Exception as exc:  # noqa: BLE001 -- ConfigError, a missing file, a YAML syntax error
        print(f"config error: {exc}", file=sys.stderr)
        _last_resort(f"{args.config} cannot be read: {type(exc).__name__}: {exc}")
        return 2
    try:
        dal = SQLiteDAL(cfg.storage.path, cfg.get("storage").get("schema_file"))
        # Wait out a writer (the server, a backup) rather than failing in Python's 5 s.
        dal._conn.execute("PRAGMA busy_timeout = 30000")
    except Exception as exc:  # noqa: BLE001 — DB unreachable is itself the failure mode
        print(f"cannot open database: {exc}", file=sys.stderr)
        _last_resort(f"the database cannot be opened: {type(exc).__name__}: {exc}", cfg)
        return 2

    try:
        now = int(time.time())
        try:
            res = check(dal, cfg, now)
        except Exception as exc:  # noqa: BLE001 -- locked, full disk, corrupt
            print(f"watchdog: the checks failed: {exc}", file=sys.stderr)
            _last_resort(f"the watchdog's checks failed: {type(exc).__name__}: {exc}", cfg)
            return 2
        _recovered(cfg)
        # Delivery of the watchdog's OWN alerts (data stopped, a controller dark) and the
        # check-in comes BEFORE the rules pass: after downtime that pass replays days of data
        # and could delay, or outlast, the one message that matters most (2026-11-06 review).
        # Fenced off from detection: a failure to send must never change what was detected,
        # the alert rows written, or the exit code.
        try:
            sent = notify.run(dal, cfg, now)
        except Exception as exc:  # noqa: BLE001
            # The exception TYPE only -- its text could carry the webhook URL.
            sent = {"errors": [f"delivery raised {type(exc).__name__}"]}
        # The rule-based alarms, fenced the same way: a rules failure must never change the
        # liveness check, its alerts or the exit code. Imported HERE, not at the top, so even
        # a module that fails to import cannot stop the watchdog.
        rules_out: dict = {}
        try:
            from hvactrend.rules import live as rules_live
            rules_out = rules_live.run(dal, cfg, now)
        except Exception as exc:  # noqa: BLE001
            rules_out = {"error": f"{type(exc).__name__}: {exc}"}
            try:
                dal.set_meta("rules.last_error", f"{now} {rules_out['error']}"[:500])
            except Exception:  # noqa: BLE001 -- the report of the failure must not fail
                pass
        # A push step that raises is recorded (its type only) for the check-in to report:
        # otherwise the equipment alarms could stop reaching the phone with no sign at all.
        try:
            pushed = notify.run_incidents(dal, cfg, now)
            pushed.setdefault("errors", []).extend(
                notify.run_reminders(dal, cfg, now)["errors"])
            if dal.get_meta("notify.incident_error") is not None:
                dal.set_meta("notify.incident_error", None)
        except Exception as exc:  # noqa: BLE001
            pushed = {"errors": [f"incident delivery raised {type(exc).__name__}"]}
            try:
                dal.set_meta("notify.incident_error", f"{now} {type(exc).__name__}")
            except Exception:  # noqa: BLE001 -- the report of the failure must not fail
                pass
        sent.setdefault("errors", []).extend(pushed.get("errors") or [])
    finally:
        dal.close()

    if sent.get("errors"):
        print(f"watchdog: alert delivery failed: {sent['errors'][0]}", file=sys.stderr)
    elif not args.quiet and (sent.get("alerts") or sent.get("cleared") or sent.get("checkin")):
        print(f"watchdog: sent to the phone: {sent.get('alerts', 0)} alert(s), "
              f"{sent.get('cleared', 0)} all-clear(s)"
              + (f", the daily check-in ({sent['checkin']})" if sent.get("checkin") else ""))

    if rules_out.get("error"):
        print(f"watchdog: the alarm rules failed: {rules_out['error']}", file=sys.stderr)
    elif rules_out.get("errors"):
        print(f"watchdog: {len(rules_out['errors'])} alarm rule(s) failed: "
              f"{rules_out['errors'][0]}", file=sys.stderr)
    elif not args.quiet and rules_out.get("new"):
        print(f"watchdog: {len(rules_out['new'])} new equipment incident(s)")

    if res["problems"]:
        print(f"WATCHDOG: {len(res['problems'])} problem(s) "
              f"({res['raised']} newly raised, {res['resolved']} resolved):")
        for m in res["problems"]:
            print(f"  - {m}")
        return 1
    if not args.quiet:
        print(f"watchdog: healthy (data flowing; {res['resolved']} alert(s) resolved)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
