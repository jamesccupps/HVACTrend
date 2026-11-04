"""Core data-model dataclasses.

Mirror of db/schema.sql — Site -> Building -> System -> Device -> Point plus the
long/narrow time-series Sample. Plain read-only-friendly containers; persistence
lives in the DAL (storage/dal.py). No BACnet write / command is representable in
this model by design.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# Object types that carry a loggable present-value (or a harvestable trend log).
# Shared by discovery (flagging) and the DAL/browser (filtering).
LOGGABLE_OBJECT_TYPES = frozenset({
    "analog-input", "analog-output", "analog-value",
    "binary-input", "binary-output", "binary-value",
    "multi-state-input", "multi-state-output", "multi-state-value",
    "large-analog-value", "integer-value", "positive-integer-value",
    "trend-log",
})


class PointSource(str, Enum):
    BACNET = "bacnet"
    DERIVED = "derived"   # virtual points (delta, wet-bulb, tower KPIs — plan §7)
    WEATHER = "weather"   # Tempest / Open-Meteo


class Quality(str, Enum):
    GOOD = "good"
    STALE = "stale"        # flatline / unchanged too long
    FAULT = "fault"        # reliability != no-fault-detected, or out-of-service
    COMM_LOSS = "comm-loss"


@dataclass(slots=True)
class Site:
    code: str
    name: str
    tz: str = "America/New_York"
    id: Optional[int] = None
    notes: Optional[str] = None


@dataclass(slots=True)
class Building:
    code: str
    name: str
    site_id: Optional[int] = None
    id: Optional[int] = None
    notes: Optional[str] = None


@dataclass(slots=True)
class System:
    """Logical equipment grouping (AHU-1, Condenser Loop). NOT 1:1 with a device."""
    code: str
    name: str
    building_id: Optional[int] = None
    kind: Optional[str] = None       # 'ahu' | 'tower' | 'boiler' | 'pump' | 'hv'
    id: Optional[int] = None
    notes: Optional[str] = None


@dataclass(slots=True)
class Device:
    """A BACnet controller. Addressed by device_instance; stack handles routing."""
    device_instance: int
    site_id: Optional[int] = None
    name: Optional[str] = None
    address: Optional[str] = None
    network_number: Optional[int] = None   # None = local; else remote (routed UC)
    mac_address: Optional[str] = None
    router_instance: Optional[int] = None  # the SC/PXC that routes to this device
    vendor_id: Optional[int] = None
    vendor_name: Optional[str] = None
    model_name: Optional[str] = None
    firmware: Optional[str] = None
    is_router: bool = False
    rpm_supported: Optional[bool] = None   # None = unknown until Phase 1
    poll_budget_per_min: Optional[int] = None  # per-trunk read budget if this device routes MS/TP
    segmentation: Optional[int] = None
    last_seen_utc: Optional[int] = None
    id: Optional[int] = None
    notes: Optional[str] = None


@dataclass(slots=True)
class Point:
    """A loggable point: real BACnet object, derived/virtual, or weather."""
    source: PointSource = PointSource.BACNET
    device_id: Optional[int] = None        # None for derived/weather
    system_id: Optional[int] = None
    object_type: Optional[str] = None      # 'analogInput', 'binaryValue', 'trendLog'
    object_instance: Optional[int] = None
    name: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None
    units: Optional[int] = None            # BACnet EngineeringUnits enum
    units_text: Optional[str] = None
    poll_interval_s: Optional[int] = None  # None -> global interval
    cov_enabled: bool = False
    trendlog_harvest: bool = False
    enabled: bool = True
    # Deterministic out-of-range alarm limits — computed by us, never written to the device.
    alarm_enabled: bool = False
    alarm_hi: Optional[float] = None
    alarm_lo: Optional[float] = None
    alarm_deadband: Optional[float] = None
    spec: Optional[str] = None             # JSON: derived formula / weather map / COV params
    id: Optional[int] = None


@dataclass(slots=True)
class Sample:
    """One time-series row. Status captured every sample."""
    point_id: int
    ts_utc: int                            # epoch seconds, UTC
    value_num: Optional[float] = None
    value_text: Optional[str] = None       # resolved state-text for enum/binary/multistate
    status_flags: Optional[str] = None     # BACnet status-flags as text ('in-alarm;fault')
    reliability: Optional[int] = None
    out_of_service: Optional[bool] = None
    quality: Quality = Quality.GOOD


@dataclass(slots=True)
class PollHealth:
    """One poll-cycle result per device."""
    device_id: int
    ts_utc: int
    ok: bool
    latency_ms: Optional[int] = None
    reads_attempted: Optional[int] = None
    reads_ok: Optional[int] = None
    method: Optional[str] = None           # 'rpm' | 'single'
    error: Optional[str] = None
    id: Optional[int] = None


# ── AI engine ──────────────────────────────────────────────────────
class Severity(str, Enum):
    """Finding severity ladder (schema `findings.severity`). Ordered by rank()."""
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    def rank(self) -> int:
        return ["info", "low", "medium", "high", "critical"].index(self.value)


@dataclass(slots=True)
class Anomaly:
    """A deterministic pre-screen candidate — surfaced to the LLM as a
    lead, not a conclusion. Not persisted directly; it feeds the context builder."""
    kind: str                              # 'stuck' | 'fault' | 'comm-loss' | 'out-of-range' | 'deviation'
    system_code: str
    point_id: int
    point_name: str
    severity: Severity
    metric: str
    detail: str                            # human-readable one-liner
    evidence: dict = field(default_factory=dict)


@dataclass(slots=True)
class Report:
    """One AI report (schema `reports`). Prose + a set of structured findings."""
    kind: str                              # 'daily' | 'weekly' | 'manual'
    period_start_utc: int
    period_end_utc: int
    created_utc: int
    provider: Optional[str] = None         # 'claude' | 'ollama'
    model: Optional[str] = None
    prose: Optional[str] = None
    status: str = "complete"               # pending | complete | error
    id: Optional[int] = None


@dataclass(slots=True)
class Finding:
    """One structured finding (schema `findings`). `fingerprint` is the stable key
    that lets the same issue be tracked across reports (first_seen/last_seen)."""
    summary: str
    created_utc: int
    report_id: Optional[int] = None
    system_id: Optional[int] = None
    point_id: Optional[int] = None
    fingerprint: Optional[str] = None
    severity: Optional[str] = None         # Severity value
    timeframe: Optional[str] = None        # 'immediate' | 'this-week' | 'monitor'
    equipment: Optional[str] = None
    metric: Optional[str] = None
    recommendation: Optional[str] = None
    evidence: Optional[str] = None         # JSON string (schema mirror)
    status: str = "open"                   # open | acknowledged | closed
    first_seen_utc: Optional[int] = None
    last_seen_utc: Optional[int] = None
    id: Optional[int] = None


@dataclass(slots=True)
class Alert:
    """An active between-report alert (schema `alerts`). A high/critical condition the
    continuous check raised so it's caught in minutes, not at the next report. Tracked by
    `fingerprint` (same live condition updates in place), auto-resolved when it clears."""
    kind: str                              # 'fault'|'comm-loss'|'out-of-range'|'collector-stale'|...
    severity: str                          # 'high' | 'critical'
    message: str
    fingerprint: str
    created_utc: int
    last_seen_utc: int
    system_code: Optional[str] = None
    point_id: Optional[int] = None
    point_name: Optional[str] = None
    source: str = "prescreen"              # 'prescreen' | 'watchdog' — each resolves its own
    status: str = "open"                   # open | resolved
    resolved_utc: Optional[int] = None
    notified_utc: Optional[int] = None
    id: Optional[int] = None
