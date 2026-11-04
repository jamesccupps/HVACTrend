"""Alert delivery to the phone (notify.py, via ntfy) and the daily check-in (checkin.py).

What these pin, in order of what would hurt most if it regressed:
  * the ntfy topic never escapes -- not in an error, a log detail, the API, or a snapshot;
    anyone holding it can read the alerts and post fake ones;
  * nothing is sent, or even recorded, until a topic is set;
  * the pre-screen's orphaned alerts are never sent (two sat open from 2026-09-15);
  * one failed poll cycle is held back; data-stopped goes at once, at top priority;
  * one all-clear per announced alert, none for flaps, and no replay of history;
  * a failed delivery is retried, not lost;
  * the check-in goes once a day after its time, and ALWAYS goes (it is the dead-man's
    switch), even when part of it cannot be read;
  * the watchdog does not load the collector or the AI layer.

No test here touches the network: an autouse fixture makes httpx.post raise, and
tests/conftest.py keeps the real secrets file out of every test.
"""

import importlib.util
import os
import re
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from hvactrend import archives, checkin, notify
from hvactrend.models import Alert, Quality, Sample
from hvactrend.storage.sqlite_dal import SQLiteDAL

REPO = Path(__file__).resolve().parents[1]
TOPIC = "occ-7kq2-m9xw-4tfh-p3vd-8ncr"
TGT = {"server": "https://ntfy.sh", "topic": TOPIC}
NO_CHECKIN = {"alerts": {"checkin": False}}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    import httpx

    def _refuse(*a, **k):
        raise AssertionError("a test tried to make a real HTTP request")
    monkeypatch.setattr(httpx, "post", _refuse)


class Wire:
    """Stands in for notify.post: records what would have gone to the phone."""

    def __init__(self):
        self.sent, self.ok, self.detail = [], True, "HTTP 200"

    def __call__(self, tgt, msg, *, timeout=10.0):
        self.sent.append((tgt, msg))
        return self.ok, self.detail


@pytest.fixture
def wire(monkeypatch):
    w = Wire()
    monkeypatch.setattr(notify, "post", w)
    return w


def _dal(tmp_path):
    d = SQLiteDAL(tmp_path / "n.db")
    d.apply_schema()
    c = d._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'DEMO','x')")
    for i, inst, name in ((1, 100400, "AC-4"), (2, 100200, "SC-2+")):
        c.execute("INSERT INTO devices(id,site_id,device_instance,name,address,created_utc) "
                  "VALUES (?,1,?,?,'192.0.2.9',0)", (i, inst, name))
        c.execute("INSERT INTO poll_health(device_id,ts_utc,ok,reads_attempted,reads_ok) "
                  "VALUES (?,1,1,1,1)", (i,))
    return d


def _alert(d, kind, sev, *, created, seen=None, source="watchdog", code=None, msg="m"):
    aid, _ = d.upsert_alert(Alert(
        kind=kind, severity=sev, message=msg, fingerprint=f"{kind}:{code or '-'}",
        created_utc=created, last_seen_utc=created if seen is None else seen,
        system_code=code, source=source))
    return aid


def _texts(msg):
    return msg.title + "\n" + msg.text()


def _local(y, mo, d, h, mi):
    return int(time.mktime((y, mo, d, h, mi, 0, 0, 0, -1)))


# ── the transport ────────────────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, code, text=""):
        self.status_code, self.text = code, text


def test_post_publishes_json_to_the_server_root(monkeypatch):
    """JSON to the root, not POST /<topic>: headers cannot carry an emoji title, and it keeps
    the topic out of the URL -- which is what exception texts echo."""
    import httpx
    seen = {}

    def fake(url, json=None, timeout=None):
        seen.update(url=url, json=json, timeout=timeout)
        return _Resp(200, "{}")
    monkeypatch.setattr(httpx, "post", fake)
    msg = notify.Message("\U0001f534 HVACTrend: Data has stopped", priority=5,
                         lines=["No new samples for 17 min"], footer="Tue 29 Sep 14:22")
    assert notify.post(TGT, msg) == (True, "HTTP 200")
    assert seen["url"] == "https://ntfy.sh/" and TOPIC not in seen["url"]
    assert seen["json"] == {"topic": TOPIC, "title": msg.title, "priority": 5,
                            "message": "No new samples for 17 min\n\nTue 29 Sep 14:22"}
    assert seen["timeout"] == 10.0


def test_the_topic_never_appears_in_an_error(monkeypatch):
    import httpx

    def boom(url, **k):
        raise httpx.ConnectError(f"could not publish to https://ntfy.sh/{TOPIC}")
    monkeypatch.setattr(httpx, "post", boom)
    ok, detail = notify.post(TGT, notify.Message("t"))
    assert not ok and detail.startswith("ConnectError") and TOPIC not in detail

    monkeypatch.setattr(httpx, "post", lambda url, **k: _Resp(
        403, f'{{"error": "topic {TOPIC} is reserved"}}'))
    ok, detail = notify.post(TGT, notify.Message("t"))
    assert not ok and detail.startswith("HTTP 403 (the topic is reserved")
    assert TOPIC not in detail


def test_post_refuses_a_plain_http_server_or_a_guessable_topic():
    # the autouse _no_network fixture would raise if either got as far as a request
    ok, detail = notify.post({"server": "http://ntfy.sh", "topic": TOPIC}, notify.Message("t"))
    assert not ok and "https" in detail
    ok, detail = notify.post({"server": "https://ntfy.sh", "topic": "hvac"}, notify.Message("t"))
    assert not ok and "12-64" in detail


def test_a_long_message_is_cut_to_what_ntfy_keeps_as_text():
    msg = notify.Message("t", lines=["x" * 5000])
    assert len(msg.text().encode("utf-8")) <= 3900


def test_no_topic_means_no_target(monkeypatch):
    assert notify.target({}) is None
    monkeypatch.setenv("HVACTREND_NTFY_TOPIC", TOPIC)
    assert notify.target({"alerts": {"ntfy_server": "https://ntfy.example.org/"}}) == {
        "server": "https://ntfy.example.org", "topic": TOPIC}


# ── what the watchdog sends ─────────────────────────────────────────────────────────
def test_nothing_is_sent_or_recorded_without_a_topic(tmp_path, wire):
    d = _dal(tmp_path)
    _alert(d, "collector-stale", "critical", created=1000)
    out = notify.run(d, NO_CHECKIN, 2000)
    assert out["configured"] is False and wire.sent == []
    assert d.meta_with_prefix("notify.") == {}


def test_the_prescreens_orphaned_alerts_are_never_sent(tmp_path, wire):
    d = _dal(tmp_path)
    _alert(d, "deviation", "high", source="prescreen", created=1000, code="AC-1",
           msg="AC-1 Mixed Air Temp: last 186.11 is +5.7sigma")
    _alert(d, "collector-stale", "critical", created=1000, msg="No new samples for 17 min")
    out = notify.run(d, NO_CHECKIN, 2000, tgt=TGT)
    assert out["alerts"] == 1 and len(wire.sent) == 1 and wire.sent[0][0] == TGT
    text = _texts(wire.sent[0][1])
    assert "Data has stopped" in text and "No new samples for 17 min" in text
    assert "Mixed Air" not in text
    assert [a for a in d.open_alerts() if a["source"] == "prescreen"][0]["notified_utc"] is None


def test_data_stopped_goes_at_once_at_top_priority(tmp_path, wire):
    d = _dal(tmp_path)
    _alert(d, "collector-stale", "critical", created=1000)
    notify.run(d, NO_CHECKIN, 1000, tgt=TGT)
    msg = wire.sent[0][1]
    assert msg.priority == 5 and msg.title.startswith("\U0001f534")


def test_one_failed_poll_cycle_waits_for_the_next_run(tmp_path, wire):
    d = _dal(tmp_path)
    aid = _alert(d, "device-poll-failed", "high", created=1000, code="100400",
                 msg="AC-4: last poll cycle FAILED (0/45 reads ok).")
    notify.run(d, NO_CHECKIN, 1000, tgt=TGT)
    assert wire.sent == []                                   # seen once: held back
    d._conn.execute("UPDATE alerts SET last_seen_utc=1300 WHERE id=?", (aid,))
    notify.run(d, NO_CHECKIN, 1300, tgt=TGT)                 # still failing 5 min later
    assert len(wire.sent) == 1
    assert "AC-4: poll cycles failing" in _texts(wire.sent[0][1])
    assert wire.sent[0][1].priority == 4


def test_one_all_clear_per_announced_alert_and_none_for_a_flap(tmp_path, wire):
    d = _dal(tmp_path)
    notify.run(d, NO_CHECKIN, 500, tgt=TGT)                  # first run sets the watermark
    _alert(d, "device-stale", "high", created=1000, code="100200",
           msg="SC-2+: no poll result for 31 min")
    _alert(d, "device-poll-failed", "high", created=1000, code="100400")   # never persists
    notify.run(d, NO_CHECKIN, 1000, tgt=TGT)
    assert len(wire.sent) == 1                               # only the device-stale
    d.resolve_missing_alerts([], 1600, source="watchdog")    # both clear
    notify.run(d, NO_CHECKIN, 1600, tgt=TGT)
    assert len(wire.sent) == 2
    text = _texts(wire.sent[1][1])
    assert "SC-2+ is responding again" in text and "10 min" in text
    assert "polling normally" not in text                    # no alarm went out, so no all-clear
    notify.run(d, NO_CHECKIN, 1900, tgt=TGT)
    assert len(wire.sent) == 2                               # once only


def test_switching_delivery_on_does_not_replay_history(tmp_path, wire):
    d = _dal(tmp_path)
    aid = _alert(d, "collector-stale", "critical", created=100)
    d.mark_alerts_notified([aid], 100)
    d.resolve_missing_alerts([], 200, source="watchdog")
    notify.run(d, NO_CHECKIN, 5000, tgt=TGT)
    assert wire.sent == []


def test_a_failed_delivery_is_retried_and_recorded_without_the_topic(tmp_path, wire):
    d = _dal(tmp_path)
    _alert(d, "collector-stale", "critical", created=1000)
    wire.ok, wire.detail = False, "HTTP 429 (rate-limited by ntfy; will retry)"
    out = notify.run(d, NO_CHECKIN, 1000, tgt=TGT)
    assert out["errors"] and d.alerts_needing_notification(source="watchdog")
    st = notify.status(d)
    assert st["last_error_utc"] == 1000 and "429" in st["last_error"]
    assert st["last_ok_utc"] is None
    wire.ok, wire.detail = True, "HTTP 200"
    notify.run(d, NO_CHECKIN, 1300, tgt=TGT)
    assert len(wire.sent) == 2 and not d.alerts_needing_notification(source="watchdog")
    assert notify.status(d)["last_ok_kind"] == "alert"
    assert TOPIC not in str(notify.status(d))


# ── the daily check-in: when ─────────────────────────────────────────────────────────
def _fake_build(dal, config, now_utc, *, dests=None):
    return {"day": "Mon 28 Sep", "state": "ok", "attention": [], "facts": [("k", "v")],
            "built_utc": now_utc}


def test_the_checkin_goes_once_a_day_after_its_time(tmp_path, wire, monkeypatch):
    d = _dal(tmp_path)
    monkeypatch.setattr(checkin, "build", _fake_build)
    cfg = {"alerts": {"checkin": True, "checkin_time": "07:00"}}
    notify.run(d, cfg, _local(2026, 9, 29, 6, 59), tgt=TGT)
    assert wire.sent == []
    notify.run(d, cfg, _local(2026, 9, 29, 7, 1), tgt=TGT)
    assert len(wire.sent) == 1 and "check-in: all normal" in wire.sent[0][1].title
    assert wire.sent[0][1].priority == 2                      # a normal morning is quiet
    notify.run(d, cfg, _local(2026, 9, 29, 7, 6), tgt=TGT)
    notify.run(d, cfg, _local(2026, 9, 29, 23, 59), tgt=TGT)
    assert len(wire.sent) == 1
    notify.run(d, cfg, _local(2026, 9, 30, 7, 0), tgt=TGT)
    assert len(wire.sent) == 2


def test_a_failed_checkin_is_retried_on_the_next_run(tmp_path, wire, monkeypatch):
    d = _dal(tmp_path)
    monkeypatch.setattr(checkin, "build", _fake_build)
    cfg = {"alerts": {"checkin": True, "checkin_time": "07:00"}}
    wire.ok = False
    notify.run(d, cfg, _local(2026, 9, 29, 7, 1), tgt=TGT)
    wire.ok = True
    notify.run(d, cfg, _local(2026, 9, 29, 7, 6), tgt=TGT)
    notify.run(d, cfg, _local(2026, 9, 29, 7, 11), tgt=TGT)
    assert len(wire.sent) == 2                                # the failure, then one success


def test_the_checkin_goes_out_even_when_part_of_it_cannot_be_read(tmp_path, wire, monkeypatch):
    d = _dal(tmp_path)

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(d, "poll_health_between", locked)
    cfg = {"alerts": {"checkin": True, "checkin_time": "00:00"},
           "storage": {"path": str(tmp_path / "n.db")}}
    out = notify.run(d, cfg, int(time.time()), tgt=TGT, dests=[])
    assert out["checkin"] is not None and len(wire.sent) == 1
    assert "could not be read (OperationalError)" in _texts(wire.sent[0][1])


# ── the daily check-in: what it says ───────────────────────────────────────────────────
def _world(tmp_path, *, now, cycles=None, gap_s=0, dev2_cycles=None, dev1_read_share=1.0,
           newest_age=600, backup_age=2 * 86400, snap_age=2 * 3600):
    """A day of polling for two controllers, plus archives on disk, relative to `now`."""
    d = _dal(tmp_path)
    c = d._conn
    c.execute("DELETE FROM poll_health")
    for pid, dev in ((1, 1), (2, 2)):
        c.execute("INSERT INTO points(id,device_id,object_type,object_instance,name,enabled,"
                  "created_utc) VALUES (?,?,'analog-input',?,?,1,0)", (pid, dev, pid, f"P{pid}"))
    t0, t1 = checkin.yesterday(now)
    n = cycles or round((t1 - t0) / 120)
    rows, samples = [], []
    for dev in (1, 2):
        for i in range(dev2_cycles if dev == 2 and dev2_cycles else n):
            ts = t0 + 30 + i * 120 + (gap_s if i >= n // 2 else 0)
            if ts >= t1:
                break
            ok_reads = 0 if (dev == 1 and i % 10 >= dev1_read_share * 10) else 1
            rows.append((dev, ts, 1, 1, ok_reads))
            if ok_reads:
                samples.append(Sample(point_id=dev, ts_utc=ts, value_num=1.0,
                                      quality=Quality.GOOD))
    c.executemany("INSERT INTO poll_health(device_id,ts_utc,ok,reads_attempted,reads_ok) "
                  "VALUES (?,?,?,?,?)", rows)
    samples.append(Sample(point_id=1, ts_utc=now - newest_age, value_num=1.0,
                          quality=Quality.GOOD))
    d.write_samples(samples)
    d.build_rollups(now)
    c.execute("INSERT INTO weather(ts_utc,oat_f,source,created_utc) VALUES (?,60,'tempest',?)",
              (now - 3600, now - 1800))

    bdir, snaps = tmp_path / "backups", tmp_path / "snaps"
    bdir.mkdir()
    snaps.mkdir()
    b = bdir / "hvactrend.20260927_155228.db"
    b.write_bytes(b"x" * 1000)
    os.utime(b, (now - backup_age, now - backup_age))
    off = tmp_path / "OneDrive" / "HVACTrend_db_backups"      # its off-site copy
    archives.make_offsite_copy(b, off, staging_dir=bdir / ".offsite_staging")
    s = snaps / "HVACTrend_snapshot_2026-09-29_050000.zip"
    s.write_bytes(b"x")
    os.utime(s, (now - snap_age, now - snap_age))
    cfg = {"bacnet": {"poll": {"interval_s": 120}},
           "alerts": {"stale_after_s": 900},
           "storage": {"path": str(tmp_path / "n.db"), "snapshot_interval_s": 86400,
                       "backup": {"dir": str(bdir), "interval_s": 604800,
                                  "min_free_bytes": 1, "offsite_dir": str(off)}}}
    return d, cfg, [snaps], n


def test_a_normal_day_reads_as_all_normal(tmp_path):
    now = _local(2026, 9, 29, 7, 2)
    d, cfg, dests, n = _world(tmp_path, now=now)
    s = checkin.build(d, cfg, now, dests=dests)
    facts = dict(s["facts"])
    assert s["state"] == "ok" and s["attention"] == [], s["attention"]
    assert s["day"] == "Mon 28 Sep"
    assert facts["Poll cycles"] == f"{n} of {n} on all 2 controllers"
    assert facts["Reads answered"] == "100 %"
    assert facts["Readings stored"] == f"{2 * n:,}"
    assert facts["Longest gap"] == "2 min"
    assert facts["Data now"].startswith("flowing")
    assert "next due" in facts["Database backup"]
    assert facts["Open alerts"] == "none"
    assert facts["Off-site copy"].endswith("in OneDrive")
    msg = notify.checkin_message(s)
    assert "all normal" in msg.title and msg.priority == 2
    assert "Poll cycles: " in msg.text() and "Yesterday, Mon 28 Sep" in msg.text()


def test_a_controller_added_yesterday_is_judged_from_its_first_poll(tmp_path):
    """The boiler panel was added at 15:38 on 09-29, and the next check-in reported it as
    "251 of 720 poll cycles". A controller that did not exist yet owes nothing; one that
    existed and went quiet for the morning still does."""
    now = _local(2026, 9, 29, 7, 2)
    d, cfg, dests, n = _world(tmp_path, now=now)
    t0, _ = checkin.yesterday(now)
    d._conn.execute("DELETE FROM poll_health WHERE device_id = 2 AND ts_utc < ?", (t0 + 12 * 3600,))
    s = checkin.build(d, cfg, now, dests=dests)
    assert not any("SC-2+" in a for a in s["attention"]), s["attention"]
    assert "SC-2+, polled since 12:" in dict(s["facts"])["New controllers"]
    d._conn.execute("INSERT INTO poll_health(device_id,ts_utc,ok,reads_attempted,reads_ok) "
                    "VALUES (2, ?, 1, 1, 1)", (t0 - 3600,))          # it existed the day before
    s = checkin.build(d, cfg, now, dests=dests)
    assert any(a.startswith("SC-2+:") and "poll cycles" in a for a in s["attention"])


def test_a_bad_day_names_each_problem(tmp_path):
    now = _local(2026, 9, 29, 7, 2)
    d, cfg, dests, n = _world(tmp_path, now=now, gap_s=1800, dev2_cycles=600,
                              dev1_read_share=0.9, backup_age=9 * 86400,
                              snap_age=3 * 86400)
    s = checkin.build(d, cfg, now, dests=dests)
    got = "\n".join(s["attention"])
    assert s["state"] == "attention"
    assert "SC-2+: 600 of" in got                                  # short on cycles
    assert re.search(r"AC-4: 90\.\d% of reads answered", got)      # failing reads
    assert re.search(r"\b32 min hole in the record at \d\d:\d\d", got)   # 30 min + a cycle
    assert "database backup is overdue" in got
    assert "local snapshot is 3 days old" in got
    msg = notify.checkin_message(s)
    assert "to look at" in msg.title and msg.priority == 4


def test_stopped_data_is_the_headline(tmp_path):
    now = _local(2026, 9, 29, 7, 2)
    d, cfg, dests, _ = _world(tmp_path, now=now, newest_age=2 * 3600)
    s = checkin.build(d, cfg, now, dests=dests)
    assert s["state"] == "critical"
    assert s["attention"][0].startswith("Data has stopped")
    msg = notify.checkin_message(s)
    assert "data has stopped" in msg.title and msg.priority == 5


# ── independence, the API, and the secret ─────────────────────────────────────────────
def test_the_watchdog_does_not_load_the_collector_or_the_ai_layer():
    """If it did, a broken collector deploy would also silence the report that it broke."""
    code = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('wd', r'{REPO / 'scripts' / 'watchdog.py'}')\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "import hvactrend.checkin\n"
        "print([n for n in sys.modules if n.startswith(('hvactrend.collector', "
        "'hvactrend.ai', 'bacpypes3'))])\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO / "src")},
                       timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "[]"


def _app(tmp_path):
    db = tmp_path / "app.db"
    SQLiteDAL(db).apply_schema()
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(f"""site: {{code: DEMO, name: Demo Building}}
bacnet:
  local_address: '<COLLECTOR_IP>/24'
  device_instance: '<LOGGER_DEVICE_INSTANCE>'
  poll: {{interval_s: 120}}
  safety: {{allow_writes: false, allow_cov: false}}
storage: {{path: '{str(db).replace(chr(92), chr(47))}'}}
alerts: {{checkin_time: '06:30'}}
""", encoding="utf-8")
    from hvactrend.api.app import create_app
    return create_app(str(cfg), enable_bacnet=False)


def test_the_test_button_route(tmp_path, monkeypatch, wire):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/notify/test")
        assert r.status_code == 400 and "No ntfy topic" in r.json()["detail"]
        assert wire.sent == []

        monkeypatch.setenv("HVACTREND_NTFY_TOPIC", TOPIC)
        r = c.post("/api/notify/test")
        assert r.status_code == 200 and r.json()["ok"] is True
        assert len(wire.sent) == 1 and wire.sent[0][0] == TGT
        assert "test message" in wire.sent[0][1].title
        assert TOPIC not in r.text

        s = c.get("/api/settings")
        n = s.json()["notify"]
        assert n["configured"] is True and n["last_ok_kind"] == "test"
        assert n["channel"] == "ntfy" and n["checkin_time"] == "06:30"   # from config
        assert s.json()["secrets"]["ntfy_topic"]["last4"] == TOPIC[-4:]
        assert TOPIC not in s.text

    with TestClient(app, client=("192.0.2.77", 51234)) as rc:   # a tech on the VLAN
        assert rc.post("/api/notify/test").status_code == 404
    assert len(wire.sent) == 1


def test_the_snapshot_guard_catches_the_topic(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("snap_t", REPO / "scripts" / "snapshot.py")
    snap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(snap)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "secrets.env").write_text(f"HVACTREND_NTFY_TOPIC={TOPIC}\n",
                                                     encoding="utf-8")
    monkeypatch.setattr(snap, "REPO", tmp_path)
    secrets = snap._secret_values()
    assert TOPIC in secrets

    z = tmp_path / "leak.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for req in snap.REQUIRED:
            zf.writestr("HVACTrend/" + req, "x" * 60_000)
        zf.writestr("HVACTrend/logs/watchdog.txt", f"publishing to {TOPIC}")
    with pytest.raises(RuntimeError, match="secret VALUE"):
        snap._verify(z, secrets)
