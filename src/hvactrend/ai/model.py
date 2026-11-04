"""The model rebuild: classify point roles, then apply the plant's limits, and build the
learned baselines only when `ai.baselines` asks for them.

ONE place, used by the collector's daily job and by the Point Selection page's
"Rebuild model" button. The button built the baselines regardless: 28 s on the event loop
the BACnet stack shares, for tables nothing reads with the old detection layer off
(2026-10-27 review). The collector had stopped doing that on 2026-09-22 (audit M-8).
"""

from __future__ import annotations

from typing import Any


def baselines_on(config: Any) -> bool:
    ai = config.get("ai") if config else None
    ai = ai.as_dict() if hasattr(ai, "as_dict") else (ai or {})
    return bool(ai.get("baselines", False))


def rebuild(dal: Any, config: Any) -> dict:
    """{roles, limits, baselines}; baselines is None when they are switched off."""
    from .limits import apply_plant_limits
    from .roles import assign_roles
    roles = assign_roles(dal)
    limits = apply_plant_limits(dal, config)      # after roles: it needs them
    baselines = None
    if baselines_on(config):
        from .baseline import build_baselines     # lazy: keeps the import light
        baselines = build_baselines(dal, config)
    return {"roles": roles, "limits": limits, "baselines": baselines}
