"""Versioned snapshot of this installation's SETUP: the config/ folder.

The trend history has its own backup (storage.backup: the database, weekly). This covers the
hand-made part a database backup does not: config/config.yaml, with its BACnet identity,
equipment map, site knowledge and alarm thresholds, and the AI report prompts. It is small,
and losing it means redoing the commissioning by hand.

Writes a timestamped .zip to data/snapshots and, when storage.backup.offsite_dir is set, to
that synced folder too (OneDrive, Google Drive, a NAS share), keeps a fortnight of daily
archives in each, and verifies each archive after writing: an unverified backup is not a
backup.

  python scripts/snapshot.py              # snapshot now
  python scripts/snapshot.py --list       # what exists, where, how old

The collector takes one a day by itself (storage.snapshot_interval_s; 0 = off).

NEVER includes config/secrets.env, and the guard checks archive CONTENTS for the secret
VALUES, not just the filename: rotated logs once carried a weather-service token in
plaintext because an HTTP library logged full request URLs.
"""
from __future__ import annotations

import os
import sys
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
KEEP = 14                       # days of history to retain per destination
SOURCES = ("config",)           # what a snapshot holds, relative to the install


def _offsite_dir() -> Path | None:
    """storage.backup.offsite_dir from config/config.yaml, when one is set. Read directly
    (not through the full config loader) so a snapshot never depends on the rest of the
    configuration being valid -- that is when it is most needed."""
    try:
        import yaml
        cfg = yaml.safe_load((REPO / "config" / "config.yaml").read_text(encoding="utf-8")) or {}
        d = ((cfg.get("storage") or {}).get("backup") or {}).get("offsite_dir")
        return Path(d) / "HVACTrend_snapshots" if d else None
    except Exception:  # noqa: BLE001 -- no config, no YAML, no folder: no off-site copy
        return None


DESTS = [d for d in (REPO / "data" / "snapshots", _offsite_dir()) if d is not None]

EXCLUDE_FILES = {"secrets.env"}
EXCLUDE_EXT = {".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".log", ".zip"}

# Present in every snapshot or the snapshot is not worth keeping.
REQUIRED = ("config/config.yaml",)


def _secret_values() -> list[str]:
    """The actual secret strings, so the guard can scan CONTENTS rather than filenames."""
    out: list[str] = []
    sp = REPO / "config" / "secrets.env"
    if sp.exists():
        for line in sp.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                if len(v) >= 12:
                    out.append(v)
    return out


def _build(target: Path) -> tuple[int, int]:
    target.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for src in SOURCES:
            for root, dirs, files in os.walk(REPO / src):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for f in files:
                    if f in EXCLUDE_FILES or f.startswith("secrets") \
                            or Path(f).suffix.lower() in EXCLUDE_EXT:
                        continue
                    full = Path(root) / f
                    z.write(full, Path("HVACTrend") / full.relative_to(REPO))
                    n += 1
    return n, target.stat().st_size


def _verify(target: Path, secrets: list[str]) -> None:
    """Raise unless the archive opens, carries the files that matter, and leaks nothing."""
    with zipfile.ZipFile(target) as z:
        if z.testzip() is not None:
            raise RuntimeError(f"{target.name}: archive is corrupt")
        names = z.namelist()
        for req in REQUIRED:
            if not any(x.endswith(req) for x in names):
                raise RuntimeError(f"{target.name}: missing {req}")
        if any(x.endswith("config/secrets.env") for x in names):
            raise RuntimeError(f"{target.name}: secrets.env is IN the archive")
        for nm in names:
            blob = z.read(nm).decode("utf-8", "ignore")
            for v in secrets:
                if v in blob:
                    raise RuntimeError(f"{target.name}: a secret VALUE appears in {nm}")


def _prune(dest: Path) -> list[str]:
    """Keep KEEP DAYS of history, not KEEP FILES.

    Counting files was itself a data-loss path: a restart loop, or several manual runs in an
    afternoon, pushed genuine daily archives out of the window within minutes while the file
    count sat reassuringly at KEEP. Now every archive from the last 24 h survives, plus the
    newest archive of each of the KEEP most recent days. A burst can no longer evict a day it
    did not replace."""
    archives = sorted(dest.glob("HVACTrend_snapshot_*.zip"))
    if not archives:
        return []
    now = time.time()
    by_day: dict[str, list[Path]] = {}
    for f in archives:
        day = time.strftime("%Y-%m-%d", time.localtime(f.stat().st_mtime))
        by_day.setdefault(day, []).append(f)
    survivors = {f for f in archives if now - f.stat().st_mtime < 86400}
    for day in sorted(by_day, reverse=True)[:KEEP]:
        survivors.add(by_day[day][-1])          # the newest archive of that day
    dropped = []
    for f in archives:
        if f not in survivors:
            f.unlink()
            dropped.append(f.name)
    return dropped


def snapshot() -> list[str]:
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    secrets = _secret_values()
    written = []
    for dest in DESTS:
        target = dest / f"HVACTrend_snapshot_{stamp}.zip"
        try:
            n, size = _build(target)
            _verify(target, secrets)
        except Exception as exc:                      # one bad destination must not stop the other
            if target.exists():
                target.unlink()                       # never leave an unverified archive
            written.append(f"{dest}  FAILED: {exc}")
            continue
        try:
            dropped = _prune(dest)
        except OSError as exc:      # a locked old archive must not cost the one just verified
            dropped, note = [], f", prune skipped: {exc}"
        else:
            note = f", pruned {len(dropped)}" if dropped else ""
        written.append(f"{target}  ({n} files, {size/1024:.0f} KB{note})")
    return written


def _list() -> None:
    now = time.time()
    for dest in DESTS:
        print(f"\n{dest}")
        if not dest.exists():
            print("   (does not exist yet)")
            continue
        zips = sorted(dest.glob("HVACTrend_snapshot_*.zip"))
        if not zips:
            print("   (empty)")
        for z in zips[-KEEP:]:
            age = (now - z.stat().st_mtime) / 3600
            print(f"   {z.name}   {z.stat().st_size/1024:6.0f} KB   {age:5.1f} h old")


if __name__ == "__main__":
    if "--list" in sys.argv:
        _list()
    else:
        for line in snapshot():
            print(line)
