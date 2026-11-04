"""API auth-gate tests (api.auth.enabled + HVACTREND_UI_TOKEN).

Auth is being turned ON so the server can run unattended as SYSTEM. That makes two
things load-bearing and worth pinning down:
  * every DB-MUTATING endpoint actually refuses an unauthenticated caller, and
  * the gate FAILS CLOSED — auth enabled with no token configured must not fall back
    to "allow", which would be the worst possible outcome of a misconfiguration.
"""

import pytest

from hvactrend.models import Quality, Sample
from hvactrend.storage.sqlite_dal import SQLiteDAL


def _seed(db_path):
    dal = SQLiteDAL(db_path)
    dal.apply_schema()
    c = dal._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'DEMO','Demo Building')")
    c.execute("INSERT INTO buildings(id,site_id,code,name) VALUES (1,1,'MAIN','Main')")
    c.execute("INSERT INTO systems(id,building_id,code,name,kind) "
              "VALUES (1,1,'AC-1','AC-1','ahu')")
    c.execute("INSERT INTO devices(id,site_id,device_instance,address,created_utc) "
              "VALUES (1,1,100100,'192.0.2.19',0)")
    c.execute("INSERT INTO points(id,device_id,system_id,source,object_type,"
              "object_instance,name,units_text,enabled,created_utc) "
              "VALUES (1,1,1,'bacnet','analog-input',1,'Mixed Air Temp',"
              "'degrees-fahrenheit',1,0)")
    dal.write_samples([Sample(point_id=1, ts_utc=1000, value_num=55.0,
                              quality=Quality.GOOD)])
    return dal


def _cfg(tmp_path, db, *, auth: bool):
    p = tmp_path / "cfg.yaml"
    p.write_text(
        "site: {code: DEMO, name: Demo Building}\n"
        "bacnet:\n"
        "  local_address: '<COLLECTOR_IP>/24'\n"
        "  device_instance: '<LOGGER_DEVICE_INSTANCE>'\n"
        "  poll: {interval_s: 120}\n"
        "  safety: {allow_writes: false, allow_cov: false}\n"
        f"api: {{host: 127.0.0.1, port: 8000, auth: {{enabled: {str(auth).lower()}}}}}\n"
        f"storage: {{path: '{str(db).replace(chr(92), '/')}'}}\n",
        encoding="utf-8")
    return p


def _client(cfg_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from hvactrend.api.app import create_app
    return TestClient(create_app(str(cfg_path), enable_bacnet=False))


# every endpoint that WRITES to the DB, spends money, or discloses config/secrets —
# each must refuse an anonymous caller
MUTATORS = [
    ("post", "/api/points/1/enabled", {"enabled": False}),
    ("post", "/api/findings/1/status", {"status": "closed"}),
    ("put", "/api/systems/1/context", {"notes": "hi"}),
    ("put", "/api/systems/1/equipment", {"specs": {"tons": 40}}),
    ("put", "/api/settings", {"settings": {}}),
    ("get", "/api/settings", None),          # discloses config + secret last4
    ("post", "/api/backup", None),
    ("get", "/api/backups", None),
    ("post", "/api/alerts/check", None),
    ("post", "/api/model/rebuild", None),
    ("post", "/api/reports/generate", {"kind": "daily"}),   # billed LLM call
]

# Endpoints that put TRAFFIC ON THE PLANT NETWORK. They mutate nothing locally, which is
# why they were missed, but they are the only unauthenticated way to make this program
# talk to the BAS -- and device_objects on a 2000-object panel is a ~4 minute operation.
# /api/discover was gated from the start; these three were not.
PLANT_TRAFFIC = [
    ("get", "/api/points/1/read", None),
    ("post", "/api/points/read", {"ids": [1]}),
    ("get", "/api/devices/100100/objects", None),
    ("post", "/api/discover", None),
]


@pytest.fixture(autouse=True)
def _isolate_secrets(tmp_path, monkeypatch):
    """Never read (or write) the operator's real config/secrets.env from a test."""
    monkeypatch.setenv("HVACTREND_SECRETS", str(tmp_path / "secrets.env"))


def test_mutating_endpoints_require_a_token_when_auth_is_on(tmp_path, monkeypatch):
    monkeypatch.setenv("HVACTREND_UI_TOKEN", "s3cret-token")
    db = tmp_path / "auth.db"
    _seed(db).close()
    with _client(_cfg(tmp_path, db, auth=True)) as c:
        for method, url, body in MUTATORS:
            r = getattr(c, method)(url, json=body) if body is not None else \
                getattr(c, method)(url)
            assert r.status_code == 401, f"{method.upper()} {url} was NOT gated ({r.status_code})"
        # ...and the same calls succeed with the token
        ok = c.post("/api/points/1/enabled", json={"enabled": False},
                    headers={"Authorization": "Bearer s3cret-token"})
        assert ok.status_code == 200 and ok.json()["enabled"] is False
        ok2 = c.post("/api/findings/1/status", json={"status": "closed"},
                     headers={"X-Auth-Token": "s3cret-token"})
        assert ok2.status_code == 200


def test_auth_enabled_without_a_token_fails_CLOSED(tmp_path, monkeypatch):
    """A half-configured gate must never degrade to 'allow everyone'."""
    monkeypatch.delenv("HVACTREND_UI_TOKEN", raising=False)
    db = tmp_path / "noauth.db"
    _seed(db).close()
    with _client(_cfg(tmp_path, db, auth=True)) as c:
        r = c.post("/api/points/1/enabled", json={"enabled": False})
        assert r.status_code == 503            # explicitly misconfigured, NOT open
        assert c.get("/api/systems").status_code == 200   # read-only views still serve


def test_a_wrong_token_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HVACTREND_UI_TOKEN", "right-token")
    db = tmp_path / "wrong.db"
    _seed(db).close()
    with _client(_cfg(tmp_path, db, auth=True)) as c:
        r = c.post("/api/points/1/enabled", json={"enabled": False},
                   headers={"Authorization": "Bearer wrong-token"})
        assert r.status_code == 401


def test_auth_off_keeps_the_trusted_vlan_default(tmp_path, monkeypatch):
    """Default posture is unchanged: with api.auth off the UI works with no token."""
    monkeypatch.delenv("HVACTREND_UI_TOKEN", raising=False)
    db = tmp_path / "off.db"
    _seed(db).close()
    with _client(_cfg(tmp_path, db, auth=False)) as c:
        assert c.post("/api/points/1/enabled",
                      json={"enabled": False}).status_code == 200


def test_ui_token_is_a_managed_secret_readable_by_a_SYSTEM_task(tmp_path, monkeypatch):
    """The token has to survive in config/secrets.env: the server is moving to an
    unattended SYSTEM task, and a per-USER env var is invisible to SYSTEM."""
    from hvactrend import secrets_store
    path = tmp_path / "secrets.env"
    monkeypatch.delenv("HVACTREND_UI_TOKEN", raising=False)

    assert secrets_store.set_secret("ui_token", "abcd1234", path) == "HVACTREND_UI_TOKEN"
    monkeypatch.delenv("HVACTREND_UI_TOKEN", raising=False)   # simulate a fresh process
    assert "HVACTREND_UI_TOKEN" in secrets_store.load_into_env(path)
    import os
    assert os.environ["HVACTREND_UI_TOKEN"] == "abcd1234"

    st = secrets_store.status(path)["ui_token"]
    assert st["set"] is True and st["last4"] == "1234"
    assert "abcd1234" not in str(st)            # value itself is never disclosed

    # the allowlist still holds — the file can't inject arbitrary env vars
    with pytest.raises(KeyError):
        secrets_store.set_secret("PATH", "/evil", path)


def test_auth_cannot_be_disabled_through_the_settings_api(tmp_path):
    """Regression: the UI must never be able to switch its own gate off."""
    from hvactrend.config import is_settable
    for key in ("api.auth.enabled", "api.auth.token", "api.host", "api.port",
                "bacnet.safety.allow_writes"):
        assert is_settable(key) is False, key


def test_endpoints_that_touch_the_plant_network_require_a_token(tmp_path, monkeypatch):
    """These write nothing locally, so they are easy to overlook -- but they are the only
    unauthenticated way to generate load on the BAS. It matters at exactly one moment: the
    plan of record is to enable api.auth before opening the firewall, and until this test
    existed these three would have stayed open on the other side of it."""
    monkeypatch.setenv("HVACTREND_UI_TOKEN", "s3cret-token")
    db = tmp_path / "auth.db"
    _seed(db).close()
    with _client(_cfg(tmp_path, db, auth=True)) as c:
        for method, url, body in PLANT_TRAFFIC:
            r = getattr(c, method)(url, json=body) if body is not None else \
                getattr(c, method)(url)
            assert r.status_code == 401, \
                f"{method.upper()} {url} can reach the BAS unauthenticated ({r.status_code})"


def test_the_test_suite_never_writes_to_the_production_log(tmp_path, monkeypatch):
    """Constructing the app used to append to the REAL logs/hvactrend.log, including a
    fabricated `allow_writes=true` safety WARNING -- the single most alarming line this log
    can carry, and it never happened. Under pytest the file handler is not installed."""
    import logging
    from hvactrend.api.app import _setup_logging

    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        root.handlers = []
        _setup_logging({"logging": {"file": str(tmp_path / "should_not_exist.log")}})
        assert not (tmp_path / "should_not_exist.log").exists(), \
            "a test run must not open a log FILE"
        assert not any(getattr(h, "_occ", False) for h in root.handlers)
    finally:
        root.handlers = saved
