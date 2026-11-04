"""Ingest the BAS's OWN commissioned limits into the app's alarm thresholds.

WHY THIS EXISTS
---------------
The out-of-range detector (`prescreen.detect_point`, check 5) has existed since the
first build, is fully tested, and had **never fired once** in the life of the program.
Not because it was broken — because it reads `points.alarm_hi` / `alarm_lo`, and
**zero points had limits set**. It was waiting for someone to hand-type thresholds.

Meanwhile the plant publishes its own, per unit, on every AHU, and we were already
logging them — they just landed in role `OTHER` where nothing could see them:

    Discharge Air High Limit   70 F (11 units), 73, 72
    Discharge Air Low Limit    55 F (11 units), 69, 65
    Mixed Air Low Limit        52 F (12 units), 48        <- freeze protection
    Return Air High Limit      73 F (10 units), 75
    Return Air Low Limit       65 F (11 units)

These are better than anything we would invent: they are what the controller actually
enforces, they differ per unit for real commissioning reasons (AC-4 runs a 48 F mixed-air
low limit where the fleet uses 52), and they follow the plant if a tech retunes them.

WHY NOT SIMPLY ALARM ON ALL OF THEM
-----------------------------------
Because they are not all alarm thresholds, and assuming they were would have produced a
worse alert flood than the one this same review just fixed. Measured over 14 days of
real data, breach rate WHILE THE UNIT WAS RUNNING (median across units):

    Mixed Air Low Limit         0.0%   <- genuine alarm (freeze protection, never trips)
    Return Air Low Limit        0.0%   <- genuine alarm
    Discharge Air High Limit    1.1%   <- genuine alarm (max 5.5% on one unit)
    Discharge Air Low Limit    39.0%   <- NOT an alarm (max 64%)
    Return Air High Limit      35.5%   <- NOT an alarm (99.9% on AC-5, 90% on AC-13)

A discharge LOW limit is the floor the cooling loop controls toward, so discharge air
sitting at or just under it is normal operation, not a fault. Return-air high behaves
like a comfort target, not a threshold. So: **ingest every limit as context, alarm only
on the ones that measure like alarms.** The rest still reach the AI and the viewer, they
just don't raise.

If the plant is re-commissioned so one of these changes character, the honest move is to
re-measure rather than re-guess — `scripts/` has no runner for that yet, but the query is
the one in this module's docstring history.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .roles import LIMIT_ROLES

logger = logging.getLogger(__name__)

# The limit roles that behave like real alarm thresholds on THIS plant (measured — see
# the module docstring). Overridable via analysis.limits.alarm_roles.
ALARM_LIMIT_ROLES = frozenset({"DAT_HI_LIMIT", "MAT_LO_LIMIT", "RAT_LO_LIMIT"})


def limits_config(config: Any) -> dict:
    """analysis.limits — {from_plant: bool, alarm_roles: [role, ...]}."""
    out = {"from_plant": True, "alarm_roles": set(ALARM_LIMIT_ROLES)}
    try:
        node = config.get("analysis").get("limits") if config else None
        if node:
            d = node.as_dict() if hasattr(node, "as_dict") else dict(node)
            if "from_plant" in d:
                out["from_plant"] = bool(d["from_plant"])
            roles = d.get("alarm_roles")
            if roles is not None:
                out["alarm_roles"] = {str(r).strip().upper() for r in roles}
    except (AttributeError, TypeError, ValueError):
        pass
    return out


def _latest(dal: Any, point_id: int, now_utc: int, lookback_s: int) -> Optional[float]:
    """Most recent good numeric value for a point. Limits are near-constant, so a wide
    lookback is fine and a missing recent sample must NOT silently clear a limit."""
    rows = dal.query_series(point_id, now_utc - lookback_s, now_utc)
    for r in reversed(rows):
        if r.get("quality") == "good" and r.get("value_num") is not None:
            return float(r["value_num"])
    return None


def apply_plant_limits(dal: Any, config: Any = None, *, now_utc: Optional[int] = None,
                       lookback_s: int = 7 * 86400) -> dict:
    """Copy each unit's trended limit points onto the canonical point they bound.

    Sets `alarm_hi`/`alarm_lo` always (so the value is visible and usable as a drift
    target), but only sets `alarm_enabled=1` for roles in the configured alarm set —
    the out-of-range detector is gated on `alarm_enabled`, so a control limit is
    recorded without raising.

    Idempotent; safe to run on every model rebuild. Returns a summary.
    """
    import time
    now = now_utc if now_utc is not None else int(time.time())
    cfg = limits_config(config)
    if not cfg["from_plant"]:
        return {"applied": 0, "alarming": 0, "skipped": "disabled"}

    # points_for_analysis returns every ENABLED point (all roles), with role,
    # role_canonical and system_code — one query serves both lookups below.
    pts = dal.points_for_analysis()
    # canonical target per (system, role) — the point a limit will be written onto
    canon: dict[tuple, dict] = {}
    for p in pts:
        if p.get("role_canonical"):
            canon[(p.get("system_id"), p.get("role"))] = p

    # every enabled limit point, whatever its canonical flag (limits are per unit and
    # there is normally exactly one of each)
    limit_pts = [p for p in pts if p.get("role") in LIMIT_ROLES]

    staged: dict[int, dict] = {}
    unmatched = []
    recorded_only = []
    for lp in limit_pts:
        role = lp["role"]
        target_role, side = LIMIT_ROLES[role]
        tgt = canon.get((lp.get("system_id"), target_role))
        if not tgt:
            unmatched.append(f"{lp.get('system_code') or '?'}:{role}")
            continue
        # ONLY alarm-grade limits may occupy alarm_hi / alarm_lo.
        #
        # `alarm_enabled` is a SINGLE flag per point and the out-of-range detector
        # checks BOTH sides under it (prescreen.detect_point check 5). So writing a
        # control limit into alarm_lo "just for context" and enabling the point for its
        # genuine alarm_hi silently switches the control limit on too — on this plant
        # that would have alarmed the discharge LOW limit, which the cooling loop rides
        # 39% of the time. A non-alarming limit stays visible through its own role
        # (DAT_LO_LIMIT etc. are classified points the context and viewer can render);
        # it does not belong in a column that means "alarm threshold".
        if role not in cfg["alarm_roles"]:
            recorded_only.append(role)
            continue
        val = _latest(dal, lp["id"], now, lookback_s)
        if val is None:
            continue
        ent = staged.setdefault(tgt["id"], {"hi": None, "lo": None,
                                            "code": tgt.get("system_code")})
        # BACnet floats arrive as 55.00002670288086; an unrounded threshold makes a
        # reading of exactly 55.0 "below the limit" forever.
        ent[side] = round(val, 2)

    applied = 0
    for pid, ent in staged.items():
        dal.set_point_limits(pid, alarm_hi=ent["hi"], alarm_lo=ent["lo"],
                             alarm_enabled=True)
        applied += 1
    alarming = applied

    # Clear points this function MANAGES that no longer have a plant limit — the plant
    # stopped publishing one, the point lost canonical status, or the operator narrowed
    # alarm_roles. Without this a withdrawn limit keeps alarming forever against a value
    # nothing maintains, which is the silent-staleness failure this project keeps hitting.
    # Scoped to canonical points of the bounded roles, so nothing else is ever touched.
    bounded_roles = {tr for tr, _ in LIMIT_ROLES.values()}
    cleared = 0
    for p in pts:
        if (p.get("role_canonical") and p.get("role") in bounded_roles
                and p["id"] not in staged
                and (p.get("alarm_hi") is not None or p.get("alarm_lo") is not None
                     or p.get("alarm_enabled"))):
            dal.set_point_limits(p["id"], alarm_hi=None, alarm_lo=None,
                                 alarm_enabled=False)
            cleared += 1

    if unmatched:
        logger.info("plant limits with no canonical target: %s", ", ".join(sorted(unmatched)))
    return {"applied": applied, "alarming": alarming, "cleared": cleared,
            "limit_points": len(limit_pts), "unmatched": len(unmatched),
            "recorded_not_alarming": len(recorded_only)}
