"""YAML config loader + validation.

Stdlib-first, minimal deps: PyYAML for parsing, dataclasses for structure. The
loaded config is a plain nested object graph exposing dotted access via
attributes; unknown keys are preserved so the config can evolve without code
changes ahead of the loader.

Validation here is intentionally light but covers the SAFETY-critical bits:
- read-only flags default false and must be booleans,
- required placeholders (local_address, device_instance) are flagged if left
  unfilled, so the collector never silently binds to a bogus address.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional


class ConfigError(ValueError):
    """Raised when the config is missing required fields or has bad types."""


class _Node:
    """Attribute + item access over a nested dict, preserving unknown keys."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getattr__(self, name: str) -> Any:
        try:
            val = self._data[name]
        except KeyError as exc:
            raise AttributeError(name) from exc
        return _Node(val) if isinstance(val, dict) else val

    def get(self, name: str, default: Any = None) -> Any:
        val = self._data.get(name, default)
        return _Node(val) if isinstance(val, dict) else val

    def as_dict(self) -> dict[str, Any]:
        return self._data

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __repr__(self) -> str:
        return f"_Node({sorted(self._data)})"


@dataclass(slots=True)
class Config:
    """Root config object. Access nested values as attributes: config.bacnet.poll.interval_s."""
    _root: _Node

    def __getattr__(self, name: str) -> Any:
        return getattr(self._root, name)

    def get(self, name: str, default: Any = None) -> Any:
        return self._root.get(name, default)

    def as_dict(self) -> dict[str, Any]:
        return self._root.as_dict()


_PLACEHOLDER_PREFIX = "<"   # e.g. "<COLLECTOR_IP>/24"


def _is_placeholder(value: Any) -> bool:
    return isinstance(value, str) and value.strip().startswith(_PLACEHOLDER_PREFIX)


def load_config(path: str | os.PathLike[str]) -> Config:
    """Load and validate a YAML config file.

    Raises ConfigError with an actionable message on structural problems.
    """
    try:
        import yaml  # lazy: keeps the rest of the package importable without PyYAML
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise ConfigError(
            "PyYAML is required to load config (pip install -e .)."
        ) from exc

    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top-level YAML must be a mapping.")

    _validate(data, source=str(path))
    return Config(_Node(data))


def _validate(data: dict[str, Any], *, source: str) -> None:
    """Light structural + SAFETY validation."""
    bacnet = data.get("bacnet")
    if not isinstance(bacnet, dict):
        raise ConfigError(f"{source}: missing 'bacnet' section.")

    safety = bacnet.get("safety", {}) or {}
    for flag in ("allow_writes", "allow_cov"):
        val = safety.get(flag, False)
        if not isinstance(val, bool):
            raise ConfigError(
                f"{source}: bacnet.safety.{flag} must be a boolean (got {val!r})."
            )
    # Hard guardrail: read-only is the default posture.
    if safety.get("allow_writes", False):
        # Not fatal (an operator may knowingly override in a lab), but never silent.
        import logging
        logging.getLogger(__name__).warning(
            "%s: bacnet.safety.allow_writes=true — BACnet writes are UNGATED. "
            "Do not run this against production controllers.", source
        )

    poll = bacnet.get("poll", {}) or {}
    interval = poll.get("interval_s", 120)
    if not isinstance(interval, (int, float)) or interval <= 0:
        raise ConfigError(f"{source}: bacnet.poll.interval_s must be a positive number.")

    storage = data.get("storage")
    if not isinstance(storage, dict) or "path" not in storage:
        raise ConfigError(f"{source}: missing 'storage.path'.")


def unfilled_placeholders(config: Config) -> list[str]:
    """Return dotted paths still holding <PLACEHOLDER> values.

    Deploy-time guard: the collector must refuse to bind BACnet while
    bacnet.local_address / bacnet.device_instance are unfilled, so it never
    connects with a bogus identity (plan guardrail: no invented addresses).
    """
    found: list[str] = []

    def walk(node: Any, prefix: str) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                walk(val, f"{prefix}.{key}" if prefix else key)
        elif _is_placeholder(node):
            found.append(prefix)

    walk(config.as_dict(), "")
    return found


# ── DB-backed setting overrides (the Settings tab, plan §11) ─────────────────
# Top-level config domains the Settings UI / DB MAY override at startup. `bacnet`
# is deliberately EXCLUDED: BACnet identity and the read-only safety posture stay
# YAML/code-only and can NEVER be changed from the network UI (hard guardrail; the
# read-only posture is also re-checked by safety.assert_read_only_default).
SETTABLE_DOMAINS = frozenset({
    # ⚠️ `storage` was here and is deliberately GONE (2026-09-22 audit, L-10).
    # F-05 denied storage.retention.* because a typo there deletes the record. Applying the
    # same test to the rest of the domain left NOTHING that should be editable from a web
    # form -- so the deny-list was denying 100% of the real keys, which is a domain that
    # does not belong in this set:
    #     retention.*            deletes history on the next maintenance cycle
    #     path                   silently repoints at a different (empty) database
    #     backup.*               keep:1 / enabled:false removes the rotation
    #     rollups.enabled        stops the LONG memory growing while raw still expires
    #     maintenance_interval_s 0 stops rollups and retention together
    #     snapshot_interval_s    0 stops snapshotting SESSION_STATE.md, the one file in
    #                            this program that genuinely cannot be regenerated
    #     engine / schema_file   inert anyway -- the DAL is built BEFORE overrides apply
    # Storage is where the record lives. Every knob here is a durability knob, and all of
    # them stay editable in config.yaml, which is a deliberate act with a file to review.
    "site", "ai", "analysis", "weather", "alarms", "api",
    "appearance", "logging",
})
# Refused even inside a settable domain: secrets belong in the secrets store, not
# the settings table; auth credentials are never plain-settable.
# Deny the WHOLE api.auth subtree (never let the UI disable/alter its own auth gate)
# and the bind host/port (never let it widen network exposure), plus the LLM key path.
# storage.retention.* and storage.path are DENIED for the same reason bacnet.* is: they
# are irreversible. A retention window is the only setting in the program whose new value
# DELETES data the moment the next maintenance cycle runs -- no confirmation, no undo --
# and storage.path silently repoints the program at a different (empty) database while
# the old one stops being written. Neither belongs behind a dropdown; both stay editable
# in config.yaml, which is a deliberate act with a file to review.
# ⚠️ F-05 denied `storage.retention.` because a typo there deletes the record. The SAME
# argument reaches further than that fix did, and the 2026-09-22 audit (L-10) found the rest
# still settable from the Settings page:
#   storage.backup.keep          -> 1 leaves a single copy; 0 disables rotation
#   storage.backup.enabled       -> false stops backups, silently
#   storage.backup.min_free_bytes-> removes the free-space floor
#   storage.rollups.enabled      -> false stops the LONG memory growing while raw still
#                                   expires at 730 days: delayed, irreversible data loss
#   storage.maintenance_interval_s -> 0 stops rollups and retention together
# None of these is a thing to nudge from a web form; all of them are a deliberate act with
# a file to review. `storage.backup.` subsumes the old `storage.backup.dir` entry.
# The access keys too (2026-11-06 review, M13): api.remote_viewer_only=false from the form
# opened every page to the whole network at the next restart, with no login, and
# local_hosts / allowed_hosts widen who counts as this computer or which names it answers.
_DENY_KEYS = ("bacnet.", "api.auth.", "api.host", "api.port", "ai.providers.claude.api_key",
              "api.remote_viewer_only", "api.local_hosts", "api.allowed_hosts",
              "storage.retention.", "storage.path", "storage.backup.",
              "storage.rollups.", "storage.maintenance_interval_s")


def _names_a_file(key: str) -> bool:
    """A key whose value is a file or folder path. Not settable from a web form: pointed at
    config/secrets.env, ai.prompts.daily made the whole secrets file the AI summary's
    prompt, sent to the provider (2026-10-27 review)."""
    leaf = key.rsplit(".", 1)[-1]
    return (key.startswith("ai.prompts.") or leaf in ("path", "dir", "file")
            or leaf.endswith(("_path", "_dir", "_file")))


def is_settable(key: str) -> bool:
    """True if a dotted config key may be overridden via the Settings tab / DB."""
    if key.split(".", 1)[0] not in SETTABLE_DOMAINS or _names_a_file(key):
        return False
    return not any(key == d or key.startswith(d) for d in _DENY_KEYS)


def _leaf(data: Any, key: str) -> "tuple[bool, Any]":
    node = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def setting_problem(config: Any, key: str, value: Any) -> Optional[str]:
    """Why `value` cannot replace the current `key`, or None when it can. The value must be of
    the kind it replaces: a number for a number (or null, which most intervals read as
    "off"), true/false for a switch, text for text, a list for a list. A string where a
    number was meant used to be stored and then stopped the collector at the next restart,
    reported as a BACnet failure (2026-10-27 review)."""
    import math
    data = config.as_dict() if hasattr(config, "as_dict") else (config or {})
    found, cur = _leaf(data, key)
    if not found or isinstance(cur, dict):
        return "not a setting"
    if isinstance(cur, bool):
        return None if isinstance(value, bool) else "must be true or false"
    if isinstance(cur, (int, float)):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value):
            return "must be a number"
        return None
    if isinstance(cur, str):
        return None if isinstance(value, str) else "must be text"
    if isinstance(cur, list):
        return None if isinstance(value, list) else "must be a list"
    if cur is None:
        return None if value is None or isinstance(value, (str, int, float, bool)) \
            else "must be a single value"
    return "not a setting"


def apply_overrides(config: Config, overrides: dict[str, Any]) -> list[str]:
    """Overlay DB settings (dotted keys) onto the loaded config IN PLACE. A key is applied
    ONLY if it is settable AND maps to an EXISTING scalar/list leaf in the loaded config —
    never a non-settable key (e.g. bacnet.*), never a brand-new key, and never clobbering a
    dict subtree or a scalar traversed as if it were a dict. This keeps the read-only posture
    unflippable and stops a malformed/hostile override from corrupting real config or injecting
    new keys. Returns the keys applied."""
    data = config.as_dict()
    applied: list[str] = []
    for key, value in overrides.items():
        if not is_settable(key):
            continue
        # a value stored before values were checked (2026-10-27) is skipped, not applied
        if setting_problem(config, key, value):
            continue
        parts = key.split(".")
        node: Any = data
        ok = True
        for part in parts[:-1]:
            child = node.get(part) if isinstance(node, dict) else None
            if not isinstance(child, dict):
                ok = False              # path doesn't exist as a dict — refuse
                break
            node = child
        leaf = parts[-1]
        if not ok or not isinstance(node, dict) or leaf not in node or isinstance(node[leaf], dict):
            continue                    # require an existing scalar/list leaf; never create/clobber
        node[leaf] = value
        applied.append(key)
    return applied
