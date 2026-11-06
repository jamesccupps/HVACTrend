"""The AI summary: the model narrates; it does not detect.

All hermetic: a fake provider, no network, no key.
"""

import asyncio
import json

import pytest

from hvactrend import checkin
from hvactrend.ai import summary
from hvactrend.ai.provider import ProviderResult
from hvactrend.ai.reports import generate_report
from hvactrend.models import Quality, Report, Sample
from hvactrend.storage.sqlite_dal import SQLiteDAL

T0 = 1_792_000_000                      # a Tuesday morning, 2026-10-13
DAY = 86400


def _db(path):
    """AC-1 running all window with discharge air 3 °F over its setpoint, AC-9 on a vacant
    floor, one open alarm incident, one watchdog alert and one operator note."""
    dal = SQLiteDAL(path)
    dal.apply_schema()
    c = dal._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'DEMO','Demo Building')")
    c.execute("INSERT INTO buildings(id,site_id,code,name) VALUES (1,1,'MAIN','Main')")
    c.execute("INSERT INTO systems(id,building_id,code,name,kind) VALUES (1,1,'AC-1','AC-1','ahu')")
    c.execute("INSERT INTO systems(id,building_id,code,name,kind,occupancy_mode) "
              "VALUES (2,1,'AC-9','AC-9','ahu','vacant')")
    c.execute("INSERT INTO devices(id,site_id,device_instance,address,created_utc) "
              "VALUES (1,1,100100,'192.0.2.19',0)")
    for pid, sid, role, nm in ((1, 1, "FAN_SPEED", "SF VFD Mod|dac-3"),
                               (2, 1, "DAT", "Discharge Air Temp|dac-3"),
                               (3, 1, "DAT_ACTIVE_SP", "Discharge Air Temp Active SP|dac-3"),
                               (4, 1, "MAT", "Filter Status|dac-3"),     # set aside below
                               (5, 2, "FAN_SPEED", "SF VFD Mod|dac-1")):
        c.execute("INSERT INTO points(id,device_id,system_id,source,object_type,object_instance,"
                  "name,role,role_canonical,enabled,created_utc) "
                  "VALUES (?,1,?,'bacnet','analog-value',?,?,?,1,1,0)", (pid, sid, pid, nm, role))
    rows = []
    for t in range(T0 - DAY, T0 + 1, 120):
        rows += [Sample(point_id=1, ts_utc=t, value_num=80.0, quality=Quality.GOOD),
                 Sample(point_id=2, ts_utc=t, value_num=58.0, quality=Quality.GOOD),
                 Sample(point_id=3, ts_utc=t, value_num=55.0, quality=Quality.GOOD),
                 Sample(point_id=4, ts_utc=t, value_num=99.0, quality=Quality.GOOD),
                 Sample(point_id=5, ts_utc=t, value_num=0.0, quality=Quality.GOOD)]
    dal.write_samples(rows)
    c.execute("INSERT INTO incidents(id, rule, system_code, point_key, fingerprint, tier, "
              "status, title, detail, started_utc, opened_utc, updated_utc) VALUES "
              "(7,'sat-setpoint','AC-1','','sat-setpoint:AC-1:','action','open',"
              "'Discharge air not meeting its setpoint','AC-1: discharge air +3.0 °F',?,?,?)",
              (T0 - 3 * 3600, T0 - 3 * 3600, T0))
    c.execute("INSERT INTO alerts(id, kind, severity, message, fingerprint, source, status, "
              "created_utc, last_seen_utc, resolved_utc) VALUES (3,'device-stale','high',"
              "'SC-1 silent for 31 min','device-stale:100100','watchdog','resolved',?,?,?)",
              (T0 - 7200, T0 - 6000, T0 - 6000))
    dal.add_annotation(ts_utc=T0 - 5000, system_id=1, title="Belt replaced on AC-1")
    return dal


_KNOW = {"site_knowledge": {"ignore_points": [
    {"match": "Filter Status*", "systems": "AC-*", "why": "the filter switches lie"}]}}


def _action(refs, what="Check the AC-1 cooling", when="today"):
    return {"when": when, "unit": "AC-1", "refs": refs, "what": what, "why": "+3.0 °F"}


class _Fake:
    name, model = "fake", "fake-1"

    def __init__(self, actions, summary_text="All quiet but AC-1.", headline="AC-1 is warm."):
        self.data = {"headline": headline, "summary": summary_text, "actions": actions}
        self.prompts = []

    async def generate(self, prompt, *, system=None, schema=None):
        self.prompts.append((prompt, system, schema))
        return ProviderResult(data=self.data, provider=self.name, model=self.model,
                              call={"stop_reason": "end_turn", "usage": {"input_tokens": 9}})


def test_the_model_is_handed_what_the_detectors_found_each_under_an_id(tmp_path):
    dal = _db(tmp_path / "s.db")
    inp = summary.build_inputs(dal, _KNOW, "daily", T0 - DAY, T0)
    assert set(inp["items"]) == {"I7", "A3", "N1"}
    assert inp["items"]["I7"]["severity"] == "high"               # an ACTION incident
    text = summary.render(inp)
    # names and text from the field arrive quoted (2026-11-06 review, M2)
    assert '[I7] AC-1 · "Discharge air not meeting its setpoint" · ACTION' in text
    assert '[A3] device-stale · "SC-1 silent for 31 min' in text
    assert "[N1] AC-1 · " in text and "Belt replaced on AC-1" in text
    # the unit picture: the rules' own gate, so the hour of settling is not judged
    line = next(ln for ln in text.splitlines() if ln.startswith("AC-1: "))
    assert line.startswith("AC-1: ran 24.0 h; discharge air +3.0 °F from its setpoint")
    assert "within 2 °F 0% of that time" in line
    assert "AC-9: did not run (vacant floor)" in text
    # The operator's site knowledge goes in as "not faults", and a set-aside point is not read
    assert 'Points named "Filter Status*" on AC-* are set aside: "the filter switches lie"' in text
    assert "mixed air" not in line
    dal.close()


def test_an_alarm_that_already_cleared_ranks_low_whatever_tier_it_reached(tmp_path):
    """The first live summary (10-20) filed AC-3's 4-minute freeze alarm, over the day
    before, as a CRITICAL finding to monitor."""
    dal = _db(tmp_path / "r.db")
    dal._conn.execute(
        "INSERT INTO incidents(id, rule, system_code, point_key, fingerprint, tier, status, "
        "title, started_utc, opened_utc, resolved_utc, updated_utc) VALUES (8,'bas-alarm',"
        "'AC-3','3980','bas-alarm:AC-3:3980','critical','resolved','Controller alarm',?,?,?,?)",
        (T0 - 7200, T0 - 7200, T0 - 6960, T0 - 6960))
    items = summary.build_inputs(dal, _KNOW, "daily", T0 - DAY, T0)["items"]
    assert (items["I8"]["severity"], items["I7"]["severity"]) == ("low", "high")
    dal.close()


def test_an_action_must_cite_an_item_it_was_given():
    items = {"I7": {}, "O5280": {}}
    kept, dropped = summary.accept({"actions": [
        _action(["i7"]),                       # case does not matter
        _action(["[O5280]", "I7", "I7"]),      # brackets stripped, duplicates folded
        _action(["I#7"]),                      # "#" is not part of an id
        _action(["I99"]),                      # never handed over
        _action([]),
    ]}, items)
    assert [a["refs"] for a in kept] == [["I7"], ["O5280", "I7"], ["I7"]]
    assert len(dropped) == 2


def test_an_action_citing_nothing_found_is_dropped_and_counted(tmp_path):
    dal = _db(tmp_path / "d.db")
    prov = _Fake([_action(["I7"]), _action(["I99"], what="Replace the compressor")])
    res = asyncio.run(generate_report(dal, _KNOW, "daily", provider=prov, now_utc=T0))
    assert res["status"] == "complete" and res["finding_count"] == 1 and res["dropped"] == 1
    rep = dal.get_report(res["report_id"])
    assert rep["prose"].startswith("AC-1 is warm.\n\nAll quiet but AC-1.")
    assert "Replace the compressor" not in rep["prose"]
    assert "1 proposed action cited no detected item and were left out" in rep["prose"]
    call = json.loads(rep["call_json"])
    assert (call["kept_actions"], call["dropped_actions"], call["stop_reason"]) == (1, 1, "end_turn")
    assert json.loads(rep["inputs_json"])["items"] == {"incident": 1, "alert": 1, "note": 1}
    (f,) = dal.list_findings()
    assert (f["severity"], f["timeframe"], f["fingerprint"]) == \
        ("high", "immediate", "ai:sat-setpoint:AC-1:")
    # the fixed rules travel as the system prompt, whatever the editable template says
    prompt, system, schema = prov.prompts[0]
    assert "You do not look for new faults" in system and schema is summary.SCHEMA
    assert "[I7]" in prompt
    dal.close()


def test_the_inputs_are_built_off_the_event_loop_on_their_own_connection(tmp_path, monkeypatch):
    """The old context took 60 s for a week ON the loop the BACnet collector polls from
."""
    dal = _db(tmp_path / "t.db")
    seen = []
    real = summary.build_inputs

    def spy(d, *a):
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            on_loop = False
        seen.append((on_loop, d is dal))
        return real(d, *a)
    monkeypatch.setattr(summary, "build_inputs", spy)
    res = asyncio.run(generate_report(dal, _KNOW, "daily", provider=_Fake([_action(["I7"])]),
                                      now_utc=T0))
    # (not on the loop, not the collector's connection)
    assert seen == [(False, False)] and res["finding_count"] == 1
    dal.close()


def test_the_previous_summary_is_carried_forward_only_while_it_is_recent(tmp_path):
    dal = _db(tmp_path / "p.db")
    dal.insert_report(Report(kind="daily", period_start_utc=T0 - 2 * DAY, period_end_utc=T0 - DAY,
                             created_utc=T0 - DAY, provider="fake", model="m",
                             prose="Yesterday: AC-1 warm.", status="complete"))
    text = summary.render(summary.build_inputs(dal, _KNOW, "daily", T0 - DAY, T0))
    assert "Yesterday: AC-1 warm." in text
    text = summary.render(summary.build_inputs(dal, _KNOW, "daily", T0 + 2 * DAY, T0 + 3 * DAY))
    assert "THE PREVIOUS SUMMARY\nnone in the last two windows" in text
    dal.close()


def test_the_template_notes_are_for_the_operator_not_the_model(tmp_path):
    (tmp_path / "daily.md").write_text("<!-- a note for the operator -->\nWrite it.\n\n{context}\n",
                                       encoding="utf-8")
    cfg = {"ai": {"prompts": {"template_dir": str(tmp_path)}}}
    assert summary.load_template(cfg, "daily") == "Write it.\n\n{context}"
    assert summary.load_template(cfg, "manual") == "Write it.\n\n{context}"   # the daily one
    assert "{context}" in summary.load_template(cfg, "weekly")                # built-in


@pytest.mark.parametrize("report, fact, attention", [
    (None, "none in the last day", "No AI summary was written in the last day."),
    ("error", "failed", "The AI summary failed: AI provider error: credit balance is too low"),
    ("complete", "1 action, on the Reports page", None),
])
def test_the_morning_checkin_says_whether_the_summary_was_written(tmp_path, report, fact,
                                                                  attention):
    """In September the reports failed for days on an empty credit balance, and nothing
    said so."""
    dal = _db(tmp_path / "c.db")
    if report:
        rid = dal.insert_report(Report(
            kind="daily", period_start_utc=T0 - DAY, period_end_utc=T0, created_utc=T0,
            provider="claude", model="claude-opus-5-5", status=report,
            prose=("AI provider error: credit balance is too low" if report == "error"
                   else "fine")))
        if report == "complete":
            from hvactrend.models import Finding
            dal.upsert_finding(Finding(summary="x", created_utc=T0, report_id=rid,
                                       fingerprint="ai:x", severity="high", status="open",
                                       first_seen_utc=T0, last_seen_utc=T0))
    cfg = {"ai": {"enabled": True}, "bacnet": {"poll": {"interval_s": 120}}}
    s = checkin.build(dal, cfg, T0 + 3600, dests=[])
    facts = dict(s["facts"])
    assert fact in facts["AI summary"]
    if attention:
        assert attention in s["attention"]
    else:
        assert not any("AI summary" in a for a in s["attention"])
    # switched off, the check-in says nothing about it
    s = checkin.build(dal, {"ai": {"enabled": False}}, T0 + 3600, dests=[])
    assert "AI summary" not in dict(s["facts"])
    dal.close()


def test_the_frozen_health_grades_are_gone(tmp_path):
    """2026-10-20: "we dont need them anymore". The grades came from the old AI
    detection layer and had sat frozen at its last run (09-14) on every unit."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from hvactrend.api.app import create_app
    db = tmp_path / "g.db"
    _db(db).close()
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("site: {code: DEMO, name: Demo Building}\n"
                   "bacnet:\n  local_address: '<COLLECTOR_IP>/24'\n"
                   "  device_instance: '<LOGGER_DEVICE_INSTANCE>'\n"
                   "  poll: {interval_s: 120}\n"
                   "  safety: {allow_writes: false, allow_cov: false}\n"
                   f"storage: {{path: '{db.as_posix()}'}}\n", encoding="utf-8")
    with TestClient(create_app(str(cfg), enable_bacnet=False)) as c:
        units = c.get("/api/overview").json()["units"]
        assert units and not any("grade" in u or "score" in u for u in units)
        assert "health" not in c.get("/api/systems/1/overview").json()
        # the grades and the old pre-screen ran the old detectors on the collector's loop
        assert c.get("/api/health").status_code == 404
        assert c.get("/api/anomalies").status_code == 404
        for page in ("/dashboard", "/reports"):
            html = c.get(page).text
            assert "grade" not in html and "loadHealth" not in html, page
