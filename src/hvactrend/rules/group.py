"""One cause, one message: group the findings that open together.

On 2026-09-03 at 16:01 every multistack alarmed, and within 30 minutes eleven air handlers'
discharge air ran 14-22 °F high: some 26 findings from one cause, which one push should
carry. Findings that open within `window_s` of the first one in a cluster, on `min_units` or
more units, become ONE building-wide group. The rest stay per unit, where simultaneous
findings on one unit (HV1 has two points for the same alarm) are also one message.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from .engine import TIER_ORDER


@dataclass
class Group:
    kind: str                      # 'building' | 'unit'
    opened: int                    # when its first finding opened
    tier: str                      # the worst tier among its findings
    units: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    summary: str = ""


def _natural(code: str) -> list:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", code or "")]


def group(findings: list, *, window_s: int = 1800, min_units: int = 3) -> list:
    fs = sorted(findings, key=lambda f: f.episode.opened)
    clusters: list = []
    for f in fs:
        if clusters and f.episode.opened - clusters[-1][0].episode.opened <= window_s:
            clusters[-1].append(f)
        else:
            clusters.append([f])
    out: list = []
    for cl in clusters:
        units = sorted({f.unit for f in cl}, key=_natural)
        if len(units) >= min_units:
            out.append(_make("building", cl))
        else:
            out.extend(_make("unit", [f for f in cl if f.unit == u]) for u in units)
    return out


def _make(kind: str, fs: list) -> Group:
    units = sorted({f.unit for f in fs}, key=_natural)
    tier = max((f.tier for f in fs), key=TIER_ORDER.get)
    if kind == "unit":
        summary = fs[0].detail if len(fs) == 1 else (
            f"{units[0]}: " + "; ".join(sorted({f.title.lower() for f in fs})))
    else:
        by_rule = Counter(f.title for f in fs)
        what = "; ".join(f"{t.lower()} on {n}" for t, n in by_rule.most_common())
        summary = (f"{len(units)} units at once ({', '.join(units)}): {what}. All together "
                   f"points at something they share (the condenser water loop, power, a "
                   f"controller) before the units themselves.")
    return Group(kind=kind, opened=fs[0].episode.opened, tier=tier, units=units,
                 findings=fs, summary=summary)
