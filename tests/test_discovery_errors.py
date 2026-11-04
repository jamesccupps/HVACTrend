"""Regression tests for bacpypes3 error-object handling in discovery.

bacpypes3 read_property / read_property_multiple RETURN an ErrorRejectAbortNack
(AbortPDU/RejectPDU/ErrorPDU — subclasses of BaseException, NOT Exception) on
failure instead of raising. These tests reproduce the Phase 1 audit findings:
  1. a per-element object-list reject must not abort the whole device,
  2. a returned error must not be cached as a phantom 'None' value.

Hermetic — a scripted fake app returns real bacpypes3 error objects; no UDP, so
these run even where loopback binding is unavailable.
"""

import asyncio

import pytest

pytest.importorskip("bacpypes3", reason="bacpypes3 not installed")

from bacpypes3.apdu import AbortPDU, RejectPDU  # noqa: E402

from hvactrend.bacnet.discovery import (  # noqa: E402
    DiscoveredDevice,
    _is_error,
    _present_value,
    _read_object_list,
    dac_number,
    enumerate_device,
)


def test_dac_number_parsing():
    assert dac_number("Discharge Air Temperature|dac-1") == 1
    assert dac_number("HW Valve|dac-2_AOP~_03") == 2
    assert dac_number("Return Air Temp|dac-10_UIP~_05") == 10
    assert dac_number("SF~-A VFD Mod|DAC-7_AOP~_01") == 7        # case-insensitive
    assert dac_number("Facility Outdoor Air Temperature") is None
    assert dac_number(None) is None


def test_is_error_recognizes_returned_nacks():
    assert _is_error(AbortPDU()) is True
    assert _is_error(RejectPDU()) is True
    assert _is_error(None) is False          # a legitimate 'no value'
    assert _is_error(55.3) is False
    assert _is_error("active") is False


def test_present_value_no_phantom_on_abort():
    # Before the fix this returned (None, 'None') — a phantom reading.
    assert _present_value(AbortPDU()) == (None, None)
    assert _present_value(RejectPDU()) == (None, None)


class _FakeApp:
    """Scripts read_property / read_property_multiple responses for one device."""

    def __init__(self, elements, rpm):
        self._elements = elements   # {index: value_or_error} for object-list
        self._rpm = rpm             # {oid_str: rows_or_error}

    async def read_property(self, address, oid, prop, property_array_index=None):
        if prop == "object-list" and property_array_index is None:
            return AbortPDU()                     # force the indexed fallback
        if prop == "object-list" and property_array_index == 0:
            return len(self._elements)
        if prop == "object-list":
            return self._elements[property_array_index]
        return AbortPDU()                          # single-prop reads all abort

    async def read_property_multiple(self, address, parameter_list):
        return self._rpm.get(str(parameter_list[0]), AbortPDU())


def test_object_list_indexed_fallback_skips_bad_element():
    # bulk read aborts -> fallback; element 2 rejects and must be skipped, not fatal
    app = _FakeApp(elements={1: "analog-value,1", 2: RejectPDU(), 3: "binary-value,1"},
                   rpm={})
    oids = asyncio.run(_read_object_list(app, "addr", "device,1"))
    assert [str(o) for o in oids] == ["analog-value,1", "binary-value,1"]


def test_enumerate_tolerates_per_object_errors():
    # analog-value reads fine via RPM; binary-value's RPM aborts (falls to single
    # reads, which also abort). The whole device must still enumerate both, and the
    # unreadable object must NOT be cached with a phantom 'None' value.
    app = _FakeApp(
        elements={1: "analog-value,1", 2: "binary-value,1"},
        rpm={
            "device,1": [(("device", 1), "object-name", None, "MockDev")],
            "analog-value,1": [
                (("analog-value", 1), "object-name", None, "AV1"),
                (("analog-value", 1), "present-value", None, 55.0),
            ],
            "binary-value,1": AbortPDU(),
        },
    )
    dd = DiscoveredDevice(device_instance=1, address="addr")
    objs = asyncio.run(enumerate_device(app, dd, inter_read_delay=0))

    types = {o.object_type for o in objs}
    assert types == {"analog-value", "binary-value"}          # neither dropped
    av = next(o for o in objs if o.object_type == "analog-value")
    assert av.name == "AV1" and av.value_num == 55.0
    bv = next(o for o in objs if o.object_type == "binary-value")
    assert bv.value_num is None and bv.value_text is None       # no phantom 'None'
