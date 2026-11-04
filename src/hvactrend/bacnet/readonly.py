"""Read-only enforcement for the BACnet stack.

Posture: **allowlist-first**. The only outbound services HVACTrend ever emits
are reads and discovery. This module is defense-in-depth: every request the
application sends is classified, and anything that is not an explicit read is
blocked. It exists so that a future edit that *tries* to add a write trips a
loud, deliberate gate instead of silently touching a production controller.

Three tiers, keyed on bacpypes3's real APDU request class names (verified
against bacpypes3 0.0.106):

  ALLOWED  — reads + discovery. Emitted freely.
  GATED    — property writes / COV. Blocked unless the matching config flag is
             explicitly true (default false). COV mutates a device's
             subscription table + adds load, so it is gated too, not "read".
  NEVER    — reinit, device-comm-control, file/flash writes, create/delete
             object, private-transfer. Blocked UNCONDITIONALLY, regardless of
             any flag. These are the "brick or disrupt a controller" services.

There is intentionally no code path in HVACTrend that constructs a GATED or
NEVER request. The guard is a tripwire, not a feature switch.
"""

from __future__ import annotations

from typing import Iterable

__all__ = [
    "WriteBlockedError",
    "ALLOWED_SERVICES",
    "GATED_WRITE_SERVICES",
    "GATED_COV_SERVICES",
    "NEVER_ALLOWED_SERVICES",
    "classify_service",
    "guard_service",
    "guard_request",
]


class WriteBlockedError(RuntimeError):
    """Raised when a non-read BACnet service is attempted. Read-only means read-only."""


# Reads + discovery — the ONLY services the application emits.
ALLOWED_SERVICES: frozenset[str] = frozenset({
    "ReadPropertyRequest",
    "ReadPropertyMultipleRequest",
    "ReadRangeRequest",          # Trend Log buffer harvest
    "AtomicReadFileRequest",     # read-only file read (e.g. some trend-log files)
    "WhoIsRequest",
    "WhoHasRequest",
    # Responses this app emits as its own device identity (harmless, expected):
    "IAmRequest",
    "IHaveRequest",
})

# Property writes — the actual control risk. Gated behind bacnet.safety.allow_writes.
GATED_WRITE_SERVICES: frozenset[str] = frozenset({
    "WritePropertyRequest",
    "WritePropertyMultipleRequest",
    "WriteGroupRequest",
})

# COV subscription mutates device subscription state + adds load. Gated behind
# bacnet.safety.allow_cov.
GATED_COV_SERVICES: frozenset[str] = frozenset({
    "SubscribeCOVRequest",
    "SubscribeCOVPropertyRequest",
})

# Blocked no matter what. No flag enables these.
NEVER_ALLOWED_SERVICES: frozenset[str] = frozenset({
    "ReinitializeDeviceRequest",          # reboot / factory reset
    "DeviceCommunicationControlRequest",  # can silence a device's comms — disrupts control
    "AtomicWriteFileRequest",             # file / flash writes
    "CreateObjectRequest",
    "DeleteObjectRequest",
    "ConfirmedPrivateTransferRequest",    # vendor proprietary — unknown effects
    "UnconfirmedPrivateTransferRequest",
})


def _service_name(request: object) -> str:
    """Best-effort service name for a bacpypes3 APDU (or a plain string)."""
    if isinstance(request, str):
        return request
    return type(request).__name__


def classify_service(service: object) -> str:
    """Return one of: 'allowed' | 'gated-write' | 'gated-cov' | 'never' | 'unknown'."""
    name = _service_name(service)
    if name in ALLOWED_SERVICES:
        return "allowed"
    if name in GATED_WRITE_SERVICES:
        return "gated-write"
    if name in GATED_COV_SERVICES:
        return "gated-cov"
    if name in NEVER_ALLOWED_SERVICES:
        return "never"
    return "unknown"


def guard_service(service: object, *, allow_writes: bool = False,
                  allow_cov: bool = False) -> None:
    """Raise WriteBlockedError unless `service` is permitted under the given flags.

    Unknown services fail closed (blocked) — a service HVACTrend doesn't
    recognize is not something it should be sending.
    """
    name = _service_name(service)
    kind = classify_service(name)

    if kind == "allowed":
        return
    if kind == "gated-write":
        if allow_writes:
            return
        raise WriteBlockedError(
            f"{name} blocked: BACnet writes are disabled "
            f"(set bacnet.safety.allow_writes to override — do NOT in production)."
        )
    if kind == "gated-cov":
        if allow_cov:
            return
        raise WriteBlockedError(
            f"{name} blocked: COV subscriptions are disabled "
            f"(set bacnet.safety.allow_cov to enable)."
        )
    if kind == "never":
        raise WriteBlockedError(
            f"{name} is NEVER allowed: destructive/disruptive service is blocked "
            f"unconditionally, no flag enables it."
        )
    # unknown -> fail closed
    raise WriteBlockedError(f"{name} blocked: unrecognized service (fail-closed, read-only).")


def guard_request(request: object, *, allow_writes: bool = False,
                  allow_cov: bool = False) -> object:
    """Guard an outgoing APDU and return it unchanged if permitted (chainable)."""
    guard_service(request, allow_writes=allow_writes, allow_cov=allow_cov)
    return request


def assert_no_write_services(services: Iterable[object]) -> None:
    """Helper for tests / audits: raise on the first non-read service."""
    for svc in services:
        guard_service(svc)
