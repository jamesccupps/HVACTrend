"""A poll every minute.

The collector polls every 2 minutes by default, and several things had the 2 minutes written into
them instead of reading bacnet.poll.interval_s: the CSV export's cycle grouping, its label and
file name, the series row budget, the viewer's delta tolerance and export estimate, and the
schedule detector's day-coverage floor. Two that did read the rate judged a whole day by it,
so the day of a change read as every controller short of its poll cycles. Each test here fails
on the code before the change."""

import time

import pytest

from hvactrend import export, pollrate
from hvactrend.ai import summary
from hvactrend.models import PollHealth, Quality, Sample
from hvactrend.storage.sqlite_dal import SQLiteDAL

D = int(time.mktime((2026, 11, 10, 0, 0, 0, 0, 0, -1)))
CFG60 = {"bacnet": {"poll": {"interval_s": 60}}}


def _dal(path):
    d = SQLiteDAL(path)
    d.apply_schema()
    c = d._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'S','S')")
    c.execute("INSERT INTO devices(id,site_id,device_instance,name,address,created_utc) "
              "VALUES (1,1,1001,'Tower panel','192.0.2.9',0)")
    for pid in (1, 2):
        c.execute("INSERT INTO points(id,device_id,source,object_type,object_instance,name,"
                  "enabled,created_utc) VALUES (?,1,'bacnet','analog-input',?,?,1,0)",
                  (pid, pid, f"P{pid}"))
    return d


# ── the rate, and when it changed ──────────────────────────────────────────────────────
def test_the_rate_is_noted_and_a_change_is_stamped(tmp_path):
    d = _dal(tmp_path / "r.db")
    assert pollrate.note(d, 120, 1000) is None
    assert d.get_meta(pollrate.META_RATE) == "120"
    assert pollrate.changed_at(d) is None, "a first note cannot know what came before"
    assert pollrate.note(d, 120, 2000) is None                 # a restart at the same rate
    assert pollrate.changed_at(d) is None
    assert pollrate.note(d, 60, 3000) == 120.0                 # the switch
    assert (d.get_meta(pollrate.META_RATE), pollrate.changed_at(d)) == ("60", 3000)
    assert pollrate.note(d, 60, 4000) is None                  # restarted again: still 3000
    assert pollrate.changed_at(d) == 3000
    d.close()


def test_counts_start_at_a_change_only_inside_the_window(tmp_path):
    d = _dal(tmp_path / "w.db")
    pollrate.note(d, 120, 0)
    pollrate.note(d, 60, 5000)
    assert pollrate.counted_from(d, 1000, 9000) == 5000
    assert pollrate.counted_from(d, 6000, 9000) == 6000        # changed before the window
    assert pollrate.counted_from(d, 1000, 4000) == 1000        # changes after it
    d.close()


def test_the_rate_from_config_and_its_name():
    assert pollrate.interval(CFG60) == 60
    assert pollrate.interval(None) == 120
    assert pollrate.interval({"bacnet": {"poll": {}}}) == 120
    assert pollrate.interval({"bacnet": {"poll": {"interval_s": 0}}}) == 120
    assert [pollrate.label(s) for s in (60, 120, 30, 90)] == ["1-min", "2-min", "30-s", "90-s"]


# ── the day of the change ──────────────────────────────────────────────────────────────
def _changed_day(d, since):
    """Every 2 minutes from midnight to `since`, every minute after it."""
    ts = list(range(D + 30, since, 120)) + list(range(since, D + 86400, 60))
    for t in ts:
        d.record_poll_health(PollHealth(device_id=1, ts_utc=t, ok=True, latency_ms=300,
                                        reads_attempted=2, reads_ok=2, method="rpm"))
    pollrate.note(d, 120, D - 86400)
    pollrate.note(d, 60, since)
    return (D + 86400 - since) // 60


def test_the_summary_counts_the_cycles_from_the_change(tmp_path):
    d = _dal(tmp_path / "s.db")
    since = D + 15 * 3600
    m = _changed_day(d, since)
    out = summary._data_health(d, CFG60, D, D + 86400)
    at = time.strftime("%a %H:%M", time.localtime(since))
    assert out[0] == (f"poll cycles recorded since the poll rate changed to every 1-min at "
                      f"{at}: {m} of about {m}")
    # a window after the change is counted whole
    assert summary._data_health(d, CFG60, since + 3600, since + 7200)[0] == \
        "poll cycles recorded: 60 of about 60"
    d.close()


# ── the CSV export ─────────────────────────────────────────────────────────────────────
def _minute_db(path):
    """An hour at one minute. At 10 minutes P1 answers and P2 does not; a minute later (59 s,
    as a cycle that started a second early stamps it) P2 answers and P1 does not."""
    d = _dal(path)
    rows = []
    for k in range(60):
        t = D + k * 60 - (1 if k == 11 else 0)
        for pid in (1, 2):
            if (k, pid) in ((10, 2), (11, 1)):
                continue
            rows.append(Sample(point_id=pid, ts_utc=t, value_num=float(k), quality=Quality.GOOD))
    d.write_samples(rows)
    return d


def test_a_one_minute_export_keeps_every_cycle_on_its_own_row(tmp_path):
    d = _minute_db(tmp_path / "e.db")
    metas = [d.point_meta(1), d.point_meta(2)]
    rows = list(export.day_rows(d, metas, D, D + 3600, "raw", 60))
    # the old 60 s tolerance put P2's 10:59 reading on the 10:00 row, a minute early
    assert len(rows) == 60
    ten = next(r for r in rows if r[1] == D + 600)
    assert ten[4:8] == [10.0, "good", "", ""]
    assert rows[0][3] == "1-min polls"
    d.close()


def test_a_one_minute_export_is_named_and_sized_for_it(tmp_path):
    _minute_db(tmp_path / "n.db").close()
    info = export.write_export(str(tmp_path / "n.db"), [1, 2], D, D + 3600, "raw", "one",
                               str(tmp_path / "x.csv"), poll_s=60)
    assert info["filename"] == "hvactrend_export_2026-11-10_1min.csv"
    assert info["rows"] == 60
    assert export.estimate_cells(2, D, D + 86400, "raw", 60) == 2 * 1440
    assert export.estimate_cells(2, D, D + 86400, "5m", 60) == 2 * 288


# ── the series row budget ──────────────────────────────────────────────────────────────
def test_the_row_budget_counts_a_reading_a_minute():
    from hvactrend.api.app import _bounded_rollup
    six_days = 6 * 86400              # 64 points: 276k rows at 2 min, 553k at 1 (budget 400k)
    assert _bounded_rollup(None, 64, six_days) is None
    assert _bounded_rollup(None, 64, six_days, 60) == "5m"
    assert _bounded_rollup("raw", 64, six_days, 60) == "5m"
    assert _bounded_rollup(None, 64, 3 * 86400, 60) is None


def test_the_viewer_is_told_the_rate(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from hvactrend.api.app import create_app
    _dal(tmp_path / "a.db").close()
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "site: {code: S, name: S}\n"
        "bacnet:\n  local_address: '<COLLECTOR_IP>/24'\n"
        "  device_instance: '<LOGGER_DEVICE_INSTANCE>'\n  poll: {interval_s: 60}\n"
        "  safety: {allow_writes: false, allow_cov: false}\n"
        f"storage: {{path: '{(tmp_path / 'a.db').as_posix()}'}}\n", encoding="utf-8")
    with TestClient(create_app(str(cfg), enable_bacnet=False)) as client:
        assert client.get("/api/logged").json()["poll_s"] == 60
