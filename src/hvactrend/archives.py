"""Where the archives are, and how old the newest one is -- read from the FILES.

Shared by the collector, which decides when the next snapshot / database backup is due, and
the watchdog's daily check-in, which reports whether they are actually happening. One
definition of "newest", so the two can never disagree about it.

Kept out of collector/service.py on purpose: the watchdog must not import the collector.
That module pulls in the BACnet stack, and a broken collector deploy would then take down
the one process whose job is to report a broken collector.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import time
import zipfile
import zlib
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[2]
SNAPSHOT_SCRIPT = REPO / "scripts" / "snapshot.py"

# The only names a finished database archive can have. `.partial` files never match: the
# copy is renamed into this pattern only after it verifies.
DB_BACKUP_GLOB = "hvactrend.*.db"
# ...and a finished OFF-SITE copy of one: the archive's own name plus .zip.
OFFSITE_GLOB = "hvactrend.*.db.zip"


def newest_archive_mtime(dests) -> float:
    """Newest snapshot archive across every destination, or 0.0 if there are none.

    This is the DURABLE STATE the snapshot schedule reads. Process uptime is not a clock on
    this box -- the service restarts on failure and on every reboot -- so "when did we last
    snapshot" has to be answered by the files, which are the only thing that survives."""
    newest = 0.0
    for dest in dests or []:
        try:
            for f in Path(dest).glob("HVACTrend_snapshot_*.zip"):
                newest = max(newest, f.stat().st_mtime)
        except OSError:
            continue
    return newest


def newest_db_backup(bdir) -> "tuple[Optional[Path], float]":
    """(newest finished database archive, its mtime), or (None, 0.0) when there is none."""
    best: Optional[Path] = None
    t = 0.0
    try:
        for f in Path(bdir).glob(DB_BACKUP_GLOB):
            m = f.stat().st_mtime
            if m > t:
                best, t = f, m
    except OSError:
        pass
    return best, t


def snapshot_dests(script: Path = SNAPSHOT_SCRIPT) -> "list[Path]":
    """The snapshot destinations exactly as scripts/snapshot.py computes them.

    Loaded from the script rather than re-derived, because the OneDrive half is subtle: the
    service runs as SYSTEM, whose Path.home() is the systemprofile, where OneDrive never
    syncs. snapshot.py resolves the operator's real profile; a second copy of that logic is
    a second chance to get it wrong. [] if the script is missing or will not load."""
    try:
        spec = importlib.util.spec_from_file_location("occ_snapshot_dests", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return [Path(d) for d in getattr(mod, "DESTS", [])]
    except Exception:  # noqa: BLE001 -- a report about backups must not crash on one
        return []


# ── off-site copies of the database backups ─────────────────────────────────────────
# Until 2026-09-29 every database backup sat on the same disk as the database it protects:
# one disk failure would have taken the record and all its backups together. The operator chose
# OneDrive as the off-site destination. Only a FINISHED, VERIFIED archive is ever copied --
# never the live database, which a sync client would upload mid-write.

def offsite_name(archive) -> str:
    """An off-site copy is named after its archive, so each one says which backup it is."""
    return Path(archive).name + ".zip"


def newest_offsite_copy(dest_dir) -> "tuple[Optional[Path], float]":
    """(newest finished off-site copy, its mtime), or (None, 0.0)."""
    best: Optional[Path] = None
    t = 0.0
    try:
        for f in Path(dest_dir).glob(OFFSITE_GLOB):
            m = f.stat().st_mtime
            if m > t:
                best, t = f, m
    except OSError:
        pass
    return best, t


def _crc32_of(path, bufsize: int = 8 << 20) -> int:
    crc = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(bufsize)
            if not b:
                return crc & 0xFFFFFFFF
            crc = zlib.crc32(b, crc)


def make_offsite_copy(archive, dest_dir, *, staging_dir, keep: int = 2,
                      min_free_bytes: int = 0) -> dict:
    """Compress ONE verified database archive into `dest_dir`, prove the copy, then keep only
    the newest `keep` copies there. Raises on any failure; the caller logs and retries later.

    The zip is built in `staging_dir`, OUTSIDE the synced folder, and moved in by a rename
    only after it checks out: its stored CRC must equal an independent CRC of the archive,
    and testzip() must decompress every byte cleanly. A sync client watching `dest_dir`
    therefore never sees -- and never uploads -- a half-written file. (Different volumes
    cannot rename atomically, so there the copy goes in under a .partial name first.)

    Measured 2026-09-29: the 1.75 GB archive compresses to ~22 % (~0.38 GB) in ~33 s."""
    archive, dest_dir, staging_dir = Path(archive), Path(dest_dir), Path(staging_dir)
    final = dest_dir / offsite_name(archive)
    if final.exists():
        return {"copy": final, "skipped": True, "pruned": []}
    t0 = time.monotonic()
    size = archive.stat().st_size
    staging_dir.mkdir(parents=True, exist_ok=True)
    dest_dir.mkdir(parents=True, exist_ok=True)
    for stale in staging_dir.glob("*.partial"):          # a hard kill mid-zip leaves one
        try:
            stale.unlink()
        except OSError:
            pass
    if min_free_bytes:
        free = shutil.disk_usage(str(staging_dir)).free
        if free - size // 2 < min_free_bytes:            # the zip is ~22 %; allow 50 %
            raise RuntimeError(f"not enough free space to stage the copy: {free / 1e9:.1f} GB "
                               f"free, floor {min_free_bytes / 1e9:.1f} GB")
    tmp = staging_dir / (final.name + ".partial")
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6,
                             allowZip64=True) as z:
            z.write(archive, arcname=archive.name)
        with zipfile.ZipFile(tmp) as z:
            info = z.getinfo(archive.name)
            if info.file_size != size or info.CRC != _crc32_of(archive):
                raise RuntimeError(f"the compressed copy of {archive.name} does not match it")
            bad = z.testzip()                           # decompresses all of it
            if bad is not None:
                raise RuntimeError(f"the compressed copy of {archive.name} is corrupt ({bad})")
        try:
            os.replace(tmp, final)
        except OSError:                                 # a different volume
            part = dest_dir / (final.name + ".partial")
            shutil.copyfile(tmp, part)
            os.replace(part, final)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    pruned = []
    copies = sorted(dest_dir.glob(OFFSITE_GLOB), key=lambda f: f.stat().st_mtime, reverse=True)
    for old in copies[max(1, int(keep)):]:
        if old == final:
            continue
        try:
            old.unlink()
            pruned.append(old.name)
        except OSError:
            pass
    return {"copy": final, "skipped": False, "bytes": final.stat().st_size,
            "archive_bytes": size, "seconds": time.monotonic() - t0, "pruned": pruned}
