"""Is the unit supposed to be doing this right now? The one operating-state gate.

Every rule asks this before it judges anything. Nothing judges discharge air, static pressure
or temperatures while the unit is off, unoccupied, or still settling after a start: the old AI
layer's top two findings on 2026-09-07 were AC-2's nightly shutdown, and it once charged
eleven air handlers with over-temperature for still air sitting at room temperature overnight.

A gate is built from one or more ON-spans (fan proven, occupied, a boiler enabled) and a
settle time that applies after the start of EACH span: a unit switching into occupied mode
needs the same time to pull its discharge air down as one that has just started.

Pure: spans in, a predicate out. `on_spans` follows ai/derived.running_windows (thresholds
and the 15-minute gap tolerance), but lives here so the watchdog never imports the AI layer.
"""

from __future__ import annotations

import bisect
from typing import Callable, Iterable, Optional

# A fan speed is "running" above 10 %, a binary status above 0.5 (ai/derived.py).
FAN_SPEED_ON = 10.0
BINARY_ON = 0.5


def on_spans(series: Iterable[tuple], *, threshold: float = BINARY_ON,
             max_gap_s: float = 900) -> list:
    """[(t0, t1), ...] while the signal is above `threshold`. `series` is (t, value) in time
    order. A data gap longer than `max_gap_s` ends a span: nobody saw it stay on."""
    spans: list = []
    cur: Optional[list] = None
    last_t: Optional[int] = None
    for t, v in series:
        if v is None:
            continue
        on = v > threshold
        if cur is not None and (not on or (last_t is not None and t - last_t > max_gap_s)):
            spans.append((cur[0], cur[1]))
            cur = None
        if on:
            if cur is None:
                cur = [t, t]
            else:
                cur[1] = t
        last_t = t
    if cur is not None:
        spans.append((cur[0], cur[1]))
    return spans


def union(*span_lists: list) -> list:
    """Spans covered by ANY of the lists (two supply fans: either one running is running)."""
    allspans = sorted(s for spans in span_lists for s in spans)
    out: list = []
    for a, b in allspans:
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def make_gate(span_lists: list, settle_s: float = 0.0) -> Callable[[int], bool]:
    """judged(t): t lies inside a span of EVERY list, at least `settle_s` after that span
    began. A list of None is ignored (the unit has no such signal); an EMPTY list means the
    signal exists and was never on, so nothing is judged."""
    prepared = []
    for spans in span_lists:
        if spans is None:
            continue
        spans = sorted(spans)
        prepared.append((spans, [a for a, _ in spans]))

    def judged(t: int) -> bool:
        for spans, starts in prepared:
            i = bisect.bisect_right(starts, t) - 1
            if i < 0:
                return False
            a, b = spans[i]
            if t > b or t - a < settle_s:
                return False
        return True
    return judged


def occupied_series(series: Iterable[tuple]) -> list:
    """Trane occupancy (status or request): 1 occupied, 2 unoccupied, 3 occupied bypass,
    4 occupied standby. On this site only 1 and 2 occur; AC-9, a vacant floor, sits at 2.
    Returned as 1/0 so on_spans can take it."""
    return [(t, None if v is None else (0.0 if round(v) == 2 else 1.0)) for t, v in series]
