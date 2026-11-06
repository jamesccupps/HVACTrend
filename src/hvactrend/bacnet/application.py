"""The single shared bacpypes3 Application.

There is exactly ONE BACnet application for the whole process. The collector's
scheduled polling AND the API/browser's discovery/preview reads use the same
instance on the same asyncio event loop. Two stacks would try to bind the same
UDP port — never do that.

Ownership: the FastAPI lifespan (api/app.py) owns the loop, calls
`get_application()` once at startup, and `close_application()` at shutdown. The
collector receives the same object — it never creates its own.

Read-only: this wrapper exposes ONLY read/discovery methods. There is no
`write_property` here, by design. Every outbound request is additionally routed
through `readonly.guard_request` (defense-in-depth, plan §5).

The bacpypes3 API used here is empirically verified against bacpypes3 0.0.106
(live loopback round-trip). bacpypes3 is imported lazily so the rest of the
package (safety, config, models, storage) imports with the stdlib alone.
"""

from __future__ import annotations

import logging
import re
import socket
from argparse import Namespace
from typing import Any, Optional

from .readonly import guard_request

logger = logging.getLogger(__name__)

# Module-level singleton. Access ONLY through get_application().
_APPLICATION: "ReadOnlyBACnetApplication | None" = None


class BACnetConfigError(RuntimeError):
    """Raised when BACnet config is unfit to bind (e.g. unfilled placeholders)."""


class ReadOnlyBACnetApplication:
    """Read-only façade over a bacpypes3 Application.

    Construction is deferred to `create()` (async) so this module imports with
    the standard library alone; bacpypes3 is imported lazily there.
    """

    def __init__(self, app: Any, *, allow_writes: bool = False,
                 allow_cov: bool = False) -> None:
        # `app` is the underlying bacpypes3.app.Application.
        self._app = app
        self._allow_writes = allow_writes   # stays False in production
        self._allow_cov = allow_cov

    # ── lifecycle ────────────────────────────────────────────────────────────
    @classmethod
    async def create(cls, config: Any) -> "ReadOnlyBACnetApplication":
        """Build the underlying bacpypes3 Application from config.bacnet.

        Refuses to bind while required identity fields are still placeholders
        (guardrail #3: no invented addresses).
        """
        bacnet_cfg = _get(config, "bacnet", {}) or {}
        safety = _get(bacnet_cfg, "safety", {}) or {}
        allow_writes = bool(_get(safety, "allow_writes", False))
        allow_cov = bool(_get(safety, "allow_cov", False))

        local_address = _get(bacnet_cfg, "local_address")
        device_instance = _get(bacnet_cfg, "device_instance")
        object_name = _get(bacnet_cfg, "object_name", "HVACTrend-Logger")
        vendor_id = int(_get(bacnet_cfg, "vendor_identifier", 999))

        _refuse_placeholders(local_address, device_instance)
        _assert_bindable(str(local_address))   # fail loudly now, not silently later

        if allow_writes:
            logger.warning(
                "bacnet.safety.allow_writes is TRUE — property writes are ungated. "
                "This must not be set in production."
            )

        # bacpypes3 wants a device instance number and an address string like
        # '192.0.2.5/24' (real NIC) or '127.0.0.1:47808' (loopback test).
        args = Namespace(
            vendoridentifier=vendor_id,
            instance=int(device_instance),
            name=str(object_name),
            address=str(local_address),
            foreign=None, network=None, bbmd=None, ttl=None,
        )
        from bacpypes3.app import Application  # lazy import
        app = Application.from_args(args)
        logger.info("ReadOnlyBACnetApplication bound: instance=%s addr=%s",
                    device_instance, local_address)
        return cls(app, allow_writes=allow_writes, allow_cov=allow_cov)

    @classmethod
    def wrap(cls, app: Any, *, allow_writes: bool = False,
             allow_cov: bool = False) -> "ReadOnlyBACnetApplication":
        """Wrap an already-built bacpypes3 Application (used by tests/mocks)."""
        return cls(app, allow_writes=allow_writes, allow_cov=allow_cov)

    async def close(self) -> None:
        """Tear down the underlying stack."""
        if self._app is not None and hasattr(self._app, "close"):
            try:
                self._app.close()
            except Exception:  # pragma: no cover - teardown best-effort
                logger.debug("BACnet app close raised during teardown", exc_info=True)
        self._app = None

    # ── read / discovery surface (the ONLY things this app does) ─────────────
    async def who_is(self, low_limit: Optional[int] = None,
                     high_limit: Optional[int] = None,
                     address: Any = None, timeout: float = 3.0) -> list[Any]:
        """Who-Is sweep. address=None broadcasts; a str/Address directs it.

        Returns a list of bacpypes3 IAmRequest objects.
        """
        guard_request("WhoIsRequest",
                      allow_writes=self._allow_writes, allow_cov=self._allow_cov)
        return await self._app.who_is(low_limit, high_limit, _addr(address), timeout)

    async def read_property(self, address: Any, object_identifier: Any,
                            property_identifier: Any,
                            property_array_index: Optional[int] = None) -> Any:
        """ReadProperty a single value. objid/prop may be strings ('analog-value,1').

        A device error/reject/abort is RETURNED (not raised) for a uniform caller
        contract — see _normalize_errors.
        """
        guard_request("ReadPropertyRequest",
                      allow_writes=self._allow_writes, allow_cov=self._allow_cov)
        err = _bacnet_error_base()
        try:
            return await self._app.read_property(
                _addr(address), object_identifier, property_identifier,
                property_array_index)
        except err as exc:                       # ErrorRejectAbortNack (a BaseException)
            return exc

    async def read_property_multiple(self, address: Any,
                                     parameter_list: Any) -> Any:
        """ReadPropertyMultiple. parameter_list is FLAT: [objid, [props], objid2, [props2]].

        Returns a list of (ObjectIdentifier, PropertyIdentifier, index|None, value),
        or a returned error object if the whole request was rejected/aborted.
        """
        guard_request("ReadPropertyMultipleRequest",
                      allow_writes=self._allow_writes, allow_cov=self._allow_cov)
        err = _bacnet_error_base()
        try:
            return await self._app.read_property_multiple(_addr(address), parameter_list)
        except err as exc:
            return exc

    async def read_range(self, address: Any, object_identifier: Any,
                         property_identifier: Any = "log-buffer",
                         array_index: Optional[int] = None,
                         range_params: Any = None) -> Any:
        """ReadRange over a Trend Log buffer for harvest/backfill. (Phase 7.)"""
        guard_request("ReadRangeRequest",
                      allow_writes=self._allow_writes, allow_cov=self._allow_cov)
        err = _bacnet_error_base()
        try:
            return await self._app.read_range(
                _addr(address), object_identifier, property_identifier,
                array_index, range_params)
        except err as exc:
            return exc

    # NB: there is deliberately NO write_property / write_property_multiple method.
    # Adding one is a guardrail violation. Reads only.
    #
    # There is also deliberately NO `raw`/underlying-Application accessor. The bare
    # bacpypes3 Application supports write_property / write_property_multiple / COV
    # subscribe and is NOT routed through guard_request, so exposing it would punch a
    # hole straight through the read-only guardrail. Everything the program needs is a
    # read/discovery method above; if a new read is required, add a guarded method here
    # — never hand out self._app.


_BACNET_ERROR_BASE: Any = None


def _bacnet_error_base() -> Any:
    """The bacpypes3 ErrorRejectAbortNack base class (lazy-imported, cached).

    bacpypes3 RAISES these (they subclass BaseException, not Exception) on a
    rejected/aborted/errored read — e.g. a controller that answers RPM with
    'unrecognized-service'. The read wrappers catch this base and RETURN the
    object so callers get a uniform 'errors are values' contract.
    """
    global _BACNET_ERROR_BASE
    if _BACNET_ERROR_BASE is None:
        from bacpypes3.apdu import ErrorRejectAbortNack
        _BACNET_ERROR_BASE = ErrorRejectAbortNack
    return _BACNET_ERROR_BASE


def _addr(address: Any) -> Any:
    """Coerce a string address to a bacpypes3 Address; pass through None/Address."""
    if address is None:
        return None
    if isinstance(address, str):
        from bacpypes3.pdu import Address  # lazy
        return Address(address)
    return address


def _assert_bindable(local_address: str) -> None:
    """Verify the IP in local_address is actually an address on THIS host.

    bacpypes3 binds its UDP endpoint in a background callback, so a bad address
    (e.g. a stale DHCP lease after the NIC's IP changed) would NOT raise from
    create() — it would leave a non-functional app and every read would hang.
    This pre-check turns that into an immediate, actionable error. Loopback and
    real NIC addresses both pass; a no-longer-present address fails.
    """
    ip = re.split(r"[/:]", local_address.strip(), maxsplit=1)[0]
    if not ip:
        return
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind((ip, 0))
        finally:
            s.close()
    except OSError as exc:
        try:
            host_ips = sorted({a[4][0] for a in socket.getaddrinfo(
                socket.gethostname(), None, socket.AF_INET)})
        except OSError:
            host_ips = []
        hint = f" This host's IPv4 addresses: {', '.join(host_ips)}." if host_ips else ""
        raise BACnetConfigError(
            f"cannot bind BACnet to {ip!r} ({exc}). It is not a current address on "
            f"this machine — check the NIC / DHCP lease and update bacnet.local_address "
            f"in config.yaml.{hint}"
        ) from exc
    # ...and the PORT. The address check above binds port 0, and bacpypes3 retries a refused
    # bind every second for ever without raising: with 47808 held by another BACnet tool
    # (YABE, a vendor workstation, a scanner) the collector "ran", /health said so, and every
    # read timed out (2026-11-06 review, M5).
    m = re.search(r":(\d+)$", local_address.strip())
    port = int(m.group(1)) if m else 47808
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind((ip, port))
        finally:
            s.close()
    except OSError as exc:
        raise BACnetConfigError(
            f"UDP port {port} on {ip} is in use ({exc}). Is another BACnet program running "
            f"on this computer (a workstation tool, a scanner), or a second copy of this one? "
            f"Close it, or give this program another port in bacnet.local_address."
        ) from exc


def _refuse_placeholders(local_address: Any, device_instance: Any) -> None:
    for label, val in (("bacnet.local_address", local_address),
                       ("bacnet.device_instance", device_instance)):
        if isinstance(val, str) and val.strip().startswith("<"):
            raise BACnetConfigError(
                f"{label} is still a placeholder ({val!r}): run scripts/configure.py, or set "
                f"it in config/config.yaml, before HVACTrend can join the BACnet network."
            )
    try:
        int(device_instance)
    except (TypeError, ValueError):
        raise BACnetConfigError(
            f"bacnet.device_instance must be an integer (got {device_instance!r})."
        )


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Attr-or-key accessor so config can be a Config/_Node, dataclass, or dict."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


async def get_application(config: Any) -> ReadOnlyBACnetApplication:
    """Return the process-wide singleton, creating it once. Idempotent.

    Binds the UDP port exactly once. Never construct ReadOnlyBACnetApplication
    directly in the running program — always come through here.
    """
    global _APPLICATION
    if _APPLICATION is None:
        _APPLICATION = await ReadOnlyBACnetApplication.create(config)
    return _APPLICATION


async def close_application() -> None:
    """Close and clear the singleton (called from the API lifespan shutdown)."""
    global _APPLICATION
    if _APPLICATION is not None:
        await _APPLICATION.close()
        _APPLICATION = None
