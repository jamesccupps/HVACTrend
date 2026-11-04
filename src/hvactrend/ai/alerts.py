"""Between-report alerting — proactive monitoring (the 24-hour blind spot).

The daily/weekly AI reports run once a day/week. A HARD fault — a sensor reading
fault/out-of-service, a controller that stopped communicating, a value out of range —
should not wait up to 24 h to surface. This module runs the DETERMINISTIC pre-screen
over a short recent window every few minutes, and raises + tracks high/critical
conditions as `alerts` (deduped by fingerprint, auto-resolved when the condition clears).

No LLM, no API cost — pure deterministic detectors (the same ones the reports use as
leads). These alerts are IN-APP ONLY (a banner + GET /api/alerts). They used to be posted
to an optional generic webhook as raw JSON; that path was removed on 2026-09-29 when real
delivery arrived (notify.py), for three reasons: no actual channel could read that JSON, it
sent every source's alerts including this pre-screen's orphans, and two delivery paths is
one too many.
When the pre-screen returns its incidents go through notify.py.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..models import Alert, Severity
from .prescreen import screen

log = logging.getLogger("hvactrend.ai.alerts")

_DEFAULTS = {"enabled": True, "check_interval_s": 300.0, "window_s": 1800,
             "min_severity": "high"}


def _alert_cfg(config: Any) -> dict:
    """Read the `alerts` config block, with safe defaults."""
    out = dict(_DEFAULTS)
    try:
        node = config.get("alerts") if config else None
        if node:
            d = node.as_dict() if hasattr(node, "as_dict") else dict(node)
            out["enabled"] = bool(d.get("enabled", out["enabled"]))
            out["check_interval_s"] = float(d.get("check_interval_s", out["check_interval_s"]))
            out["window_s"] = int(d.get("window_s", out["window_s"]))
            out["min_severity"] = str(d.get("min_severity", out["min_severity"])).lower()
    except (AttributeError, TypeError, ValueError):
        pass
    return out


def check_interval_s(config: Any) -> float:
    """How often the collector should call evaluate_alerts (seconds)."""
    return _alert_cfg(config)["check_interval_s"]


def _min_rank(name: str) -> int:
    try:
        return Severity(name).rank()
    except ValueError:
        return Severity.HIGH.rank()


def _fingerprint(a: Any) -> str:
    """Stable per-condition key: kind + unit + point. The same live fault updates in
    place across cycles instead of raising a new alert every few minutes."""
    return f"{a.kind}:{a.system_code or '?'}:{a.point_id or 0}"


def evaluate_alerts(dal: Any, config: Any, now_utc: int, *,
                    window_s: Optional[int] = None) -> dict:
    """Run ONE alert-evaluation cycle: deterministic pre-screen over the recent window ->
    raise/track high+critical conditions -> auto-resolve cleared ones. In-app only.
    Returns a summary dict. Never raises for a data condition; callers still wrap it."""
    cfg = _alert_cfg(config)
    if not cfg["enabled"]:
        return {"enabled": False, "raised": 0, "resolved": 0, "open": 0}
    window = window_s if window_s is not None else cfg["window_s"]
    anomalies = screen(dal, now_utc - window, now_utc, config=config)
    min_rank = _min_rank(cfg["min_severity"])

    active_fps: list[str] = []
    raised: list[dict] = []
    for a in anomalies:
        if a.severity.rank() < min_rank:
            continue
        fp = _fingerprint(a)
        if fp in active_fps:            # one alert per condition even if several detectors fire
            continue
        active_fps.append(fp)
        alert = Alert(kind=a.kind, severity=a.severity.value, message=a.detail,
                      fingerprint=fp, created_utc=now_utc, last_seen_utc=now_utc,
                      system_code=a.system_code, point_id=(a.point_id or None),
                      point_name=a.point_name)
        aid, is_new = dal.upsert_alert(alert)
        if is_new:
            log.warning("ALERT raised [%s] %s: %s", a.severity.value, a.system_code, a.detail)
            raised.append({"id": aid, "kind": a.kind, "severity": a.severity.value,
                           "system_code": a.system_code, "message": a.detail})

    # scoped to our own source: never clears the external watchdog's alerts
    resolved = dal.resolve_missing_alerts(active_fps, now_utc, source="prescreen")
    for r in resolved:
        log.info("ALERT resolved: %s", r.get("message"))

    return {"enabled": True, "raised": len(raised), "resolved": len(resolved),
            "open": len(dal.open_alerts()), "raised_detail": raised}
