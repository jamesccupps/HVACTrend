"""A mock BACnet/IP controller for local, hardware-free testing.

This is the "build the test environment first" discipline — a stand-in controller
so the discovery/read path can be exercised on loopback without touching the real
VLAN-5 plant. It is a TEST/DEV artifact, not part of the shipped read-only package.

Must be built inside a running asyncio loop (bacpypes3 objects schedule an async
post-init). `scripts/run_mock.py` runs one standalone for manual LAN testing.
"""

from __future__ import annotations

from argparse import Namespace
from typing import Any


def _args(instance: int, name: str, address: str, vendor: int = 999) -> Namespace:
    return Namespace(vendoridentifier=vendor, instance=instance, name=name,
                     address=address, foreign=None, network=None, bbmd=None, ttl=None)


def build_mock_controller(instance: int = 999001, name: str = "MockPXC",
                          address: str = "127.0.0.1:47809", vendor: int = 999) -> Any:
    """A mock controller with a typical AHU object set. Call inside a running loop."""
    from bacpypes3.app import Application
    from bacpypes3.local.analog import AnalogInputObject, AnalogValueObject
    from bacpypes3.local.binary import BinaryValueObject
    from bacpypes3.local.multistate import MultiStateValueObject

    app = Application.from_args(_args(instance, name, address, vendor))
    app.add_object(AnalogInputObject(
        objectIdentifier=("analog-input", 1), objectName="AHU1-DAT",
        presentValue=55.3, units="degreesFahrenheit",
        description="AHU-1 Discharge Air Temp"))
    app.add_object(AnalogInputObject(
        objectIdentifier=("analog-input", 2), objectName="AHU1-RAT",
        presentValue=72.1, units="degreesFahrenheit",
        description="AHU-1 Return Air Temp"))
    app.add_object(AnalogValueObject(
        objectIdentifier=("analog-value", 1), objectName="AHU1-DAT-SP",
        presentValue=55.0, units="degreesFahrenheit",
        description="AHU-1 Discharge Air Temp Setpoint"))
    app.add_object(BinaryValueObject(
        objectIdentifier=("binary-value", 1), objectName="AHU1-FAN",
        presentValue="active", description="AHU-1 Supply Fan Status"))
    app.add_object(MultiStateValueObject(
        objectIdentifier=("multi-state-value", 1), objectName="AHU1-MODE",
        presentValue=2, numberOfStates=3, description="AHU-1 Occupancy Mode"))
    return app


async def serve_forever(instance: int = 999001, name: str = "MockPXC",
                        address: str = "127.0.0.1:47809") -> None:  # pragma: no cover
    import asyncio
    app = build_mock_controller(instance, name, address)
    print(f"mock controller '{name}' (instance {instance}) at {address} — Ctrl+C to stop")
    try:
        await asyncio.get_event_loop().create_future()  # run until cancelled
    finally:
        app.close()
