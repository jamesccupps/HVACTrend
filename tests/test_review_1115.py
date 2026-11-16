"""The 2026-11-15 full-read review of this repository. Each test failed on the code before its
fix."""

import asyncio
import time
from types import SimpleNamespace

import pytest

T0 = 1_790_000_000


# ── finding 2: one timed-out request no longer turns grouped reads off for good ──────────
def _reject_unrecognized():
    from bacpypes3.apdu import RejectPDU, RejectReason
    return RejectPDU(reason=RejectReason.unrecognizedService)


def _no_reply():
    from bacpypes3.apdu import AbortPDU, AbortReason
    return AbortPDU(reason=AbortReason.noResponse)


class _OneDevice:
    """read_property_multiple answers with whatever `rpm` returns next; plain reads answer."""
    def __init__(self, *rpm):
        self.rpm, self.calls = list(rpm), []

    async def read_property_multiple(self, address, params):
        self.calls.append("rpm")
        r = self.rpm.pop(0) if len(self.rpm) > 1 else self.rpm[0]
        if r == "ok":
            return [(oid, prop, None, 55.0 if prop == "present-value" else None)
                    for oid, props in zip(params[0::2], params[1::2]) for prop in props]
        if isinstance(r, Exception):
            raise r
        return r()

    async def read_property(self, address, oid, prop, property_array_index=None):
        self.calls.append(prop)
        return 56.0 if prop == "present-value" else None


def test_discovery_marks_a_device_as_refusing_grouped_reads_only_when_it_says_so():
    pytest.importorskip("bacpypes3")
    from hvactrend.bacnet.discovery import DiscoveredDevice, _read_props

    def scan(*answers):
        dev = DiscoveredDevice(device_instance=1, address="192.0.2.10")
        app = _OneDevice(*answers)
        for _ in answers:
            asyncio.run(_read_props(app, dev.address, "analog-value,1", ["object-name"],
                                    use_rpm=True, device=dev))
        return dev.rpm_supported

    assert scan(_no_reply) is None                 # a timeout says nothing about RPM
    assert scan(TimeoutError("raised")) is None    # nor does a raised one
    assert scan("ok", _no_reply) is True           # it was True and stays True
    assert scan(_no_reply, "ok") is True           # was: False, set by the first timeout
    assert scan(_reject_unrecognized) is False     # "I don't do that": marked
    assert scan(_reject_unrecognized, "ok") is True   # and an answer after it proves it does


def _collector(monkeypatch, clock, app, **poll):
    from hvactrend.collector import service
    monkeypatch.setattr(service, "time", SimpleNamespace(
        monotonic=lambda: clock[0], time=lambda: T0 + clock[0], localtime=time.localtime,
        strftime=time.strftime))
    return service.Collector(app, None, {"bacnet": {"poll": {
        "device_budget_s": 45, "rpm_batch": 10, "interval_s": 60, **poll}}})


def _pts(n, rpm):
    return [{"point_id": i, "object_type": "analog-value", "object_instance": i,
             "rpm_supported": rpm} for i in range(1, n + 1)]


def test_a_device_marked_as_refusing_grouped_reads_is_asked_again(monkeypatch):
    pytest.importorskip("bacpypes3")
    clock = [0.0]
    # it does answer them (the mark came from a timeout): read grouped from the first cycle
    app = _OneDevice("ok")
    col = _collector(monkeypatch, clock, app)
    _s, health = asyncio.run(col._poll_device("192.0.2.10", 8, _pts(25, rpm=0), T0))
    assert app.calls == ["rpm"] * 3 and health.method == "rpm" and health.reads_ok == 25
    clock[0] += 60
    app.calls.clear()
    asyncio.run(col._poll_device("192.0.2.10", 8, _pts(25, rpm=0), T0 + 60))
    assert app.calls == ["rpm"] * 3                # and for the rest of the run

    # it really refuses them: one grouped request, then one at a time, re-asked hourly
    clock[0] = 0.0
    app = _OneDevice(_reject_unrecognized)
    col = _collector(monkeypatch, clock, app)
    _s, health = asyncio.run(col._poll_device("192.0.2.11", 9, _pts(25, rpm=0), T0))
    assert app.calls.count("rpm") == 1 and health.method == "single" and health.reads_ok == 25
    for minutes in (1, 30, 59):
        clock[0] = minutes * 60.0
        app.calls.clear()
        asyncio.run(col._poll_device("192.0.2.11", 9, _pts(25, rpm=0), T0))
        assert "rpm" not in app.calls
    clock[0] = 3600.0
    app.calls.clear()
    asyncio.run(col._poll_device("192.0.2.11", 9, _pts(25, rpm=0), T0))
    assert app.calls.count("rpm") == 1
