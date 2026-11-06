"""BACnet discovery: Who-Is sweep + object-inventory enumeration.

Read-only end to end. Produces the browsable object inventory the commissioning
UI needs (name / type / units / live value / reliability), and the cached object
dictionary per device.

Site-safe by design:
  * inter-read delays between object reads,
  * per-device RPM with fallback to individual ReadProperty,
  * object-list read falls back to indexed element reads when a controller can't
    return the whole list in one (segmentation-limited) response,
  * every read is tolerant — one bad object never aborts the enumeration.

Nothing here writes. It uses the shared ReadOnlyBACnetApplication, whose surface
has no write method.

bacpypes3 value shapes (verified live, 0.0.106):
  present-value  -> Real(float) | BinaryPV | Enumerated | CharacterString
  units          -> EngineeringUnits (int(x)=enum value, str(x)='degrees-fahrenheit')
  object-list    -> list[ObjectIdentifier]  (str -> 'analog-value,1')
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..models import LOGGABLE_OBJECT_TYPES, Device, Point, PointSource
from .application import ReadOnlyBACnetApplication

logger = logging.getLogger(__name__)

# Properties pulled per object for the inventory/preview (all read-only).
_PREVIEW_PROPS = [
    "object-name", "description", "present-value", "units",
    "status-flags", "reliability", "out-of-service",
]

# Object types that carry a loggable present-value (shared with the DAL/browser).
_LOGGABLE_TYPES = LOGGABLE_OBJECT_TYPES


@dataclass(slots=True)
class DiscoveredDevice:
    device_instance: int
    address: str                       # bacpypes3 address string (IP or 'net:mac')
    vendor_id: Optional[int] = None
    vendor_name: Optional[str] = None
    model_name: Optional[str] = None
    firmware: Optional[str] = None
    object_name: Optional[str] = None
    segmentation: Optional[int] = None     # BACnet Segmentation enum value (0=segmented-both, 3=no-seg)
    max_apdu: Optional[int] = None
    network_number: Optional[int] = None   # set for routed (remote) devices
    rpm_supported: Optional[bool] = None   # learned during enumeration


@dataclass(slots=True)
class DiscoveredObject:
    object_type: str                   # 'analog-value'
    object_instance: int
    name: Optional[str] = None
    description: Optional[str] = None
    units: Optional[int] = None        # BACnet EngineeringUnits enum value
    units_text: Optional[str] = None   # 'degrees-fahrenheit'
    value_num: Optional[float] = None
    value_text: Optional[str] = None
    status_flags: Optional[str] = None
    reliability: Optional[str] = None
    out_of_service: Optional[bool] = None
    loggable: bool = False
    error: Optional[str] = None        # set if the object could not be read


# ── Who-Is sweep ─────────────────────────────────────────────────────────────
async def who_is_sweep(app: ReadOnlyBACnetApplication, *,
                       low_limit: Optional[int] = None,
                       high_limit: Optional[int] = None,
                       address: Any = None,
                       timeout: float = 3.0) -> list[DiscoveredDevice]:
    """Broadcast (address=None) or directed Who-Is. Returns devices, deduped by instance.

    Remote (routed) devices come back with their network number on the I-Am source
    — captured so the Trane UCs behind SC1/SC2 are addressable by instance.
    """
    iams = await app.who_is(low_limit, high_limit, address, timeout)
    devices: dict[int, DiscoveredDevice] = {}
    for iam in iams:
        try:
            _otype, inst = _split_oid(iam.iAmDeviceIdentifier)
            src = iam.pduSource
            devices[inst] = DiscoveredDevice(
                device_instance=inst,
                address=str(src),
                vendor_id=_maybe_int(getattr(iam, "vendorID", None)),
                segmentation=_maybe_int(getattr(iam, "segmentationSupported", None)),
                max_apdu=_maybe_int(getattr(iam, "maxAPDULengthAccepted", None)),
                network_number=getattr(src, "addrNet", None),
            )
        except Exception as exc:  # a malformed I-Am shouldn't kill the sweep
            logger.warning("skipping malformed I-Am: %s", exc)
    logger.info("Who-Is sweep found %d device(s)", len(devices))
    return list(devices.values())


# ── Object enumeration ───────────────────────────────────────────────────────
async def enumerate_device(app: ReadOnlyBACnetApplication, device: DiscoveredDevice,
                           *, use_rpm: bool = True,
                           inter_read_delay: float = 0.05) -> list[DiscoveredObject]:
    """Read the device's object-list, then name/units/value/status per object.

    Also enriches `device` in place with vendor/model/firmware/object-name and
    learns whether RPM works (device.rpm_supported).
    """
    dev_oid = f"device,{device.device_instance}"

    # Enrich the device record (best-effort; failures are non-fatal).
    await _enrich_device(app, device, dev_oid)

    oids = await _read_object_list(app, device.address, dev_oid)
    logger.info("device %s: %d objects", device.device_instance, len(oids))

    results: list[DiscoveredObject] = []
    for oid in oids:
        try:
            otype, oinst = _split_oid(oid)
        except (ValueError, TypeError):
            logger.debug("skipping unparseable object id %r on %s", oid, device.address)
            continue
        obj = DiscoveredObject(object_type=otype, object_instance=oinst,
                               loggable=otype in _LOGGABLE_TYPES)
        try:
            props = await _read_props(app, device.address, oid, _PREVIEW_PROPS,
                                      use_rpm=use_rpm, device=device)
            _fill_object(obj, props)
        except Exception as exc:
            obj.error = str(exc)
            logger.debug("read failed for %s on %s: %s", oid, device.address, exc)
        results.append(obj)
        if inter_read_delay:
            await asyncio.sleep(inter_read_delay)   # site-safe pacing
    return results


async def enumerate_device_fast(app: ReadOnlyBACnetApplication, device: DiscoveredDevice,
                                *, props: Optional[list[str]] = None,
                                batch_size: int = 10,
                                inter_read_delay: float = 0.02,
                                stats: Optional[dict] = None,
                                on_progress: Optional[Any] = None) -> list[DiscoveredObject]:
    """Enumerate a LARGE controller via batched ReadPropertyMultiple.

    For bulk inventory caching of big Trane SCs (thousands of objects) where the
    one-object-per-request `enumerate_device` is far too slow. Reads a compact
    prop set (name/units/description by default — NOT present-value; live values
    are read later per §9.1) for many objects per request, cutting a 5,500-object
    SC from thousands of requests to a few hundred. Any batch the device rejects
    (APDU size / no-RPM) falls back to per-object reads, so nothing is lost.

    `stats` (a dict, filled in) says whether the object list was read in full -- see
    _read_object_list. `on_progress(done, total)` is called after every batch.
    """
    props = list(props) if props else ["object-name", "units", "description"]
    dev_oid = f"device,{device.device_instance}"
    await _enrich_device(app, device, dev_oid)
    oids = await _read_object_list(app, device.address, dev_oid, stats=stats)
    if on_progress:
        on_progress(0, len(oids))

    results: list[DiscoveredObject] = []
    for start in range(0, len(oids), batch_size):
        batch = oids[start:start + batch_size]
        params: list[Any] = []
        for oid in batch:
            params.append(oid)
            params.append(list(props))
        try:
            rows = await app.read_property_multiple(device.address, params)
        except Exception:  # noqa: BLE001 -- a reply that would not decode: per object
            rows = None

        if rows is None or _is_error(rows):
            # device rejected the whole batch (APDU limit / no RPM), or sent something that
            # was not an RPM-ACK (bacpypes3 returns None) — per-object
            for oid in batch:
                per = await _read_props(app, device.address, oid, props,
                                        use_rpm=True, device=device)
                obj = _object_from_props(oid, per)
                if obj is not None:
                    results.append(obj)
        else:
            grouped: dict[str, dict[str, Any]] = {}
            for objid, propid, _idx, value in rows:
                d = grouped.setdefault(str(objid), {})
                if not _is_error(value):
                    d[str(propid)] = value
            for oid in batch:
                obj = _object_from_props(oid, grouped.get(str(oid), {}))
                if obj is not None:
                    results.append(obj)

        if on_progress:
            on_progress(min(start + batch_size, len(oids)), len(oids))
        if inter_read_delay:
            await asyncio.sleep(inter_read_delay)   # site-safe pacing between batches
    return results


async def identify_devices(app: ReadOnlyBACnetApplication,
                           devices: list[DiscoveredDevice], *,
                           inter_read_delay: float = 0.05) -> list[DiscoveredDevice]:
    """Read device-object identity (name/vendor/model/firmware) for each device,
    WITHOUT enumerating its objects. Light roster pass for the commissioning view
 — lets an operator tell AHU-1 from a VAV box before deep enumeration.
    """
    for dd in devices:
        await _enrich_device(app, dd, f"device,{dd.device_instance}")
        if inter_read_delay:
            await asyncio.sleep(inter_read_delay)     # site-safe pacing
    return devices


async def _enrich_device(app: ReadOnlyBACnetApplication, device: DiscoveredDevice,
                         dev_oid: str) -> None:
    props = ["object-name", "vendor-name", "model-name",
             "application-software-version", "vendor-identifier"]
    try:
        got = await _read_props(app, device.address, dev_oid, props,
                                use_rpm=True, device=device)
    except Exception as exc:
        logger.debug("device enrich failed for %s: %s", dev_oid, exc)
        return
    device.object_name = _maybe_str(got.get("object-name")) or device.object_name
    device.vendor_name = _maybe_str(got.get("vendor-name")) or device.vendor_name
    device.model_name = _maybe_str(got.get("model-name")) or device.model_name
    device.firmware = _maybe_str(got.get("application-software-version")) or device.firmware
    if device.vendor_id is None:
        device.vendor_id = _maybe_int(got.get("vendor-identifier"))


async def _read_object_list(app: ReadOnlyBACnetApplication, address: Any,
                            dev_oid: str, *, stats: Optional[dict] = None) -> list[Any]:
    """Read object-list, falling back to indexed element reads for big controllers.

    `stats`, when given, is filled with `declared` (the device's own count, None when it
    would not say), `listed` (how many were read) and `complete` -- True only when the
    count was readable and every element came back. A rescan reports points as gone from
    a controller ONLY on a complete list: a short read must never look like deletions.

    Some controllers — especially a device behind an MS/TP router (small APDU) —
    return a *truncated but non-error* whole-array object-list: the tail of the list
    (often the entire block of output objects) silently missing. Trusting that short
    list is how a routed JCI unit's dampers/valves/stage outputs never get discovered.
    So we read the declared element count (array index 0) and, if the whole-array read
    came back short, re-read the list element-by-index (each a small, segment-safe
    response). Index reads are also the path for big controllers whose full array
    can't fit one response at all.
    """
    st = stats if stats is not None else {}
    st.update(declared=None, listed=0, complete=False)
    # Declared length first (single-element read; cheap). Lets us detect a truncated
    # whole-array response instead of trusting a short-but-valid-looking list.
    declared: Optional[int] = None
    try:
        cnt = await app.read_property(address, dev_oid, "object-list",
                                      property_array_index=0)
        if cnt is not None and not _is_error(cnt):
            declared = int(cnt)
    except Exception as exc:
        logger.debug("object-list count read failed on %s: %s", dev_oid, exc)
    st["declared"] = declared

    # Whole-array read (fast path for well-behaved devices).
    try:
        objlist = await app.read_property(address, dev_oid, "object-list")
    except Exception as exc:
        objlist = None
        logger.info("bulk object-list read raised on %s (%s); trying indexed reads",
                    dev_oid, exc)
    # A big controller that can't fit object-list in one (segment-limited) response
    # RETURNS an abort/reject object here (bacpypes3 doesn't raise). Detect that, AND
    # detect a truncated-but-valid list (fewer than declared), then fall through.
    if objlist is not None and not _is_error(objlist):
        got = list(objlist)
        if declared is None or len(got) >= declared:
            # complete only when the device said how many: an uncounted list may be short
            st.update(listed=len(got), complete=declared is not None)
            return got
        logger.warning("object-list on %s returned %d of %d declared objects; "
                       "re-reading by index (truncated/segment-limited response)",
                       dev_oid, len(got), declared)

    logger.info("bulk object-list unavailable/short on %s; reading elements by index",
                dev_oid)
    oids: list[Any] = []
    missed: list[int] = []
    try:
        count = declared
        if count is None:
            cnt = await app.read_property(address, dev_oid, "object-list",
                                          property_array_index=0)
            if cnt is None or _is_error(cnt):
                logger.warning("object-list count unreadable on %s", dev_oid)
                return oids
            count = int(cnt)
        for i in range(1, count + 1):
            # ⚠️ The try is PER ELEMENT, not around the loop. A read that RAISES -- a
            # timeout surfacing as an exception rather than as a returned error object --
            # used to escape to the outer handler, abandon every remaining element, and
            # return the partial list AS IF IT WERE COMPLETE. That is how a 2,023-object
            # panel is cached as 22 objects with one WARNING and nothing downstream able to
            # tell a genuinely small device from a truncated read. 2026-09-22 audit, H-3.
            try:
                oid = await app.read_property(address, dev_oid, "object-list",
                                              property_array_index=i)
            except Exception as exc:
                logger.debug("object-list element %d raised on %s: %s", i, dev_oid, exc)
                oid = None
            if oid is None or _is_error(oid):
                # a transient per-element reject must not kill the whole device
                missed.append(i)
                continue
            oids.append(oid)
        st.update(declared=count, listed=len(oids), complete=not missed)
        if missed:
            # Loud, WITH NUMBERS, at the one place that knows both what the device
            # advertises and what was actually read. Before this, a partial inventory was
            # indistinguishable from a small device: the only symptom in the field was
            # someone noticing CT-1 and CT-2 had different point counts while building a
            # chart, and that was two months after the fact.
            logger.error("object-list on %s INCOMPLETE: read %d of %d declared objects; "
                         "%d element(s) unreadable (first: %s). Re-run discovery for this "
                         "device.", dev_oid, len(oids), count, len(missed), missed[:10])
    except Exception as exc:
        st.update(listed=len(oids), complete=False)
        logger.error("indexed object-list read failed on %s: %s (kept %d of %s objects)",
                     dev_oid, exc, len(oids), count)
    return oids


async def _read_props(app: ReadOnlyBACnetApplication, address: Any, oid: Any,
                      props: list[str], *, use_rpm: bool,
                      device: Optional[DiscoveredDevice] = None) -> dict[str, Any]:
    """Return {property-name: value}. RPM first, per-property fallback on failure."""
    if use_rpm:
        rows: Any = None
        try:
            rows = await app.read_property_multiple(address, [oid, list(props)])
        except Exception as exc:
            logger.debug("RPM raised on %s (%s); falling back to single reads", oid, exc)
        # A whole-request reject/abort comes back as a RETURNED error object.
        if rows is not None and not _is_error(rows):
            if device is not None and device.rpm_supported is None:
                device.rpm_supported = True
            out: dict[str, Any] = {}
            try:
                for _objid, propid, _idx, value in rows:
                    if not _is_error(value):        # skip per-property inline errors
                        out[str(propid)] = value
                return out
            except (TypeError, ValueError) as exc:
                logger.debug("RPM result unpack failed on %s (%s); single reads", oid, exc)
        if device is not None:
            device.rpm_supported = False

    # Individual ReadProperty. read_property RETURNS an error object on failure, so
    # check _is_error on the result — don't store a phantom 'None'-valued point.
    out = {}
    for prop in props:
        try:
            value = await app.read_property(address, oid, prop)
        except Exception:
            continue
        if not _is_error(value):
            out[prop] = value
    return out


def _object_from_props(oid: Any, props: dict[str, Any]) -> Optional[DiscoveredObject]:
    """Build a DiscoveredObject from an object id + its read properties; None if
    the object id can't be parsed (never abort a batch on one bad id)."""
    try:
        otype, oinst = _split_oid(oid)
    except (ValueError, TypeError):
        logger.debug("skipping unparseable object id %r", oid)
        return None
    obj = DiscoveredObject(object_type=otype, object_instance=oinst,
                           loggable=otype in _LOGGABLE_TYPES)
    _fill_object(obj, props)
    return obj


# Trane flattens each unit's points into the SC with a '|dac-N' suffix (e.g.
# 'Discharge Air Temperature|dac-1', 'HW Valve|dac-2_AOP~_03'). This pulls the N.
_DAC_RE = re.compile(r"\|dac-(\d+)", re.IGNORECASE)


def dac_number(object_name: Optional[str]) -> Optional[int]:
    """Return the Trane |dac-N group number embedded in an object name, or None."""
    if not object_name:
        return None
    m = _DAC_RE.search(object_name)
    return int(m.group(1)) if m else None


def _fill_object(obj: DiscoveredObject, props: dict[str, Any]) -> None:
    obj.name = _maybe_str(props.get("object-name"))
    obj.description = _maybe_str(props.get("description"))
    obj.units, obj.units_text = _units(props.get("units"))
    obj.value_num, obj.value_text = _present_value(props.get("present-value"))
    obj.status_flags = _maybe_str(props.get("status-flags"))
    obj.reliability = _maybe_str(props.get("reliability"))
    obj.out_of_service = _maybe_bool(props.get("out-of-service"))


# ── model mapping (for caching to the DB) ────────────────────────────────────
def to_model_device(dd: DiscoveredDevice, *, site_id: Optional[int],
                    created_utc: int) -> Device:
    return Device(
        device_instance=dd.device_instance, site_id=site_id,
        name=dd.object_name, address=dd.address,
        network_number=dd.network_number,
        vendor_id=dd.vendor_id, vendor_name=dd.vendor_name,
        model_name=dd.model_name, firmware=dd.firmware,
        segmentation=dd.segmentation,
        rpm_supported=dd.rpm_supported, last_seen_utc=created_utc,
    )


def to_model_point(do: DiscoveredObject, *, device_id: int,
                   system_id: Optional[int] = None) -> Point:
    """Map a discovered object to a cached inventory Point (enabled=False by default).

    Discovery records the full inventory; the operator opts a point INTO logging
    later. `system_id` groups the point under its equipment (AHU) when
    known. trend-log objects are flagged for ReadRange harvest.
    """
    return Point(
        source=PointSource.BACNET, device_id=device_id, system_id=system_id,
        object_type=do.object_type, object_instance=do.object_instance,
        name=do.name, description=do.description,
        units=do.units, units_text=do.units_text,
        trendlog_harvest=(do.object_type == "trend-log"),
        enabled=False,
    )


# ── value normalization (bacpypes3 typed values -> our schema types) ─────────
def _present_value(v: Any) -> tuple[Optional[float], Optional[str]]:
    """Return (value_num, value_text). Numeric -> num; enum/binary -> text + numeric index.

    bacpypes3 Real is a float subclass; BinaryPV/Enumerated are int subclasses whose
    str() is a symbolic label ('active'), not the number. Detect the label case by
    comparing str(v) to the plain integer string.
    """
    if v is None or _is_error(v):
        return None, None
    if isinstance(v, bool):
        return float(v), ("active" if v else "inactive")
    if isinstance(v, float):                       # Real
        f = float(v)
        # IEEE 754: some controllers report a failed sensor as +/-Inf or NaN. Stored as a
        # number, one Inf poisons the 5-minute and hourly maxima and averages (the hourly are
        # kept for ever) and fails any JSON response that carries it (2026-11-06 review, M9).
        # No number, and the text says what came: the collector marks it a fault.
        return (f, None) if math.isfinite(f) else (None, repr(f))
    if isinstance(v, int):                          # Unsigned/Integer OR BinaryPV/Enumerated
        label = str(v)
        if label != str(int(v)):                    # symbolic enum label present
            return float(int(v)), label
        return float(v), None                       # plain number (e.g. multistate index)
    return None, str(v)                             # CharacterString etc.


def _units(v: Any) -> tuple[Optional[int], Optional[str]]:
    if v is None or _is_error(v):
        return None, None
    try:
        return int(v), str(v)
    except (TypeError, ValueError):
        return None, _maybe_str(v)


def _split_oid(oid: Any) -> tuple[str, int]:
    """('analog-value', 1) from an ObjectIdentifier or 'analog-value,1' string."""
    s = str(oid)
    type_str, _, inst_str = s.rpartition(",")
    return type_str, int(inst_str)


def _is_error(v: Any) -> bool:
    """True for a bacpypes3 error/reject/abort result.

    IMPORTANT: bacpypes3 read_property / read_property_multiple RETURN an
    ErrorRejectAbortNack (AbortPDU/RejectPDU/ErrorPDU) on failure rather than
    raising it, and those subclass BaseException (NOT Exception) — so neither
    `except Exception` nor `isinstance(v, Exception)` catches them. Inline RPM
    property errors are ErrorType with an errorClass. Catch all of these. A plain
    None is a legitimate 'no value', not an error.
    """
    if v is None:
        return False
    if isinstance(v, BaseException):        # ErrorRejectAbortNack + ordinary Exceptions
        return True
    return hasattr(v, "errorClass") or type(v).__name__ in (
        "ErrorType", "ErrorPDU", "AbortPDU", "RejectPDU")


def _no_response(v: Any) -> bool:
    """True for the abort bacpypes3 returns when a request got no reply at all, after its own
    retries: the device or its trunk is not there. A reject or an error is a reply, from a
    device that is (2026-11-06 review, M7)."""
    return (type(v).__name__ == "AbortPDU"
            and str(getattr(v, "apduAbortRejectReason", "")) in ("no-response", "noResponse"))


def _maybe_str(v: Any) -> Optional[str]:
    if v is None or _is_error(v):
        return None
    return str(v)


def _maybe_int(v: Any) -> Optional[int]:
    if v is None or _is_error(v):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _maybe_bool(v: Any) -> Optional[bool]:
    if v is None or _is_error(v):
        return None
    try:
        return bool(int(v))
    except (TypeError, ValueError):
        return bool(v)
