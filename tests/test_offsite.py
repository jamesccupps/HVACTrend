"""Off-site copies of the database backups (archives.make_offsite_copy + the collector job).

Until 2026-09-29 every backup sat on the same disk as the database it protects; the operator chose
OneDrive for the off-site copy. These pin:
  * the copy is a verified, byte-identical zip of the ARCHIVE (never the live database);
  * the synced folder never sees a half-written file, even while a copy is failing;
  * a copy whose CRC does not match its archive is refused;
  * only the newest `keep` copies stay, and the new one is never the one pruned;
  * no room on the disk means no copy;
  * the collector copies the NEWEST archive once, detached, waits while a backup is being
    written, and backs off for hours after a failure;
  * the daily check-in reports the copy, and says so when there is none.
"""

import asyncio
import os
import sqlite3
import time
import zipfile
from pathlib import Path

import pytest

from hvactrend import archives, checkin


def _archive(path: Path, rows: int = 2000) -> Path:
    c = sqlite3.connect(path)
    with c:
        c.execute("CREATE TABLE samples (point_id INT, ts_utc INT, value_num REAL)")
        c.executemany("INSERT INTO samples VALUES (?,?,?)",
                      [(1, 1_700_000_000 + i, float(i % 7)) for i in range(rows)])
    c.close()
    return path


def _setup(tmp_path, n=1):
    """n verified-looking archives, oldest first, a day apart; and an empty OneDrive folder."""
    bdir = tmp_path / "backups"
    bdir.mkdir(parents=True)
    arcs = []
    for i in range(n):
        p = _archive(bdir / f"hvactrend.2026092{i}_155228.db")
        t = time.time() - (n - i) * 86400
        os.utime(p, (t, t))
        arcs.append(p)
    return bdir, tmp_path / "OneDrive" / "HVACTrend_db_backups", arcs


# ── the copy itself ────────────────────────────────────────────────────────────────────
def test_the_copy_is_a_verified_zip_of_the_archive(tmp_path):
    bdir, dest, (a,) = _setup(tmp_path)
    staging = bdir / ".offsite_staging"
    r = archives.make_offsite_copy(a, dest, staging_dir=staging, keep=2)
    copy = dest / (a.name + ".zip")
    assert r["copy"] == copy and r["skipped"] is False and copy.exists()
    with zipfile.ZipFile(copy) as z:
        assert z.namelist() == [a.name]
        assert z.read(a.name) == a.read_bytes()
    assert r["bytes"] < r["archive_bytes"]                   # it really compressed
    assert list(dest.iterdir()) == [copy]                     # nothing else in the synced folder
    assert list(staging.iterdir()) == []                      # and no staging leftovers
    assert archives.make_offsite_copy(a, dest, staging_dir=staging)["skipped"] is True


def test_the_synced_folder_never_sees_a_half_written_file(tmp_path, monkeypatch):
    """Watched DURING the write, not after it: the cleanup in `finally` would hide a partial
    file that OneDrive had already seen and started uploading."""
    bdir, dest, (a,) = _setup(tmp_path)
    staging = bdir / ".offsite_staging"
    seen = []
    real_write = zipfile.ZipFile.write

    def dies_midway(self, *args, **kw):
        real_write(self, *args, **kw)                         # the half-made zip exists...
        seen.extend(p.name for p in dest.iterdir())            # ...what can OneDrive see now?
        raise OSError("disk full")
    monkeypatch.setattr(zipfile.ZipFile, "write", dies_midway)
    with pytest.raises(OSError):
        archives.make_offsite_copy(a, dest, staging_dir=staging)
    assert seen == [], f"the synced folder held {seen} while the copy was being written"
    assert list(dest.iterdir()) == [] and list(staging.iterdir()) == []


def test_a_copy_that_does_not_match_its_archive_is_refused(tmp_path, monkeypatch):
    bdir, dest, (a,) = _setup(tmp_path)
    monkeypatch.setattr(archives, "_crc32_of", lambda path, bufsize=0: 12345)
    with pytest.raises(RuntimeError, match="does not match"):
        archives.make_offsite_copy(a, dest, staging_dir=bdir / ".offsite_staging")
    assert list(dest.glob("*")) == []


def test_only_the_newest_copies_are_kept(tmp_path):
    bdir, dest, arcs = _setup(tmp_path, n=3)
    staging = bdir / ".offsite_staging"
    pruned = []
    for i, a in enumerate(arcs):
        pruned += archives.make_offsite_copy(a, dest, staging_dir=staging, keep=2)["pruned"]
        c = dest / (a.name + ".zip")
        t = time.time() - (3 - i) * 3600                       # distinct, ordered mtimes
        os.utime(c, (t, t))
    assert sorted(p.name for p in dest.glob("*.zip")) == sorted(a.name + ".zip" for a in arcs[1:])
    assert pruned == [arcs[0].name + ".zip"]
    # keep=0 is nonsense; the copy just made is never the one pruned
    bdir2, dest2, (b,) = _setup(tmp_path / "k0")
    archives.make_offsite_copy(b, dest2, staging_dir=bdir2 / ".s", keep=0)
    assert (dest2 / (b.name + ".zip")).exists()


def test_no_room_means_no_copy(tmp_path, monkeypatch):
    bdir, dest, (a,) = _setup(tmp_path)

    class _U:
        free = 10
    monkeypatch.setattr(archives.shutil, "disk_usage", lambda p: _U())
    with pytest.raises(RuntimeError, match="free space"):
        archives.make_offsite_copy(a, dest, staging_dir=bdir / ".offsite_staging",
                                   min_free_bytes=10 ** 9)
    assert list(dest.glob("*")) == []


# ── the collector job ──────────────────────────────────────────────────────────────────
def _col(bdir, dest):
    from hvactrend.collector.service import Collector
    return Collector(app=None, dal=object(), config={"storage": {"backup": {
        "interval_s": 0, "dir": str(bdir), "offsite_dir": str(dest), "offsite_keep": 2}}})


def test_the_collector_copies_the_newest_archive_once(tmp_path):
    bdir, dest, arcs = _setup(tmp_path, n=2)
    col = _col(bdir, dest)

    async def go():
        col._maybe_offsite_copy()
        first = col._offsite_task
        assert first is not None, "the copy did not become a detached task"
        await first
        col._next_offsite_check = None                        # look again at once
        col._maybe_offsite_copy()
        return first
    first = asyncio.run(go())
    assert [p.name for p in dest.glob("*.zip")] == [arcs[-1].name + ".zip"]   # the NEWEST
    assert col._offsite_task is first, "an up-to-date copy started a second job"


def test_the_copy_waits_while_a_backup_is_being_written(tmp_path):
    from hvactrend.collector.service import _BACKUP_LOCK
    bdir, dest, _ = _setup(tmp_path)
    col = _col(bdir, dest)
    assert _BACKUP_LOCK.acquire(blocking=False), "lock unexpectedly held by another test"
    try:
        async def go():
            col._maybe_offsite_copy()
        before = time.monotonic()
        asyncio.run(go())
        after = time.monotonic()
        assert col._offsite_task is None
    finally:
        _BACKUP_LOCK.release()
    # Bracketed by readings on both sides: on Windows' coarse clock the test read the same
    # tick the code did, and "next - now <= 300" came out 300.0000000001 (CI, 2026-11-04).
    assert before < col._next_offsite_check <= after + 300, "look again within minutes"


def test_a_failed_copy_backs_off_for_hours(tmp_path, monkeypatch):
    import hvactrend.collector.service as svc
    bdir, dest, _ = _setup(tmp_path)
    col = _col(bdir, dest)

    def boom(*a, **k):
        raise OSError("the OneDrive folder is gone")
    monkeypatch.setattr(svc, "make_offsite_copy", boom)

    async def go():
        col._maybe_offsite_copy()
        await col._offsite_task
    asyncio.run(go())
    assert col._next_offsite_check - time.monotonic() > 5 * 3600


def test_no_offsite_dir_means_no_copy(tmp_path):
    from hvactrend.collector.service import Collector
    bdir, dest, _ = _setup(tmp_path)
    col = Collector(app=None, dal=object(), config={"storage": {"backup": {"dir": str(bdir)}}})

    async def go():
        col._maybe_offsite_copy()
    asyncio.run(go())
    assert col._offsite_task is None and not dest.exists()


# ── what the check-in says ─────────────────────────────────────────────────────────────
def test_the_checkin_reports_the_offsite_copy(tmp_path):
    bdir, dest, (a,) = _setup(tmp_path)
    archives.make_offsite_copy(a, dest, staging_dir=bdir / ".offsite_staging")
    cfg = {"storage": {"backup": {"dir": str(bdir), "interval_s": 604800,
                                  "offsite_dir": str(dest)}}}
    s = checkin.build(object(), cfg, int(time.time()), dests=[])
    assert dict(s["facts"])["Off-site copy"].endswith("in OneDrive")
    assert not any("off-site" in x or "copied to" in x for x in s["attention"]), s["attention"]


def test_the_checkin_says_when_there_is_no_offsite_copy(tmp_path):
    bdir, dest, arcs = _setup(tmp_path, n=2)
    now = int(time.time())
    base = {"dir": str(bdir), "interval_s": 604800}

    s = checkin.build(object(), {"storage": {"backup": base}}, now, dests=[])
    assert any("exist only on this disk" in x for x in s["attention"])

    cfg = {"storage": {"backup": {**base, "offsite_dir": str(dest)}}}
    s = checkin.build(object(), cfg, now, dests=[])
    assert any("no off-site copy of the database in OneDrive" in x for x in s["attention"])

    # only the OLDER archive was copied, and the newest has waited a day and a half
    archives.make_offsite_copy(arcs[0], dest, staging_dir=bdir / ".offsite_staging")
    os.utime(arcs[1], (now - 36 * 3600, now - 36 * 3600))
    os.utime(arcs[0], (now - 72 * 3600, now - 72 * 3600))
    s = checkin.build(object(), cfg, now, dests=[])
    assert any("has not been copied to OneDrive" in x for x in s["attention"]), s["attention"]
    # ...but a backup made in the last day is simply not due for its copy yet
    os.utime(arcs[1], (now - 3600, now - 3600))
    s = checkin.build(object(), cfg, now, dests=[])
    assert not any("has not been copied" in x for x in s["attention"]), s["attention"]
