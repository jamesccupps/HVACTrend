"""Downloading a backup: only a file the backup folder itself lists is served."""

import sys

import pytest

from hvactrend.storage.sqlite_dal import SQLiteDAL


@pytest.fixture(autouse=True)
def _isolate_secrets(tmp_path, monkeypatch):
    """Never read (or write) the operator's real config/secrets.env from a test."""
    monkeypatch.setenv("HVACTREND_SECRETS", str(tmp_path / "secrets.env"))


def _client(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from hvactrend.api.app import create_app
    db = tmp_path / "live.db"
    dal = SQLiteDAL(db)
    dal.apply_schema()
    dal.close()
    bk = tmp_path / "backups"
    bk.mkdir()
    cfg = tmp_path / "cfg.yaml"
    fwd = lambda p: str(p).replace(chr(92), "/")
    cfg.write_text(
        "site: {code: DEMO, name: Demo Building}\n"
        "bacnet:\n"
        "  local_address: '<COLLECTOR_IP>/24'\n"
        "  device_instance: '<LOGGER_DEVICE_INSTANCE>'\n"
        "  poll: {interval_s: 120}\n"
        "  safety: {allow_writes: false, allow_cov: false}\n"
        "api: {host: 127.0.0.1, port: 8000, auth: {enabled: false}}\n"
        f"storage: {{path: '{fwd(db)}', backup: {{dir: '{fwd(bk)}'}}}}\n",
        encoding="utf-8")
    return TestClient(create_app(str(cfg), enable_bacnet=False)), bk


def test_a_listed_backup_downloads(tmp_path):
    c, bk = _client(tmp_path)
    (bk / "hvactrend.20261120-0300.db").write_bytes(b"backup bytes")
    with c:
        assert [b["name"] for b in c.get("/api/backups").json()["backups"]] == \
            ["hvactrend.20261120-0300.db"]
        r = c.get("/api/backups/hvactrend.20261120-0300.db")
        assert r.status_code == 200 and r.content == b"backup bytes"


def test_names_outside_the_listing_are_refused(tmp_path):
    c, bk = _client(tmp_path)
    (bk / "hvactrend.20261120-0300.db").write_bytes(b"backup bytes")
    (tmp_path / "hvactrend.outside.db").write_bytes(b"not a backup")
    with c:
        for name, code in (("hvactrend.missing.db", 404),
                           ("hvactrend.20261120-0300.db:x.db", 404),
                           ("..%5Chvactrend.outside.db", 400),
                           ("notes.txt", 400)):
            assert c.get("/api/backups/" + name).status_code == code, name


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS alternate data streams")
def test_an_ntfs_stream_inside_a_backup_is_not_served(tmp_path):
    """The name check alone passed "<backup>.db:x.db", which opens a stream of that file."""
    c, bk = _client(tmp_path)
    f = bk / "hvactrend.20261120-0300.db"
    f.write_bytes(b"backup bytes")
    try:
        with open(str(f) + ":x.db", "wb") as s:
            s.write(b"hidden stream")
    except OSError:
        pytest.skip("this filesystem has no alternate data streams")
    with c:
        r = c.get("/api/backups/hvactrend.20261120-0300.db:x.db")
        assert r.status_code == 404 and b"hidden" not in r.content
