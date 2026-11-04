"""Between-report alerting tests — deterministic, hermetic (no LLM, no network).

Proves the continuous check raises high/critical conditions, tracks them (no duplicate
per cycle), and auto-resolves when the condition clears; plus the min_severity/enabled
gates and the API surface."""

from hvactrend.ai.alerts import evaluate_alerts
from hvactrend.models import Quality, Sample
from hvactrend.storage.sqlite_dal import SQLiteDAL


def _seed(db):
    dal = SQLiteDAL(db)
    dal.apply_schema()
    c = dal._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'DEMO','Demo Building')")
    c.execute("INSERT INTO buildings(id,site_id,code,name) VALUES (1,1,'MAIN','Main')")
    c.execute("INSERT INTO systems(id,building_id,code,name,kind) VALUES (1,1,'AC-1','AC-1','ahu')")
    c.execute("INSERT INTO devices(id,site_id,device_instance,address,created_utc) "
              "VALUES (1,1,100100,'192.0.2.19',0)")

    def pt(pid, ot, oi, nm):
        c.execute("INSERT INTO points(id,device_id,system_id,source,object_type,"
                  "object_instance,name,units_text,enabled,created_utc) "
                  "VALUES (?,1,1,'bacnet',?,?,?,'degrees-fahrenheit',1,0)", (pid, ot, oi, nm))

    pt(1, 'analog-input', 1, 'Mixed Air Temp')       # will be all-fault -> HIGH
    pt(2, 'analog-input', 2, 'Discharge Air Temp')   # healthy
    return dal


_CFG = {"alerts": {"enabled": True, "min_severity": "high", "window_s": 600}}


def test_evaluate_raises_tracks_and_resolves(tmp_path):
    dal = _seed(tmp_path / "a.db")
    dal.write_samples([Sample(point_id=1, ts_utc=1000 + i * 120, value_num=356.0,
                              quality=Quality.FAULT) for i in range(3)])
    dal.write_samples([Sample(point_id=2, ts_utc=1000 + i * 120, value_num=55.0 + i,
                              quality=Quality.GOOD) for i in range(3)])
    now = 1300
    r1 = evaluate_alerts(dal, _CFG, now, window_s=600)
    assert r1["raised"] == 1 and r1["open"] == 1        # the persistent MAT fault
    a = dal.open_alerts()[0]
    assert a["system_code"] == "AC-1" and a["severity"] in ("high", "critical")
    assert a["kind"] == "fault"

    # same condition next cycle -> tracked in place, NOT a second alert
    r2 = evaluate_alerts(dal, _CFG, now, window_s=600)
    assert r2["raised"] == 0 and r2["open"] == 1

    # advance now so the fault is outside the look-back window -> condition cleared,
    # the alert auto-resolves (the empty-window 'no data' is only MEDIUM, so it does
    # not itself alert at the high threshold)
    later = now + 10_000
    r3 = evaluate_alerts(dal, _CFG, later, window_s=600)
    assert r3["raised"] == 0 and r3["resolved"] == 1 and r3["open"] == 0
    assert dal.list_alerts(status="resolved")
    dal.close()


def test_min_severity_and_disabled_gates(tmp_path):
    dal = _seed(tmp_path / "b.db")
    dal.write_samples([Sample(point_id=1, ts_utc=1000 + i * 120, value_num=356.0,
                              quality=Quality.FAULT) for i in range(3)])
    # disabled -> no-op, nothing raised
    assert evaluate_alerts(dal, {"alerts": {"enabled": False}}, 1300)["enabled"] is False
    assert not dal.open_alerts()
    # min_severity=critical -> the HIGH fault does not clear the bar
    r = evaluate_alerts(dal, {"alerts": {"enabled": True, "min_severity": "critical",
                                         "window_s": 600}}, 1300, window_s=600)
    assert r["raised"] == 0 and r["open"] == 0
    dal.close()


def test_watchdog_raises_when_data_stops_and_resolves_when_it_returns(tmp_path):
    """The external watchdog is the ONLY thing that can catch a dead/asleep collector —
    a suspended process can't report itself."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "wd", str(__import__("pathlib").Path(__file__).resolve().parents[1] / "scripts" / "watchdog.py"))
    wd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wd)

    dal = _seed(tmp_path / "wd.db")
    cfg = {"alerts": {"stale_after_s": 900}}
    # newest sample is 2 h old -> collector considered stalled
    now = 2_000_000
    dal.write_samples([Sample(point_id=2, ts_utc=now - 7200, value_num=55.0,
                              quality=Quality.GOOD)])
    r1 = wd.check(dal, cfg, now)
    assert r1["raised"] == 1 and r1["problems"]
    a = [x for x in dal.open_alerts() if x["kind"] == "collector-stale"]
    assert a and a[0]["severity"] == "critical" and a[0]["source"] == "watchdog"

    # data starts flowing again -> the watchdog resolves its own alert
    dal.write_samples([Sample(point_id=2, ts_utc=now - 60, value_num=56.0,
                              quality=Quality.GOOD)])
    r2 = wd.check(dal, cfg, now)
    assert r2["resolved"] == 1
    assert not [x for x in dal.open_alerts() if x["kind"] == "collector-stale"]
    dal.close()


def test_prescreen_check_does_not_resolve_watchdog_alerts(tmp_path):
    """Source scoping: each evaluator only auto-resolves ITS OWN alerts, so the in-app
    pre-screen check can't clear a 'collector down' alert just because that condition
    isn't in its detector set."""
    from hvactrend.models import Alert
    dal = _seed(tmp_path / "src.db")
    dal.upsert_alert(Alert(kind="collector-stale", severity="critical",
                           message="collector down", fingerprint="collector-stale:-",
                           created_utc=1000, last_seen_utc=1000, source="watchdog"))
    assert len(dal.open_alerts()) == 1
    # the in-app check runs with NO active conditions -> must leave the watchdog alert alone
    evaluate_alerts(dal, _CFG, 1300, window_s=600)
    still = dal.open_alerts()
    assert len(still) == 1 and still[0]["source"] == "watchdog"
    dal.close()


def test_alerts_api_surface(tmp_path):
    import pytest
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    db = tmp_path / "api.db"
    _seed(db).close()
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "site: {code: DEMO, name: Demo Building}\n"
        "bacnet:\n"
        "  local_address: '<COLLECTOR_IP>/24'\n"
        "  device_instance: '<LOGGER_DEVICE_INSTANCE>'\n"
        "  poll: {interval_s: 120}\n"
        "  safety: {allow_writes: false, allow_cov: false}\n"
        "alerts: {enabled: true, min_severity: high, window_s: 600}\n"
        f"storage: {{path: '{str(db).replace(chr(92), '/')}'}}\n",
        encoding="utf-8",
    )
    # add a persistent fault within the last window (the endpoint checks now-window..now)
    import time
    now = int(time.time())
    dal = SQLiteDAL(db)
    dal.write_samples([Sample(point_id=1, ts_utc=now - 300 + i * 120, value_num=356.0,
                              quality=Quality.FAULT) for i in range(3)])
    dal.close()

    from hvactrend.api.app import create_app
    app = create_app(str(cfg), enable_bacnet=False)
    with TestClient(app) as client:
        # nothing yet
        assert client.get("/api/alerts").json()["open"] == []
        # run the check over a window covering the fault
        chk = client.post("/api/alerts/check").json()
        assert chk["raised"] >= 1
        opened = client.get("/api/alerts").json()["open"]
        assert opened and opened[0]["system_code"] == "AC-1"

def test_an_orphaned_prescreen_alert_is_not_shown_as_active(tmp_path):
    """2026-10-27: with the pre-screen off, its two rows left open on 09-15 were still
    "2 active alerts" on the reports page and counts on the dashboard. Every page goes
    through one helper; the watchdog's alerts still show."""
    import pytest
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from hvactrend.api.app import create_app
    from hvactrend.models import Alert

    db = tmp_path / "orphan.db"
    dal = _seed(db)
    dal.upsert_alert(Alert(kind="deviation", severity="high", system_code="AC-1",
                           message="old pre-screen row", fingerprint="deviation:AC-1:1",
                           created_utc=1000, last_seen_utc=1000, source="prescreen"))
    dal.upsert_alert(Alert(kind="device-poll-failed", severity="high", system_code="AC-1",
                           message="poll failed", fingerprint="device-poll-failed:1",
                           created_utc=2000, last_seen_utc=2000, source="watchdog"))
    dal.close()

    def client(enabled):
        cfg = tmp_path / f"cfg{int(enabled)}.yaml"
        cfg.write_text(
            "site: {code: DEMO, name: Demo Building}\n"
            "bacnet:\n"
            "  local_address: '<COLLECTOR_IP>/24'\n"
            "  device_instance: '<LOGGER_DEVICE_INSTANCE>'\n"
            "  poll: {interval_s: 120}\n"
            "  safety: {allow_writes: false, allow_cov: false}\n"
            f"alerts: {{enabled: {str(enabled).lower()}}}\n"
            f"storage: {{path: '{str(db).replace(chr(92), '/')}'}}\n", encoding="utf-8")
        return TestClient(create_app(str(cfg), enable_bacnet=False))

    with client(False) as c:
        assert [a["source"] for a in c.get("/api/alerts").json()["open"]] == ["watchdog"]
        assert c.get("/api/overview").json()["units"][0]["alerts"] == 1
        assert [a["source"] for a in c.get("/api/systems/1/overview").json()["alerts"]] \
            == ["watchdog"]
        assert len(c.get("/api/alerts").json()["recent"]) == 2      # the history keeps both
    with client(True) as c:                     # a pre-screen that runs is shown as before
        assert len(c.get("/api/alerts").json()["open"]) == 2
        assert c.get("/api/overview").json()["units"][0]["alerts"] == 2
