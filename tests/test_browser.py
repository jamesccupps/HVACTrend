"""Point-selection browser tests: DAL queries + API in browse-only mode."""

import pathlib

import pytest

from hvactrend.storage.sqlite_dal import SQLiteDAL


def _seed(db_path):
    dal = SQLiteDAL(db_path)
    dal.apply_schema()
    c = dal._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'DEMO','Demo Building')")
    c.execute("INSERT INTO buildings(id,site_id,code,name) VALUES (1,1,'MAIN','Main')")
    c.execute("INSERT INTO systems(id,building_id,code,name,kind) VALUES (1,1,'AC-1','AC-1','ahu')")
    c.execute("INSERT INTO devices(id,site_id,device_instance,address,created_utc) "
              "VALUES (1,1,100100,'192.0.2.19',0)")
    pts = [
        (1, 'analog-input', 4, 'Discharge Air Temperature|dac-3', 'degrees-fahrenheit'),
        (2, 'analog-input', 50, 'Return Air Temp|dac-3', 'degrees-fahrenheit'),
        (3, 'analog-value', 9, 'Discharge Air Setpoint|dac-3', 'degrees-fahrenheit'),
        (4, 'notification-class', 1, 'HVAC-Critical', None),   # not loggable
    ]
    for pid, ot, oi, nm, units in pts:
        c.execute("INSERT INTO points(id,device_id,system_id,source,object_type,"
                  "object_instance,name,units_text,enabled,created_utc) "
                  "VALUES (?,1,1,'bacnet',?,?,?,?,0,0)", (pid, ot, oi, nm, units))
    return dal


def test_dal_browser_queries(tmp_path):
    dal = _seed(tmp_path / "b.db")

    systems = dal.list_systems()
    assert len(systems) == 1
    assert systems[0]["code"] == "AC-1"
    assert systems[0]["point_count"] == 4
    assert systems[0]["enabled_count"] == 0

    # name search
    hits = dal.points_for_system(1, q="discharge")
    assert {p["name"] for p in hits} == {
        "Discharge Air Temperature|dac-3", "Discharge Air Setpoint|dac-3"}

    # loggable-only excludes the notification-class object
    log = dal.points_for_system(1, loggable_only=True)
    assert all(p["loggable"] for p in log)
    assert not any(p["object_type"] == "notification-class" for p in log)

    # select a point for logging -> enabled_count reflects it
    dal.set_point_enabled(1, True)
    assert dal.list_systems()[0]["enabled_count"] == 1
    dal.set_point_enabled(1, False)
    assert dal.list_systems()[0]["enabled_count"] == 0

    # operator context round-trip: notes + occupancy mode persist and only the
    # provided field updates
    assert dal.set_system_context(1, occupancy_mode="vacant") is True
    assert dal.set_system_context(1, notes="floor 8 duct issue") is True
    s = dal.list_systems()[0]
    assert s["occupancy_mode"] == "vacant" and s["notes"] == "floor 8 duct issue"
    assert dal.set_system_context(999, occupancy_mode="vacant") is False   # no such system
    dal.close()


def test_api_browse_only(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    db = tmp_path / "b.db"
    _seed(db).close()
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "site: {code: DEMO, name: Demo Building}\n"
        "bacnet:\n"
        "  local_address: '<COLLECTOR_IP>/24'\n"
        "  device_instance: '<LOGGER_DEVICE_INSTANCE>'\n"
        "  poll: {interval_s: 120}\n"
        "  safety: {allow_writes: false, allow_cov: false}\n"
        f"storage: {{path: '{str(db).replace(chr(92), '/')}'}}\n",
        encoding="utf-8",
    )

    from hvactrend.api.app import create_app
    app = create_app(str(cfg), enable_bacnet=False)
    with TestClient(app) as client:
        assert client.get("/health").json()["bacnet"] is False
        assert "<title>HVACTrend" in client.get("/").text

        systems = client.get("/api/systems").json()["systems"]
        assert systems[0]["code"] == "AC-1"
        sid = systems[0]["id"]

        pts = client.get(f"/api/systems/{sid}/points", params={"q": "discharge"}).json()
        assert pts["count"] == 2

        pid = pts["points"][0]["id"]
        assert client.post(f"/api/points/{pid}/enabled", json={"enabled": True}).json()["enabled"] is True
        assert client.get("/api/systems").json()["systems"][0]["enabled_count"] == 1

        # operator context: set a unit vacant + note -> surfaced back on /api/systems
        r = client.put(f"/api/systems/{sid}/context",
                       json={"occupancy_mode": "vacant", "notes": "9th floor vacant"})
        assert r.status_code == 200
        s0 = {s["id"]: s for s in client.get("/api/systems").json()["systems"]}[sid]
        assert s0["occupancy_mode"] == "vacant" and s0["notes"] == "9th floor vacant"
        # validation: bad mode -> 400, unknown system -> 404
        assert client.put(f"/api/systems/{sid}/context",
                          json={"occupancy_mode": "bogus"}).status_code == 400
        assert client.put("/api/systems/9999/context",
                          json={"occupancy_mode": "vacant"}).status_code == 404

        # live read needs BACnet -> 503 in browse-only mode
        assert client.post("/api/points/read", json={"ids": [pid]}).status_code == 503


def test_trend_series_api(tmp_path):
    """Phase 3 trend viewer: /api/logged, /api/series, /api/points/{id}/series
    read stored samples with no BACnet stack (browse-only)."""
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from hvactrend.models import Quality, Sample

    db = tmp_path / "t.db"
    dal = _seed(db)
    dal.set_point_enabled(1, True)          # temp point -> being trended
    dal.set_point_enabled(2, True)
    # point 1 gets samples (incl. a fault); point 2 stays empty (no data yet)
    dal.write_samples([
        Sample(point_id=1, ts_utc=1000, value_num=58.0, quality=Quality.GOOD),
        Sample(point_id=1, ts_utc=1120, value_num=59.5, quality=Quality.GOOD),
        Sample(point_id=1, ts_utc=1240, value_num=356.0, quality=Quality.FAULT),
    ])
    dal.close()

    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "site: {code: DEMO, name: Demo Building}\n"
        "bacnet:\n"
        "  local_address: '<COLLECTOR_IP>/24'\n"
        "  device_instance: '<LOGGER_DEVICE_INSTANCE>'\n"
        "  poll: {interval_s: 120}\n"
        "  safety: {allow_writes: false, allow_cov: false}\n"
        f"storage: {{path: '{str(db).replace(chr(92), '/')}'}}\n",
        encoding="utf-8",
    )

    from hvactrend.api.app import create_app
    app = create_app(str(cfg), enable_bacnet=False)
    with TestClient(app) as client:
        page = client.get("/trends").text
        assert "<title>HVACTrend" in page
        # The manual axis-scale controls are pure client-side JS with no build step, so the
        # only server-side guard against shipping a half-edited viewer is that the elements
        # the wiring binds to actually exist. Every id below is dereferenced unconditionally
        # at load ($('#x').onclick=...), so a typo here is an immediate TypeError that kills
        # the whole script — chart, picker and all — not a quietly missing button.
        for el in ("scaleBtn", "scaleMenu", "xPinFrom", "xPinTo", "xPinApply", "xPinClear",
                   "xPinValMin", "xPinValMax", "xPinValFill", "xPinHdr",
                   "xPinRowTime", "xPinRowVal",
                   "yPinMin", "yPinMax", "yPinApply", "yPinClear", "yPinFill", "scaleHint",
                   # Δ lines — same rule: every one of these is dereferenced at load
                   "deltaBtn", "deltaDlg", "dlgA", "dlgB", "dlgAdd", "dlgClose",
                   "dlgHint", "dlgList",
                   # time navigation: step arrows, live badge, calendar
                   "stepNav", "stepBack", "stepFwd", "stepNow", "calBtn", "calMenu",
                   "calDay", "calDayGo", "calFrom", "calTo", "calRangeGo", "calNote",
                   # search-first picker. Only #pclear is in the markup — #secSel,
                   # #secResults, #secRecent and #recClear are built by renderPicker(),
                   # so they cannot be asserted from the served page.
                   "psearch", "pclear",
                   # overview strip (whole-record mini-map)
                   "ovbox", "ov", "ovLabel"):
            assert f'id="{el}"' in page, f"trend viewer is missing #{el}"
        # A pinned axis must render the operator's literal numbers, so the unpadded twins of
        # scaleOf/axisNumbers have to be present — and the auto-mode originals must survive
        # alongside them rather than being refactored away underneath five other call sites.
        for fn in ("function scaleExact", "function axisNumbersExact", "function clipPlot",
                   "function yPinState", "function scaleOf", "function axisNumbers"):
            assert fn in page, f"trend viewer is missing {fn}"

        logged = client.get("/api/logged").json()
        ids = {p["id"]: p for p in logged["points"]}
        assert set(ids) == {1, 2}                      # only enabled points
        # sample_count is CHARTABLE samples (good + numeric), so the fault is excluded;
        # last_ts spans every quality, so it still reports the fault's ts as last-seen.
        assert ids[1]["sample_count"] == 2 and ids[1]["last_ts"] == 1240
        assert ids[2]["sample_count"] == 0 and ids[2]["last_ts"] is None
        assert logged["extent"]["min_ts"] == 1000 and logged["extent"]["max_ts"] == 1240

        # The viewer's range buttons ("6h" = the last 6 hours OF DATA) anchor to the newest
        # stored sample. That used to come only from /api/logged at page load and was never
        # refreshed, so a tab left open for hours anchored to a stale "now" and every range
        # button returned a window ending hours in the past. This endpoint is what lets the
        # viewer re-anchor on each click, so it has to stay cheap and stay in step with
        # /api/logged's extent.
        ext = client.get("/api/extent").json()["extent"]
        assert ext == {"min_ts": 1000, "max_ts": 1240}
        assert ext == logged["extent"], "/api/extent must agree with the picker's extent"

        one = client.get("/api/points/1/series",
                         params={"start": 0, "end": 2000}).json()
        assert one["rollup"] == "raw" and len(one["samples"]) == 3
        assert one["samples"][0] == {"t": 1000, "v": 58.0, "vt": None, "q": "good"}
        assert one["samples"][2]["q"] == "fault"          # fault carried to the viewer
        assert one["meta"]["units_text"] == "degrees-fahrenheit"

        batch = client.post("/api/series",
                            json={"ids": [1, 2], "start": 0, "end": 2000}).json()
        by = {s["point_id"]: s for s in batch["series"]}
        assert len(by[1]["samples"]) == 3 and by[2]["samples"] == []

        # range window clips to the requested span
        clipped = client.get("/api/points/1/series",
                             params={"start": 1100, "end": 1200}).json()
        assert [s["t"] for s in clipped["samples"]] == [1120]

        # a rollup request with no rollups built yet is bucketed from raw on the fly, so
        # the chart is never silently blank (rollup tables are empty here). It used to drop
        # to raw, outside the row budget (2026-10-27 review).
        rl = client.get("/api/points/1/series",
                        params={"start": 0, "end": 2000, "rollup": "1h"}).json()
        assert rl["rollup"] == "1h" and len(rl["samples"]) == 1
        rlb = client.post("/api/series",
                          json={"ids": [1], "start": 0, "end": 2000, "rollup": "5m"}).json()
        # 1000 and 1120 share the 900 bucket, 1240 is in the 1200 one
        assert rlb["series"][0]["rollup"] == "5m" and len(rlb["series"][0]["samples"]) == 2

        assert client.get("/api/points/999/series",
                          params={"start": 0, "end": 1}).status_code == 404


# ── remote (HVAC VLAN) sessions: the trend viewer and nothing else ───────────
def _viewer_app(tmp_path):
    db = tmp_path / "v.db"
    _seed(db).close()
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(f"""site: {{code: DEMO, name: Demo Building}}
bacnet:
  local_address: '<COLLECTOR_IP>/24'
  device_instance: '<LOGGER_DEVICE_INSTANCE>'
  poll: {{interval_s: 120}}
  safety: {{allow_writes: false, allow_cov: false}}
storage: {{path: '{str(db).replace(chr(92), chr(47))}'}}
""", encoding="utf-8")
    from hvactrend.api.app import create_app
    return create_app(str(cfg), enable_bacnet=False)


def _remote_client(app, host="192.0.2.77"):
    from fastapi.testclient import TestClient
    return TestClient(app, client=(host, 51234))


def test_remote_clients_get_the_trend_viewer_only(tmp_path):
    """A tech on the HVAC VLAN types the box's address and reads trends. They have no
    reason to see point selection, the unit dashboard, AI reports or settings.

    This is SCOPE, not a security boundary -- anyone on that VLAN can already speak BACnet
    straight to the controllers, which is far more access than this program has (it cannot
    write at all). Local requests are untouched."""
    pytest.importorskip("fastapi")
    app = _viewer_app(tmp_path)

    with _remote_client(app) as rc:
        # the viewer itself, and the five endpoints it actually calls
        r = rc.get("/trends")
        assert r.status_code == 200 and "<title>HVACTrend" in r.text
        assert 'class="viewer-only"' in r.text, "nav + rename must be hidden remotely"
        for url in ("/api/extent", "/api/logged"):
            assert rc.get(url).status_code == 200, url
        # Operator notes are DRAWN ON THE CHART, so a remote tech has to be able to read
        # them -- "CT-2 is down awaiting a fan shaft" is exactly the context that stops a
        # wide tower approach being read as a CT-1 fault. GET only.
        assert rc.get("/api/annotations").status_code == 200
        assert rc.get("/api/annotations?start=0&end=99").status_code == 200
        # any answer but 404 proves it passed the allowlist and reached the endpoint;
        # an empty id list is then rejected by the endpoint's own validation
        assert rc.post("/api/series", json={"ids": []}).status_code != 404

        # the bare address lands on the viewer rather than point selection
        r = rc.get("/", follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "/trends"

        # every other page and API is simply not there
        for url in ("/dashboard", "/reports", "/settings", "/api/settings",
                    "/api/systems", "/api/backups", "/api/anomalies", "/api/alerts",
                    # the coverage review can read ~100 points from the controllers
                    "/coverage", "/api/coverage", "/api/coverage?live=true"):
            assert rc.get(url).status_code == 404, f"{url} was reachable remotely"

        # writes are gone too -- display_name feeds the role classifier, so a visitor
        # renaming a point would change what the analysis measures
        assert rc.post("/api/points/1/label", json={"display_name": "x"}).status_code == 404
        assert rc.post("/api/discover").status_code == 404
        assert rc.post("/api/backup").status_code == 404
        # reading the operator log does not imply writing to it
        assert rc.post("/api/annotations", json={"title": "x"}).status_code == 404
        assert rc.delete("/api/annotations/1").status_code == 404


def test_local_sessions_keep_the_whole_program(tmp_path):
    """The restriction must apply to REMOTE clients only. On the box itself every page and
    endpoint stays exactly as it was."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    app = _viewer_app(tmp_path)

    with TestClient(app) as c:
        assert "<title>HVACTrend" in c.get("/").text
        for url in ("/dashboard", "/reports", "/settings", "/trends"):
            assert c.get(url).status_code == 200, url
        assert c.get("/api/systems").status_code == 200
        # ...and the viewer is served WITHOUT the remote marker
        assert 'class="viewer-only"' not in c.get("/trends").text


# ── Δ lines (derived series, A - B) ──────────────────────────────────────────
def _trends_js():
    from hvactrend.api import app as appmod
    return (pathlib.Path(appmod.__file__).parent / "static" / "trends.html").read_text(
        encoding="utf-8")


def test_delta_line_ids_never_reach_the_series_endpoint():
    """A Δ id is a STRING ("d:24597-24595"). `state.sel` is posted to /api/series, which
    takes integer point ids — putting a Δ in it would fail the fetch for EVERY series on
    the chart, not just the derived one. Δ ids therefore join at activeIds()/the legend,
    never in state.sel."""
    js = _trends_js()

    for call in ("post('/api/series'", 'post("/api/series"'):
        for i in _find_all(js, call):
            frag = js[i:i + 260]
            assert "activeIds()" not in frag, \
                "the series fetch must send state.sel, not activeIds() — that would post Δ ids"

    assert "state.sel.push(deltaId" not in js and "state.sel.push(did" not in js, \
        "a Δ id must never be pushed into state.sel"

    # ...and it DOES have to reach the two places that decide what is drawn/listed
    assert "for(const d of state.deltas)" in js
    assert "legIds" in js, "the legend has to list Δ lines too"


def test_delta_lines_refuse_a_meaningless_subtraction():
    """A difference between two different units is not a smaller number, it is a
    meaningless one — and it would plot perfectly happily. Refused, not warned."""
    js = _trends_js()
    assert "if(ua !== ub) return" in js, "unit mismatch must be refused"
    assert "DELTA_TOL_S" in js, "pairing needs an explicit tolerance"
    # the collector staggers devices by 5 s, so exact-timestamp matching is not enough,
    # and a tolerance wide enough to pair the wrong poll cycle would invent a difference
    assert "const DELTA_TOL_S = 60" in js


def test_delta_legend_handler_survives_a_string_id():
    """parseInt("d:24597-24595") is NaN. Every legend handler — hide, solo, remove — would
    then act on nothing, and the Δ chip would look broken rather than absent."""
    js = _trends_js()
    i = js.find("document.querySelectorAll('.leg[data-id]')")
    assert i > 0
    frag = js[i:i + 700]
    assert "isDelta(raw)" in frag, "the legend must branch on Δ ids before parseInt"
    assert "removeDelta(" in frag, "removing a Δ chip must remove the Δ, not toggleSel(NaN)"


def _find_all(hay, needle):
    out, i = [], hay.find(needle)
    while i != -1:
        out.append(i)
        i = hay.find(needle, i + 1)
    return out


def test_time_navigation_steps_by_the_window_not_a_fixed_day():
    """The stride has to be the window's OWN width — 24h steps a day, 7d steps a week, and
    a 4-day calendar range steps four days. A fixed 1-day step would make the 7d preset
    useless for exactly the comparison it exists for (this week vs last week)."""
    js = _trends_js()
    i = js.find("function stepWindow(")
    assert i > 0
    body = js[i:i + 1200]
    assert "const w = state.end - state.start" in body, "the stride must come from the window"
    assert "dir*w" in body
    # and it must not run off the end of the record in either direction
    assert "liveAnchor()" in body and "state.extent.min_ts" in body, \
        "stepping must clamp to the recorded extent, not wander into empty space"


def test_calendar_uses_local_midnight_not_utc():
    """new Date("2026-09-14") is UTC midnight. On Eastern time that is 8 PM on the 13th, so
    "show me the 14th" would render most of the previous evening and stop 4 h short. An
    operator means their OWN midnight."""
    js = _trends_js()
    assert "function localDayStart(" in js
    i = js.find("function localDayStart(")
    body = js[i:i + 500]
    assert "new Date(+m[1], +m[2]-1, +m[3]" in body, \
        "must build the date from parts (local), never parse the ISO string"
    # a range is inclusive of its last day, through the NEXT LOCAL midnight: "+ DAY_S" was
    # an hour short on the 25-hour day DST ends (2026-10-27; behaviour pinned in the JS suite)
    j = js.find("async function showDays(")
    assert j > 0 and "addLocalDays(Math.max(a, b), 1)" in js[j:j + 900], \
        "the last day of a range must be included, to its local midnight"


def test_the_live_badge_cannot_go_stale():
    """Every other reading on the page ("now 76.6", "2m ago") is relative to the window. A
    stepped-back window still claiming to be live is the one way this view misleads, so the
    badge is refreshed from updateRangeLabel() — the single chokepoint every window writer
    already passes through — rather than from each caller."""
    js = _trends_js()
    i = js.find("function updateRangeLabel(")
    assert i > 0
    assert "syncLiveFlag()" in js[i:i + 1400], \
        "the live badge must refresh from the window chokepoint, not per-caller"
    assert "function isLiveWindow(" in js
