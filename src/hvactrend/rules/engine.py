"""The incident machine: when does a breach become an incident, and when is it over?

Pure. It sees the samples of ONE rule on ONE unit (or one point) as (t, value, judged) and
the rule's tiers, and returns episodes. No I/O, so it is tested directly, and the 30-day
replay runs exactly the code the live evaluation runs.

Debounce: a tier fires only after its breach has held for its minutes, over judged samples
with no gap longer than `max_gap_s` (a missed poll or two is not a recovery).

Hysteresis: an open episode closes only after `clear_s` of judged samples that pass the
rule's `clear` test, which sits inside the tier threshold. A value hovering at the threshold
therefore cannot open and close an incident every few minutes: 64 % of the old AI layer's
alerts flapped shut within 15 minutes.

Judged = the unit is running, past its start-up settle, and has the data (state.py decides).
Unjudged time neither extends nor ends an episode: an AHU that shuts down for the night with
an open incident keeps it until it runs again and is seen to be fine, or until it has not been
judged for `idle_close_s` (a long weekend, a unit taken out of service).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

TIER_ORDER = {"watch": 0, "action": 1, "critical": 2}


@dataclass
class Tier:
    name: str                              # watch | action | critical
    minutes: float                         # how long the breach must hold
    breach: Callable[[float], bool]        # is this value bad enough for this tier?
    label: str = ""                        # e.g. "5 °F above setpoint for 30 min"


@dataclass
class Episode:
    start: int                             # first sample of the breach run that fired
    opened: int                            # when the first tier fired
    tier: str
    escalations: list = field(default_factory=list)     # [(tier, t)]
    peak: Optional[float] = None           # the worst value seen
    peak_t: Optional[int] = None
    last_breach: Optional[int] = None
    occurrences: int = 1                   # separate breach runs that fired a tier
    end: Optional[int] = None              # None = still open when the data ends
    reason: str = ""                       # 'cleared' | 'not judged'


def episodes(samples: Iterable[tuple], tiers: list, *, clear: Callable[[float], bool],
             clear_s: float, max_gap_s: float = 360, idle_close_s: float = 3 * 86400,
             worse: Callable[[float, float], bool] = lambda a, b: a > b,
             initial: Optional[Episode] = None, until: Optional[int] = None) -> list:
    """Episodes over time-ordered (t, value, judged) samples. `worse(a, b)` says whether `a`
    is a worse value than `b` (larger by default; a boiler's temperature RISE is worse when
    smaller). `initial` is an episode already open when these samples start (seeded from the
    stored incident), and `until` is the evaluation time, for the idle check at the end.
    Occurrences are counted within one call."""
    names = [t.name for t in tiers]
    run_start = dict.fromkeys(names)        # where each tier's current breach run began
    run_worst: Optional[tuple] = None       # (value, t) worst sample of the current breach
    fired_this_run = False
    # A seeded incident has already counted every breach up to its last one. Live runs re-read
    # overlapping 4-hour windows every 5 minutes, so a breach run that BEGAN at or before that
    # point is history, not a new spell; counting it added a phantom "separate spell" (a test
    # caught it the day it was written).
    seed_last = (initial.last_breach or initial.opened) if initial is not None else None
    cur = initial
    clear_start: Optional[int] = None
    last_judged: Optional[int] = None
    # the idle clock for a seeded episode starts from its own last breach, not from the
    # first sample of this call, or an episode no sample ever judges again never closes
    seeded_ref = (initial.last_breach or initial.opened) if initial is not None else None
    out: list = []

    def reset_runs() -> None:
        nonlocal run_worst, fired_this_run
        for k in names:
            run_start[k] = None
        run_worst, fired_this_run = None, False

    for t, v, judged in samples:
        if not judged or v is None:
            reset_runs()                    # a stopped unit is neither a breach nor a recovery
            clear_start = None
            continue
        if last_judged is not None and t - last_judged > max_gap_s:
            reset_runs()
            clear_start = None
        prev = last_judged if last_judged is not None else seeded_ref
        if cur is not None and prev is not None and t - prev > idle_close_s:
            cur.end, cur.reason = prev, "not judged"
            out.append(cur)
            cur = None
        last_judged = t

        breaching = False
        for tier in tiers:
            if not tier.breach(v):
                run_start[tier.name] = None
                continue
            breaching = True
            if run_start[tier.name] is None:
                run_start[tier.name] = t
            if t - run_start[tier.name] < tier.minutes * 60:
                continue
            if cur is None:                                     # opens
                cur = Episode(start=run_start[tier.name], opened=t, tier=tier.name)
                fired_this_run = True
            elif not fired_this_run:                            # recurs while still open
                if seed_last is None or run_start[tier.name] > seed_last:
                    cur.occurrences += 1
                fired_this_run = True
            if TIER_ORDER[tier.name] > TIER_ORDER[cur.tier]:  # escalates
                cur.escalations.append((tier.name, t))
                cur.tier = tier.name

        if breaching:
            if run_worst is None or worse(v, run_worst[0]):
                run_worst = (v, t)
        else:
            run_worst, fired_this_run = None, False

        if cur is None:
            continue
        if seed_last is not None and t <= seed_last:
            # History the seeded incident already holds. It still times the tiers above (so a
            # CRITICAL breach can build up across 5-minute runs), but it can neither extend the
            # incident nor END it: the quiet hours BEFORE an incident began are not a recovery.
            # (Live 2026-10-01/02: nine incidents closed on the run after they opened, "ending"
            # hours before they started, and the dedupe then kept them closed.)
            continue
        if breaching:
            cur.last_breach, clear_start = t, None
            if run_worst and (cur.peak is None or worse(run_worst[0], cur.peak)):
                cur.peak, cur.peak_t = run_worst
        elif clear(v):
            clear_start = t if clear_start is None else clear_start
            if t - clear_start >= clear_s:
                cur.end, cur.reason = clear_start, "cleared"
                out.append(cur)
                cur, clear_start = None, None
        else:
            clear_start = None              # inside the hysteresis band: neither

    if cur is not None:
        prev = last_judged if last_judged is not None else seeded_ref
        if until is not None and prev is not None and until - prev > idle_close_s:
            cur.end, cur.reason = prev, "not judged"
        out.append(cur)
    return out
