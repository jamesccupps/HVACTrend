"""Phase 1 discovery integration test against a mock BACnet controller (loopback).

Exercises the real bacpypes3 stack end to end — Who-Is sweep, object-list
enumeration, value/units normalization, and caching the inventory to SQLite —
without any real hardware. Skips cleanly where bacpypes3 or loopback UDP binding
is unavailable (e.g. a locked-down CI runner).

NOTE: passing here is NOT the same as verifying against the real plant. Phase 1
is "done" only after a live Who-Is on 192.0.2.0/24 (guardrail #4).
"""

import asyncio
import socket

import pytest

pytest.importorskip("bacpypes3", reason="bacpypes3 not installed")

from hvactrend.bacnet.application import ReadOnlyBACnetApplication  # noqa: E402
from hvactrend.bacnet.discovery import (  # noqa: E402
    enumerate_device,
    to_model_device,
    to_model_point,
    who_is_sweep,
)
from hvactrend.storage.sqlite_dal import SQLiteDAL  # noqa: E402

MOCK_PORT = 47809
READER_PORT = 47808


def _loopback_udp_ok(*ports: int) -> bool:
    for port in ports:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", port))
            s.close()
        except OSError:
            return False
    return True


pytestmark = pytest.mark.skipif(
    not _loopback_udp_ok(MOCK_PORT, READER_PORT),
    reason="loopback UDP bind unavailable (sandboxed runner)",
)


def test_discovery_and_cache_against_mock(tmp_path):
    async def run():
        from .mock_bacnet import build_mock_controller

        mock = build_mock_controller(address=f"127.0.0.1:{MOCK_PORT}")
        cfg = {
            "bacnet": {
                "local_address": f"127.0.0.1:{READER_PORT}",
                "device_instance": 1000,
                "object_name": "HVACTrend-Test",
                "vendor_identifier": 999,
                "safety": {"allow_writes": False, "allow_cov": False},
            }
        }
        reader = await ReadOnlyBACnetApplication.create(cfg)
        try:
            await asyncio.sleep(0.3)  # let both endpoints finish binding

            # 1) Who-Is (directed at the mock) finds exactly the mock device
            devices = await who_is_sweep(reader, address=f"127.0.0.1:{MOCK_PORT}")
            assert len(devices) == 1
            dev = devices[0]
            assert dev.device_instance == 999001
            assert dev.vendor_id == 999
            assert isinstance(dev.segmentation, int)      # captured as enum int, not dropped

            # 2) enumerate objects; units/value normalization is correct
            objects = await enumerate_device(reader, dev)
            by_name = {o.name: o for o in objects if o.name}
            assert "AHU1-DAT" in by_name and "AHU1-FAN" in by_name
            dat = by_name["AHU1-DAT"]
            assert dat.object_type == "analog-input"
            assert dat.units_text == "degrees-fahrenheit"
            assert dat.value_num is not None and 55.0 <= dat.value_num <= 55.6
            assert dat.loggable is True
            fan = by_name["AHU1-FAN"]
            assert fan.value_text == "active"           # binary state-text captured
            assert dev.rpm_supported is True             # mock supports RPM

            # 3) cache the inventory; nothing auto-enabled (opt-in per §9.1)
            dal = SQLiteDAL(tmp_path / "disc.db")
            dal.apply_schema()
            dal._conn.execute("INSERT INTO sites(id, code, name) VALUES (1,'DEMO','Demo Building')")
            dev_id = dal.upsert_device(to_model_device(dev, site_id=1, created_utc=0))
            for o in objects:
                if o.error:
                    continue
                dal.upsert_point(to_model_point(o, device_id=dev_id))

            seg = dal._conn.execute(
                "SELECT segmentation FROM devices WHERE id=?", (dev_id,)).fetchone()[0]
            assert seg is not None                         # segmentation persisted, not NULL
            assert dal.list_points(enabled_only=True) == []
            inventory = {p.name for p in dal.list_points(enabled_only=False)}
            assert {"AHU1-DAT", "AHU1-RAT", "AHU1-FAN"} <= inventory

            # 4) re-discovery is idempotent and preserves an operator selection
            dal._conn.execute("UPDATE points SET enabled=1 WHERE name='AHU1-DAT'")
            dal.upsert_device(to_model_device(dev, site_id=1, created_utc=0))
            for o in objects:
                if not o.error:
                    dal.upsert_point(to_model_point(o, device_id=dev_id))
            still_enabled = {p.name for p in dal.list_points(enabled_only=True)}
            assert still_enabled == {"AHU1-DAT"}, "re-discovery must not clobber selection"
            dal.close()
        finally:
            await reader.close()
            mock.close()

    asyncio.run(run())


def test_enumerate_fast_against_mock(tmp_path):
    """Batched-RPM enumeration returns the same objects (name/units) as the
    one-at-a-time path — the fast path used for big Trane SCs."""
    async def run():
        from .mock_bacnet import build_mock_controller
        from hvactrend.bacnet.discovery import enumerate_device_fast

        mock = build_mock_controller(address=f"127.0.0.1:{MOCK_PORT}")
        cfg = {"bacnet": {"local_address": f"127.0.0.1:{READER_PORT}",
                          "device_instance": 1000, "vendor_identifier": 999,
                          "safety": {"allow_writes": False, "allow_cov": False}}}
        reader = await ReadOnlyBACnetApplication.create(cfg)
        try:
            await asyncio.sleep(0.3)
            devs = await who_is_sweep(reader, address=f"127.0.0.1:{MOCK_PORT}")
            objs = await enumerate_device_fast(reader, devs[0], batch_size=3)
            by_name = {o.name: o for o in objs if o.name}
            assert {"AHU1-DAT", "AHU1-RAT", "AHU1-FAN"} <= set(by_name)
            assert by_name["AHU1-DAT"].units_text == "degrees-fahrenheit"
            assert by_name["AHU1-DAT"].object_type == "analog-input"
        finally:
            await reader.close()
            mock.close()

    asyncio.run(run())


def test_run_discovery_service_caches(tmp_path):
    """The orchestration layer (used by the CLI + API) sweeps, enumerates, caches."""
    async def run():
        from .mock_bacnet import build_mock_controller
        from hvactrend.discovery_service import run_discovery

        mock = build_mock_controller(address=f"127.0.0.1:{MOCK_PORT}")
        cfg = {"bacnet": {"local_address": f"127.0.0.1:{READER_PORT}",
                          "device_instance": 1000, "vendor_identifier": 999,
                          "safety": {"allow_writes": False, "allow_cov": False}}}
        reader = await ReadOnlyBACnetApplication.create(cfg)
        dal = SQLiteDAL(tmp_path / "svc.db")
        dal.apply_schema()
        site_id = dal.ensure_site("DEMO", "Demo Building")
        try:
            await asyncio.sleep(0.3)
            results = await run_discovery(reader, dal, site_id=site_id,
                                          address=f"127.0.0.1:{MOCK_PORT}", timeout=3.0)
            assert len(results) == 1
            dd = results[0]
            assert dd.device.device_instance == 999001
            assert dd.device_id is not None                 # cached
            assert any(o.name == "AHU1-DAT" for o in dd.objects)
            # inventory persisted, opt-in (nothing enabled)
            assert dal.list_points(enabled_only=True) == []
            assert len(dal.list_points(enabled_only=False)) == len(
                [o for o in dd.objects if not o.error])
        finally:
            await reader.close()
            mock.close()

    asyncio.run(run())
