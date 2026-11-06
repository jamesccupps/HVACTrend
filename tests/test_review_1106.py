"""Fixes from the 2026-11-06 external review (batch 1). Each test was run against the code
before its fix and failed.

H1 the Host allowlist · H3 the watchdog's last resort · M1 cross-site writes and acting GETs
· M3 the Tempest token in errors · M4 weather off the loop · M5 the BACnet port check · M8 a
priority reply that will not decode · M9 +/-Inf · M10 body size · M11 report limits · M13 the
access keys · M14 readings in the future · and the Lows: atomic secrets, device identity,
CSV formulas, security headers.
"""

import asyncio
import importlib.util
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from hvactrend.storage.sqlite_dal import SQLiteDAL

REPO = Path(__file__).resolve().parents[1]


# ── the web gate ────────────────────────────────────────────────────────────────────────
def _app(tmp_path, extra: str = ""):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    db = tmp_path / "w.db"
    dal = SQLiteDAL(db)
    dal.apply_schema()
    dal.close()
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "site: {code: S, name: S}\n"
        "bacnet:\n  local_address: '<COLLECTOR_IP>/24'\n"
        "  device_instance: '<LOGGER_DEVICE_INSTANCE>'\n  poll: {interval_s: 120}\n"
        "  safety: {allow_writes: false, allow_cov: false}\n"
        f"storage: {{path: '{db.as_posix()}'}}\n" + extra, encoding="utf-8")
    from hvactrend.api.app import create_app
    return create_app(str(cfg), enable_bacnet=False)


def test_a_foreign_host_name_is_refused(tmp_path, caplog):
    """DNS rebinding: a page whose own name now points at 127.0.0.1 sends ITS name."""
    from fastapi.testclient import TestClient
    app = _app(tmp_path, "api: {allowed_hosts: [trends.example.lan]}\n")
    with TestClient(app) as c:
        assert c.get("/health", headers={"host": "evil.example"}).status_code == 421
        assert c.get("/api/settings", headers={"host": "evil.example:8000"}).status_code == 421
        for ok in ("localhost:8000", "127.0.0.1", "[::1]:8000", "TRENDS.example.lan:8000",
                   socket.gethostname()):
            assert c.get("/health", headers={"host": ok}).status_code == 200, ok
        c.get("/health", headers={"host": "evil.example"})
    said = [r.getMessage() for r in caplog.records if "allowed_hosts" in r.getMessage()]
    assert len(said) == 2 and "'evil.example'" in said[0]   # each name once: findable


def test_a_write_from_another_site_is_refused(tmp_path):
    from fastapi.testclient import TestClient
    app = _app(tmp_path)
    with TestClient(app) as c:
        # no BACnet stack here: a request that gets through the gate answers 503
        assert c.post("/api/discover", headers={"sec-fetch-site": "cross-site"}).status_code == 403
        assert c.post("/api/discover", headers={"origin": "http://evil.example"}).status_code == 403
        assert c.post("/api/discover", headers={"origin": "null"}).status_code == 403
        assert c.post("/api/discover", headers={"sec-fetch-site": "same-origin",
                                                "origin": "http://testserver"}).status_code == 503
        assert c.post("/api/discover").status_code == 503          # curl, a script
        # reads still work from anywhere the host check allows
        assert c.get("/health", headers={"sec-fetch-site": "cross-site"}).status_code == 200


def test_the_gets_that_read_the_plant_are_posts_now(tmp_path):
    from fastapi.testclient import TestClient
    app = _app(tmp_path)
    with TestClient(app) as c:
        assert c.get("/api/points/1/read").status_code == 405
        assert c.get("/api/devices/1/objects").status_code == 405
        r = c.get("/api/coverage", params={"live": "true"})       # an <img> could do this
        assert r.status_code == 200 and r.json()["live_checked"] is None
        r = c.post("/api/coverage", params={"live": "true"})
        assert r.status_code == 200 and "not available" in r.json()["live_error"]


def test_a_large_body_is_refused_and_pages_cannot_be_framed(tmp_path):
    from fastapi.testclient import TestClient
    app = _app(tmp_path)
    with TestClient(app) as c:
        big = '{"ids": [' + ",".join(["1"] * 300_000) + "]}"   # ~600 KB
        r = c.post("/api/series", content=big, headers={"content-type": "application/json"})
        assert r.status_code == 413
        h = c.get("/").headers
        assert h["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in \
            h["content-security-policy"] and h["x-content-type-options"] == "nosniff"


def test_the_settings_page_cannot_open_the_gate():
    from hvactrend.config import is_settable
    for key in ("api.remote_viewer_only", "api.local_hosts", "api.allowed_hosts"):
        assert not is_settable(key), key


def test_a_discovery_waits_for_a_rescan(tmp_path):
    from fastapi.testclient import TestClient
    app = _app(tmp_path)
    with TestClient(app) as c:
        app.state.bacnet = object()
        app.state.rescan = {"state": "running", "device": 1}
        assert c.post("/api/discover").status_code == 409
        assert c.post("/api/devices/1/objects").status_code in (404, 409)


def test_report_generation_has_limits(tmp_path):
    from fastapi.testclient import TestClient
    app = _app(tmp_path)
    with TestClient(app) as c:
        assert c.post("/api/reports/generate", json={"kind": "daily"}).status_code == 409
    app = _app(tmp_path, "ai: {enabled: true}\n")
    with TestClient(app) as c:
        for body in ({"kind": "manual", "start": 0},                       # one-sided
                     {"kind": "manual", "start": 0, "end": 30 * 86400},    # 30 days
                     {"kind": "manual", "start": "0", "end": "86400"}):    # not numbers
            assert c.post("/api/reports/generate", json=body).status_code == 400, body


def test_a_prompt_far_past_any_real_one_is_not_sent(monkeypatch):
    from hvactrend.ai import reports, summary

    async def inputs(*a, **k):
        return {"items": {}}
    monkeypatch.setattr(reports, "_inputs", inputs)
    monkeypatch.setattr(summary, "render", lambda _i: "x" * (reports.PROMPT_MAX + 1))
    sent = []

    class _P:
        name = model = "fake"

        async def generate(self, *a, **k):
            sent.append(1)
    with pytest.raises(ValueError, match="not sent"):
        asyncio.run(reports.generate_report(None, None, "daily", provider=_P()))
    assert sent == []


def test_the_sdk_does_not_retry_inside_our_retries(monkeypatch):
    anthropic = pytest.importorskip("anthropic")
    from hvactrend.ai.provider import ClaudeProvider
    seen = {}
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **kw: seen.update(kw) or object())
    p = ClaudeProvider.__new__(ClaudeProvider)
    p.api_key, p._client = "k", None
    p._get_client()
    assert seen.get("max_retries") == 0


# ── the watchdog ────────────────────────────────────────────────────────────────────────
def _wd(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("wd_1106", REPO / "scripts" / "watchdog.py")
    wd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wd)
    monkeypatch.setattr(wd, "_FAILING_MARK", tmp_path / ".watchdog-failing")
    monkeypatch.setattr(wd.secrets_store, "load_into_env", lambda *a, **k: [])
    monkeypatch.setenv(wd.notify.ENV_VAR, "test-topic-not-real")
    posts = []
    # NEVER the network: this captures what would have gone to the phone
    monkeypatch.setattr(wd.notify, "post", lambda tgt, msg, **k: posts.append(msg) or (True, ""))
    return wd, posts


def test_a_broken_config_still_reaches_the_phone(tmp_path, monkeypatch):
    """One bad indent in config.yaml stopped every alarm and the check-in, silently."""
    wd, posts = _wd(monkeypatch, tmp_path)
    bad = tmp_path / "config.yaml"
    bad.write_text("site:\n  code: S\n name: oops\n", encoding="utf-8")   # a YAML error
    monkeypatch.setattr("sys.argv", ["watchdog.py", "--config", str(bad), "--quiet"])
    assert wd.main() == 2
    assert len(posts) == 1 and posts[0].priority == 5 and "cannot run" in posts[0].title
    assert wd.main() == 2 and len(posts) == 1            # once an hour, not every 5 minutes

    good = tmp_path / "good.yaml"
    db = tmp_path / "g.db"
    dal = SQLiteDAL(db)
    dal.apply_schema()
    dal.close()
    good.write_text(
        "site: {code: S, name: S}\n"
        "bacnet:\n  local_address: '<COLLECTOR_IP>/24'\n"
        "  device_instance: '<LOGGER_DEVICE_INSTANCE>'\n  poll: {interval_s: 120}\n"
        "  safety: {allow_writes: false, allow_cov: false}\n"
        f"storage: {{path: '{db.as_posix()}'}}\nalerts: {{checkin: false}}\n", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["watchdog.py", "--config", str(good), "--quiet"])
    wd.main()
    assert any("running again" in m.title for m in posts)
    assert not (tmp_path / ".watchdog-failing").exists()


def test_a_database_that_will_not_open_reaches_the_phone(tmp_path, monkeypatch):
    wd, posts = _wd(monkeypatch, tmp_path)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "site: {code: S, name: S}\n"
        "bacnet:\n  local_address: '<COLLECTOR_IP>/24'\n"
        "  device_instance: '<LOGGER_DEVICE_INSTANCE>'\n  poll: {interval_s: 120}\n"
        "  safety: {allow_writes: false, allow_cov: false}\n"
        f"storage: {{path: '{(tmp_path / 'no' / 'such' / 'dir' / 'x.db').as_posix()}'}}\n",
        encoding="utf-8")
    monkeypatch.setattr(wd, "SQLiteDAL", lambda *a, **k: (_ for _ in ()).throw(
        OSError("disk full")))
    monkeypatch.setattr("sys.argv", ["watchdog.py", "--config", str(cfg), "--quiet"])
    assert wd.main() == 2
    assert len(posts) == 1 and "database" in posts[0].lines[0]


def test_readings_in_the_future_do_not_hide_data_stopped(tmp_path, monkeypatch):
    wd, _posts = _wd(monkeypatch, tmp_path)
    from hvactrend.models import Quality, Sample
    dal = SQLiteDAL(tmp_path / "f.db")
    dal.apply_schema()
    dal._conn.execute("PRAGMA foreign_keys = OFF")     # samples with no inventory behind them
    now = int(time.time())
    dal.write_samples([Sample(point_id=1, ts_utc=now - 7200, value_num=1.0,
                              quality=Quality.GOOD),
                       Sample(point_id=1, ts_utc=now + 6 * 3600, value_num=1.0,
                              quality=Quality.GOOD)])          # a VM restore, an NTP jump
    res = wd.check(dal, None, now)
    kinds = {a["kind"] for a in dal.open_alerts()}
    assert {"collector-stale", "clock-ahead"} <= kinds, res
    dal.close()


# ── weather ─────────────────────────────────────────────────────────────────────────────
def test_the_tempest_token_never_reaches_an_error(monkeypatch):
    httpx = pytest.importorskip("httpx")
    from hvactrend.ai import weather
    token = "tok-SECRET-1234"
    monkeypatch.setenv("TEMPEST_TOKEN", token)
    cfg = {"weather": {"tempest": {"enabled": True, "mode": "cloud",
                                   "cloud": {"station_id": 42}}}}

    def http_401(url, params):
        req = httpx.Request("GET", url, params=params)
        raise httpx.HTTPStatusError("401 Unauthorized for url " + str(req.url), request=req,
                                    response=httpx.Response(401, request=req))
    res = weather.fetch_weather(cfg, http_get=http_401)
    assert res["error"].endswith("HTTP 401") and token not in res["error"]

    def http_other(url, params):
        raise RuntimeError(f"connect failed for {url}?token={params['token']}")
    res = weather.fetch_weather(cfg, http_get=http_other)
    assert token not in res["error"] and "***" in res["error"]


def test_the_weather_fetch_runs_off_the_loop(monkeypatch):
    from hvactrend.collector.service import Collector

    class _D:
        def upsert_weather(self, *a, **k):
            pass
    c = Collector(app=None, dal=_D(), config={"weather": {"refresh_interval_s": 3600}})
    where = []
    monkeypatch.setattr("hvactrend.ai.weather.fetch_weather",
                        lambda cfg: where.append(threading.get_ident()) or {"rows": []})
    loop_thread = []

    async def go():
        loop_thread.append(threading.get_ident())
        await c._maybe_refresh_weather()
    asyncio.run(go())
    assert where and where[0] != loop_thread[0]


# ── BACnet ──────────────────────────────────────────────────────────────────────────────
def test_a_port_already_taken_fails_loudly():
    from hvactrend.bacnet.application import BACnetConfigError, _assert_bindable
    held = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    held.bind(("127.0.0.1", 0))
    try:
        port = held.getsockname()[1]
        with pytest.raises(BACnetConfigError, match="in use"):
            _assert_bindable(f"127.0.0.1:{port}")
    finally:
        held.close()
    _assert_bindable(f"127.0.0.1:{port}")             # free again: fine


def test_infinite_readings_are_faults_not_numbers():
    from hvactrend.bacnet.discovery import _present_value
    from hvactrend.collector.service import _sample_from
    from hvactrend.models import Quality
    assert _present_value(float("inf")) == (None, "inf")
    assert _present_value(float("-inf")) == (None, "-inf")
    assert _present_value(float("nan")) == (None, "nan")
    assert _present_value(72.5) == (72.5, None)
    s = _sample_from({"point_id": 1}, {"present-value": float("inf")}, 0)
    assert s.value_num is None and s.quality == Quality.FAULT


def test_a_reply_that_will_not_decode_does_not_end_a_live_read():
    from hvactrend.api.app import _read_points_live

    class _B:
        async def read_property_multiple(self, address, params):
            if address == "a":
                raise ValueError("undecodable")
            return None                                       # not an RPM-ACK

        async def read_property(self, address, oid, prop):
            return 50.0
    targets = [{"id": 1, "address": "a", "object_type": "analog-input", "object_instance": 1},
               {"id": 2, "address": "b", "object_type": "analog-input", "object_instance": 2}]
    out = asyncio.run(_read_points_live(_B(), targets))
    assert set(out) == {1, 2}


# ── the Lows ────────────────────────────────────────────────────────────────────────────
def test_secrets_are_written_atomically_and_read_past_a_bom(tmp_path, monkeypatch):
    from hvactrend import secrets_store
    f = tmp_path / "secrets.env"
    monkeypatch.delenv("HVACTREND_NTFY_TOPIC", raising=False)
    secrets_store.set_secret("ntfy_topic", "hvac-abcd-efgh", path=f)
    assert not (tmp_path / "secrets.env.tmp").exists()
    f.write_bytes(b"\xef\xbb\xbf" + f.read_bytes())          # saved again in Notepad
    assert secrets_store._read_file(f) == {"HVACTREND_NTFY_TOPIC": "hvac-abcd-efgh"}
    # a value from the computer's own environment survives a clear of the file's
    monkeypatch.setenv("HVACTREND_NTFY_TOPIC", "from-the-machine")
    secrets_store.clear_secret("ntfy_topic", path=f)
    assert os.environ.get("HVACTREND_NTFY_TOPIC") == "from-the-machine"


def test_a_rediscovery_keeps_what_it_could_not_read(tmp_path):
    from hvactrend.models import Device
    dal = SQLiteDAL(tmp_path / "d.db")
    dal.apply_schema()
    dal._conn.execute("INSERT INTO sites(id, code, name) VALUES (1, 'S', 'S')")
    dal.upsert_device(Device(device_instance=7, site_id=1, name="SC-1", address="a",
                             vendor_name="Trane", model_name="SC+", is_router=True,
                             mac_address="m", router_instance=9))
    dal.upsert_device(Device(device_instance=7, site_id=1, name=None,
                             address="b"))                     # identity read failed
    r = dal._conn.execute("SELECT name, address, vendor_name, model_name, is_router, "
                          "mac_address, router_instance FROM devices").fetchone()
    assert tuple(r) == ("SC-1", "b", "Trane", "SC+", 1, "m", 9)
    dal.close()


def test_export_text_cannot_be_a_spreadsheet_formula(tmp_path):
    import csv
    import io
    from hvactrend.export import _SafeWriter
    f = io.StringIO()
    w = _SafeWriter(f)
    w.writerow(["=HYPERLINK(\"x\")", "+1", "@a", "-inf", "AHU-1 DAT", -3.5, 0])
    row = next(csv.reader(io.StringIO(f.getvalue())))
    assert row == ["'=HYPERLINK(\"x\")", "'+1", "'@a", "'-inf", "AHU-1 DAT", "-3.5", "0"]


def test_the_version_is_the_package_s():
    import hvactrend
    try:
        from importlib.metadata import version
        want = version("hvactrend")
    except Exception:  # noqa: BLE001 -- a source tree that was never installed
        want = "0+unknown"
    assert hvactrend.__version__ == want
