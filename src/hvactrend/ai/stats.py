"""Pure statistical helpers for the AI context builder + pre-screen.

Stdlib-only, no I/O — takes the DAL's raw series rows
(`{ts_utc, value_num, value_text, quality}`) and returns compact feature dicts.
Value statistics are computed over GOOD samples only (a faulted 356 F reading
shouldn't drag the mean); fault / comm-loss are counted separately so the
pre-screen can weigh data quality on its own.
"""

from __future__ import annotations

import math
from typing import Any, Optional, Sequence


def summarize(rows: Sequence[dict]) -> dict[str, Any]:
    """Per-point feature summary for one window.

    Returns counts (total / good / fault / comm-loss), the good-value envelope
    (min/max/mean/std/last), and `constant` (all good values identical — the
    flatline signal). Empty / all-bad input yields n>0 with good_n==0 and None
    value stats, which the pre-screen reads as a no-good-data condition.
    """
    n = len(rows)
    good = [r["value_num"] for r in rows
            if r.get("quality") == "good" and r.get("value_num") is not None]
    fault_n = sum(1 for r in rows if r.get("quality") == "fault")
    comm_n = sum(1 for r in rows if r.get("quality") == "comm-loss")

    out: dict[str, Any] = {
        "n": n, "good_n": len(good), "fault_n": fault_n, "comm_n": comm_n,
        "min": None, "max": None, "mean": None, "std": None,
        "last": None, "last_good": None, "last_text": None, "constant": False,
        "first_ts": rows[0]["ts_utc"] if n else None,
        "last_ts": rows[-1]["ts_utc"] if n else None,
    }
    if rows:
        out["last"] = rows[-1].get("value_num")
        out["last_text"] = rows[-1].get("value_text")
    if good:
        lo, hi = min(good), max(good)
        mean = sum(good) / len(good)
        var = sum((x - mean) ** 2 for x in good) / len(good) if len(good) > 1 else 0.0
        # last_good = the most recent GOOD reading (rows are ts-ordered) — deviation
        # z-scores and the "last good value" label must not use a faulted sample.
        out.update(min=lo, max=hi, mean=mean, std=math.sqrt(var),
                   last_good=good[-1], constant=(hi - lo) == 0.0)
    return out


def hourly_downsample(rows: Sequence[dict], start_utc: int, end_utc: int) -> list[dict]:
    """Hourly-average shape of a series — the trend without the noise.

    Buckets GOOD numeric samples into UTC-aligned hours; each bucket carries the
    average and sample count. Empty hours are omitted (gaps stay visible)."""
    buckets: dict[int, list[float]] = {}
    for r in rows:
        if r.get("quality") != "good" or r.get("value_num") is None:
            continue
        h = (r["ts_utc"] // 3600) * 3600
        if h < start_utc or h > end_utc:
            continue
        buckets.setdefault(h, []).append(r["value_num"])
    return [{"h": h, "avg": sum(v) / len(v), "n": len(v)}
            for h, v in sorted(buckets.items())]


def rate_per_hour(summary: dict, rows: Sequence[dict]) -> Optional[float]:
    """Net drift across the window: (last good - first good) / hours. None if <2
    good samples or a zero-length span."""
    good = [(r["ts_utc"], r["value_num"]) for r in rows
            if r.get("quality") == "good" and r.get("value_num") is not None]
    if len(good) < 2:
        return None
    dt_h = (good[-1][0] - good[0][0]) / 3600.0
    if dt_h <= 0:
        return None
    return (good[-1][1] - good[0][1]) / dt_h
