"""Discovery orchestration: sweep -> enumerate -> cache.

Thin glue above the BACnet stack and storage. Dependency direction is clean:
this layer imports bacnet.discovery and storage; neither imports this. Both the
CLI (`scripts/discover.py`) and the API discovery endpoints call `run_discovery`.

Read-only throughout — it only sweeps, reads, and writes the *local* inventory DB.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .bacnet.application import ReadOnlyBACnetApplication
from .bacnet.discovery import (
    DiscoveredDevice,
    DiscoveredObject,
    enumerate_device,
    to_model_device,
    to_model_point,
    who_is_sweep,
)
from .storage.dal import DAL

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DeviceDiscovery:
    device: DiscoveredDevice
    objects: list[DiscoveredObject] = field(default_factory=list)
    device_id: Optional[int] = None      # DB id when cached


async def run_discovery(app: ReadOnlyBACnetApplication, dal: Optional[DAL], *,
                        site_id: Optional[int] = None,
                        low_limit: Optional[int] = None,
                        high_limit: Optional[int] = None,
                        address: Any = None,
                        timeout: float = 3.0,
                        enumerate_objects: bool = True,
                        cache: bool = True,
                        inter_read_delay: float = 0.05) -> list[DeviceDiscovery]:
    """Who-Is sweep, then per-device object enumeration, optionally cached to the DB.

    Set cache=False (or dal=None) for a dry run that touches no storage.
    """
    devices = await who_is_sweep(app, low_limit=low_limit, high_limit=high_limit,
                                 address=address, timeout=timeout)
    out: list[DeviceDiscovery] = []
    for dd in devices:
        objs: list[DiscoveredObject] = []
        if enumerate_objects:
            objs = await enumerate_device(app, dd, inter_read_delay=inter_read_delay)

        dev_id: Optional[int] = None
        if cache and dal is not None:
            now = int(time.time())
            dev_id = dal.upsert_device(to_model_device(dd, site_id=site_id, created_utc=now))
            for obj in objs:
                if obj.error:
                    continue
                dal.upsert_point(to_model_point(obj, device_id=dev_id))
            logger.info("cached device %s (%d objects)", dd.device_instance,
                        sum(1 for o in objs if not o.error))
        out.append(DeviceDiscovery(device=dd, objects=objs, device_id=dev_id))
    return out
