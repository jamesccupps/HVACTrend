"""Data-access layer interface.

The rest of the app talks to storage ONLY through this abstract interface, so
SQLite can later be swapped for TimescaleDB or DuckDB without touching the
collector, API, or AI engine. Keep engine-specific SQL behind the implementation.

Phase 0 defines the seam and the method surface. Bodies are implemented in
sqlite_dal.py (schema apply + core writes/reads scaffolded; the rest raise
NotImplementedError with the phase that fills them in).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterable, Optional, Sequence

from ..models import Device, Point, PollHealth, Sample


class DAL(ABC):
    """Abstract storage interface. All timestamps are epoch seconds, UTC."""

    # ── lifecycle / schema ───────────────────────────────────────────────────
    @abstractmethod
    def apply_schema(self) -> None:
        """Create tables from db/schema.sql if absent (idempotent)."""

    @abstractmethod
    def close(self) -> None:
        ...

    # ── topology (populated by the discovery browser, plan §9.1) ─────────────
    @abstractmethod
    def upsert_device(self, device: Device) -> int:
        """Insert/update a device; return its id."""

    @abstractmethod
    def upsert_point(self, point: Point) -> int:
        """Insert/update a point; return its id."""

    @abstractmethod
    def list_points(self, *, enabled_only: bool = True) -> list[Point]:
        """Points the collector should sample."""

    # ── time-series ────────────────────────────────────────────────
    @abstractmethod
    def write_samples(self, samples: Iterable[Sample]) -> int:
        """Bulk-insert samples (idempotent on (point_id, ts_utc)); return count."""

    @abstractmethod
    def latest_value(self, point_id: int) -> Optional[Sample]:
        """Most recent sample for a point (for the overview wall / live value)."""

    @abstractmethod
    def query_series(self, point_id: int, start_utc: int, end_utc: int, *,
                     rollup: Optional[str] = None) -> Sequence[Any]:
        """Return samples (or rollup buckets) for a point over [start, end].

        rollup: None (raw) | '5m' | '1h'.
        """

    # ── rollups / retention ──────────────────────────
    @abstractmethod
    def build_rollups(self, up_to_utc: int) -> None:
        """Compute/refresh 5m and 1h rollups up to a watermark."""

    @abstractmethod
    def enforce_retention(self, now_utc: int, policy: dict) -> None:
        """Prune raw/rollup data older than the retention windows."""

    # ── logger health ──────────────────────────────────────────────
    @abstractmethod
    def record_poll_health(self, health: PollHealth) -> None:
        ...

    # ── settings (DB-backed operator overrides, plan §11) ────────────────────
    @abstractmethod
    def get_setting(self, key: str) -> Any:
        """JSON-decoded value for a dotted setting key, or None."""

    @abstractmethod
    def set_setting(self, key: str, value: Any) -> None:
        """Upsert a dotted setting key with a JSON-encodable value."""

    @abstractmethod
    def delete_setting(self, key: str) -> None:
        ...

    @abstractmethod
    def all_settings(self) -> dict[str, Any]:
        """All dotted setting keys -> JSON-decoded values."""

    # ── online backup ─────────────────────────────────────────
    @abstractmethod
    def backup_to(self, dest_path: str) -> None:
        """Consistent online backup of the live DB to dest_path (safe while writing)."""
