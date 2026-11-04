"""Top-level safety surface.

Re-exports the BACnet read-only guard and adds config-level assertions so a
caller can fail fast at startup if the safety posture has been tampered with.
Import this from the collector/API startup path.
"""

from __future__ import annotations

from typing import Any

from .bacnet.readonly import (
    WriteBlockedError,
    classify_service,
    guard_service,
    guard_request,
)

__all__ = [
    "WriteBlockedError",
    "classify_service",
    "guard_service",
    "guard_request",
    "assert_read_only_default",
]


def _get(obj: Any, *path: str, default: Any = None) -> Any:
    cur = obj
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            cur = getattr(cur, key, None)
        if cur is None:
            return default
    return cur


def assert_read_only_default(config: Any) -> None:
    """Confirm the shipped safety posture is read-only.

    Does NOT forbid a knowing lab override of allow_writes (the operator can set
    it), but guarantees the *default* is safe and that the never-allowed services
    remain unconditionally blocked. Call at startup.
    """
    safety = _get(config, "bacnet", "safety", default={}) or {}
    allow_writes = bool(_get(safety, "allow_writes", default=False))

    # The unconditional block must hold regardless of any flag.
    for destructive in ("ReinitializeDeviceRequest",
                        "DeviceCommunicationControlRequest",
                        "AtomicWriteFileRequest"):
        try:
            guard_service(destructive, allow_writes=True, allow_cov=True)
        except WriteBlockedError:
            continue
        raise AssertionError(
            f"SAFETY REGRESSION: {destructive} was not blocked unconditionally."
        )

    if allow_writes:
        import logging
        logging.getLogger(__name__).warning(
            "Read-only override active (allow_writes=true). Confirm this is a lab, "
            "not production."
        )
