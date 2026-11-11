"""The poll rate: how often the collector reads every logged point, and when that last changed.

A site that moves from every 2 minutes to every minute changes what a day owes. Several things judge the
record against the rate (the morning check-in's "cycles of expected", the summary's data
health), and a day that straddles a change reads as short at the new rate: every controller
"short of its cycles" on the morning after. So the collector notes the rate in app_meta when
it starts, and when it differs from the one noted before, the moment it changed. Those counts
then start from the change on the day it happens.

A database that has never had a rate noted gets the rate and no change time: there is no
telling whether the rate before was the same, and a change time would shorten that day's
counts for nothing.
"""

from __future__ import annotations

from typing import Any, Optional

META_RATE = "poll.interval_s"
META_SINCE = "poll.interval_since"
DEFAULT_S = 120.0


def interval(config: Any) -> float:
    """bacnet.poll.interval_s from a config dict or a Config object; 120 when unset."""
    node = config
    for k in ("bacnet", "poll", "interval_s"):
        if node is None or not hasattr(node, "get"):
            return DEFAULT_S
        node = node.get(k)
    try:
        v = float(node)
    except (TypeError, ValueError):
        return DEFAULT_S
    return v if v > 0 else DEFAULT_S


def note(dal: Any, interval_s: float, now_utc: int) -> Optional[float]:
    """Record the rate the collector is starting with. Returns the previous rate when it
    changed (and stamps the change), else None."""
    prev = dal.get_meta(META_RATE)
    rate = float(interval_s)
    if prev is not None:
        try:
            if float(prev) == rate:
                return None
        except ValueError:
            pass
    dal.set_meta(META_RATE, f"{rate:g}")
    if prev is None:
        return None
    dal.set_meta(META_SINCE, int(now_utc))
    try:
        return float(prev)
    except ValueError:
        return None


def changed_at(dal: Any) -> Optional[int]:
    """When the rate last changed, or None if it never has (as far as this database knows)."""
    try:
        v = dal.get_meta(META_SINCE)
        return int(float(v)) if v is not None else None
    except Exception:  # noqa: BLE001 -- a count is never worth failing a report over
        return None


def counted_from(dal: Any, t0: int, t1: int) -> int:
    """Where a [t0, t1) count of poll cycles should start: the rate change, when it fell
    inside the window, else t0."""
    since = changed_at(dal)
    return since if since is not None and t0 < since < t1 else t0


def label(interval_s: float) -> str:
    """'1-min', '2-min', '30-s': the rate as the viewer and the exports name it."""
    s = float(interval_s)
    if s >= 60 and s % 60 == 0:
        return f"{int(s // 60)}-min"
    return f"{s:g}-s"
