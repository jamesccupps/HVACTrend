"""Rescanning one controller from the running server (rescan.py, Point Selection's button).

The fixture is shaped like a plant panel: a whole-controller default ("CT"), a split by
name rules (CT-1 / CT-2 / CWP-1), and points placed BY HAND that the rules would put
somewhere else. Hermetic: a scripted controller answers in place of the BACnet stack.
"""

import asyncio
import sqlite3
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("bacpypes3", reason="bacpypes3 not installed")
from bacpypes3.apdu import AbortPDU  # noqa: E402

from hvactrend import rescan  # noqa: E402
from hvactrend.bacnet.discovery import (  # noqa: E402
    DiscoveredObject,
    _read_object_list,
)

INST = 2001
EQUIPMENT = {
    "device_systems": {INST: "CT"},
    "point_systems": [
        {"device": INST, "match": "SITE.CT1*", "system": "CT-1", "kind": "plant"},
        {"device": INST, "match": "SITE.CT2*", "system": "CT-2", "kind": "plant"},
        {"device": INST, "match": "SITE.CHW.PMP1*", "system": "CWP-1", "kind": "plant"},
    ],
    "building": {"code": "MAIN", "name": "Main"},
}

# what the controller has NOW: (type, instance, name)
NOW = [
    ("analog-input", 1, "SITE.CT1.SPD"),            # known, on CT-1
    ("analog-input", 2, "SITE.CHW.HDR.SUP.TEMP"),   # known, on CT
    ("binary-input", 3, "SITE.CT2.STS"),            # known, put on CT BY HAND (rule: CT-2)
    ("analog-value", 5, "SITE.CHW.PMP1.SPD"),       # known, no unit; a point rule names it
    ("analog-value", 6, "!PANEL:BATT"),            # known, no unit; only the default would
    ("analog-input", 7, "SITE.RENAME.NEW"),         # known as SITE.RENAME.OLD, logged
    ("analog-value", 10, "SITE.CT2.NEWPOINT"),      # new -> CT-2 (a unit not created yet)
    ("analog-value", 11, "SITE.CHW.NEWTHING"),      # new -> CT, the controller's default
    ("program", 1, "TOWER_PPCL"),    # new, not a point
    ("trend-log", 2, "TL_SITE_CT1_SPD_15min"),      # new trend log: stored, no unit
    ("trend-log", 8, "SITE.CT1.SPD_TL_15"),         # known trend log, no unit: stays so
]


def _seed(db) -> None:
    from hvactrend.storage.sqlite_dal import SQLiteDAL
    dal = SQLiteDAL(db)
    dal.apply_schema()
    c = dal._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'S','S')")
    c.execute("INSERT INTO buildings(id,site_id,code,name) VALUES (1,1,'MAIN','Main')")
    c.execute("INSERT INTO systems(id,building_id,code,name,kind) VALUES (1,1,'CT','CT','plant')")
    c.execute("INSERT INTO systems(id,building_id,code,name,kind) "
              "VALUES (2,1,'CT-1','CT-1','plant')")
    c.execute("INSERT INTO devices(id,site_id,device_instance,name,address,is_router,"
              "mac_address,created_utc) VALUES (1,1,?,'PXC','192.0.2.30',1,'mac-kept',0)",
              (INST,))
    for pid, ot, oi, name, sid, en in (
            (1, "analog-input", 1, "SITE.CT1.SPD", 2, 1),
            (2, "analog-input", 2, "SITE.CHW.HDR.SUP.TEMP", 1, 1),
            (3, "binary-input", 3, "SITE.CT2.STS", 1, 0),
            (4, "analog-value", 4, "SITE.OLD.POINT", 1, 1),       # gone from the panel
            (5, "analog-value", 5, "SITE.CHW.PMP1.SPD", None, 0),
            (6, "analog-value", 6, "!PANEL:BATT", None, 0),
            (7, "analog-input", 7, "SITE.RENAME.OLD", 1, 1),
            (8, "trend-log", 8, "SITE.CT1.SPD_TL_15", None, 0)):
        c.execute("INSERT INTO points(id,device_id,system_id,source,object_type,object_instance,"
                  "name,enabled,created_utc) VALUES (?,1,?,'bacnet',?,?,?,?,0)",
                  (pid, sid, ot, oi, name, en))
    dal.close()


class _Panel:
    """A scripted controller: a whole-array object list (or a short one) and names by RPM.
    Reads only, like the real stack -- there is no write method to call."""

    def __init__(self, objects, *, address="192.0.2.30", declared=None, iam_address=None,
                 hold=False):
        self.objects = list(objects)
        self.address = address
        self.declared = len(objects) if declared is None else declared
        self.iam_address = iam_address
        self.hold = hold
        self.whois = 0

    def _reach(self, address):
        if str(address) != self.address:
            raise TimeoutError(f"no answer at {address}")

    async def read_property(self, address, oid, prop, property_array_index=None):
        self._reach(address)
        if prop != "object-list":
            return AbortPDU()
        if property_array_index == 0:
            return self.declared
        oids = [f"{t},{i}" for t, i, _ in self.objects]
        if property_array_index is None:
            return oids
        return oids[property_array_index - 1] if property_array_index <= len(oids) \
            else AbortPDU()

    async def read_property_multiple(self, address, params):
        self._reach(address)
        while self.hold:
            await asyncio.sleep(0.01)
        names = {f"{t},{i}": n for t, i, n in self.objects}
        rows = []
        for oid in params[::2]:
            rows.append((str(oid), "object-name", None,
                         "PXC-TEST" if str(oid).startswith("device,") else names[str(oid)]))
        return rows

    async def who_is(self, low, high, address, timeout):
        self.whois += 1
        if self.iam_address is None:
            return []
        return [SimpleNamespace(iAmDeviceIdentifier=f"device,{INST}",
                                pduSource=self.iam_address)]


class _Cfg(dict):
    def as_dict(self):
        return dict(self)


def _config():
    return {"equipment": _Cfg(EQUIPMENT)}


def _rows(db, sql, *a):
    c = sqlite3.connect(db)
    try:
        return c.execute(sql, a).fetchall()
    finally:
        c.close()


def _unit(db, name):
    return _rows(db, "SELECT s.code FROM points p LEFT JOIN systems s ON s.id=p.system_id "
                     "WHERE p.name=?", name)[0][0]


def test_a_rescan_adds_new_points_and_never_moves_a_known_one(tmp_path):
    db = tmp_path / "r.db"
    _seed(db)
    seen = []
    r = asyncio.run(rescan.rescan_device(_Panel(NOW), str(db), None, _config(), INST,
                                         on_progress=lambda d, t: seen.append((d, t))))

    # new points: placed by the map; the trend log is kept but not offered
    assert {(n["name"], n["system"]) for n in r["new"]} == {
        ("SITE.CT2.NEWPOINT", "CT-2"), ("SITE.CHW.NEWTHING", "CT")}
    assert (r["new_count"], r["other_new"]) == (2, 2)   # + a program and a trend log
    assert _unit(db, "SITE.CT2.NEWPOINT") == "CT-2" and _unit(db, "SITE.CHW.NEWTHING") == "CT"
    assert _rows(db, "SELECT kind FROM systems WHERE code='CT-2'") == [("plant",)]
    assert _rows(db, "SELECT enabled FROM points WHERE name='SITE.CT2.NEWPOINT'") == [(0,)]

    # a hand-placed point stays where it was put, whatever the rules say
    assert _unit(db, "SITE.CT2.STS") == "CT" and _unit(db, "SITE.CT1.SPD") == "CT-1"
    # a known point with no unit: a point rule places it; a controller default does not
    assert [(p["name"], p["system"]) for p in r["placed"]] == [("SITE.CHW.PMP1.SPD", "CWP-1")]
    assert _unit(db, "SITE.CHW.PMP1.SPD") == "CWP-1" and _unit(db, "!PANEL:BATT") is None
    # trend logs go on no unit's list, new or known, whatever a rule matches
    assert _unit(db, "TL_SITE_CT1_SPD_15min") is None and _unit(db, "SITE.CT1.SPD_TL_15") is None

    # renamed in place: same row, still logged
    assert r["renamed"] == [{"old": "SITE.RENAME.OLD", "new": "SITE.RENAME.NEW",
                             "system": "CT", "logged": True}]
    assert _rows(db, "SELECT id, enabled FROM points WHERE name='SITE.RENAME.NEW'") == [(7, 1)]

    # gone: listed, kept, still logged -- never deleted
    assert r["complete"] and (r["gone_count"], r["gone_logged"]) == (1, 1)
    assert r["gone"][0]["name"] == "SITE.OLD.POINT"
    assert _rows(db, "SELECT enabled FROM points WHERE id=4") == [(1,)]

    # the controller's row keeps what a rescan cannot vouch for
    assert _rows(db, "SELECT is_router, mac_address, address FROM devices WHERE id=1") == [
        (1, "mac-kept", "192.0.2.30")]
    assert r["roles_classified"] is not None
    assert seen[0] == (0, len(NOW)) and seen[-1] == (len(NOW), len(NOW))


def test_a_second_rescan_finds_nothing_new(tmp_path):
    db = tmp_path / "r.db"
    _seed(db)
    asyncio.run(rescan.rescan_device(_Panel(NOW), str(db), None, _config(), INST))
    r = asyncio.run(rescan.rescan_device(_Panel(NOW), str(db), None, _config(), INST))
    assert (r["new_count"], r["renamed_count"], r["placed_count"]) == (0, 0, 0)
    assert r["roles_classified"] is None            # nothing changed: no rebuild
    assert r["gone_count"] == 1                     # the old point is still reported


def test_a_short_object_list_reports_nothing_as_gone(tmp_path):
    db = tmp_path / "r.db"
    _seed(db)
    panel = _Panel(NOW[:3], declared=len(NOW[:3]) + 4)     # says 7, gives 3
    r = asyncio.run(rescan.rescan_device(panel, str(db), None, _config(), INST))
    assert r["complete"] is False and r["declared"] == 7
    assert r["gone_count"] == 0                      # 4 known points missing: NOT "gone"
    assert _rows(db, "SELECT COUNT(*) FROM points")[0][0] == 8


def test_a_controller_that_moved_is_found_by_who_is(tmp_path):
    db = tmp_path / "r.db"
    _seed(db)
    panel = _Panel(NOW, address="192.0.2.31", iam_address="192.0.2.31")
    r = asyncio.run(rescan.rescan_device(panel, str(db), None, _config(), INST))
    assert panel.whois == 1 and r["address_changed"] and r["address"] == "192.0.2.31"
    assert _rows(db, "SELECT address FROM devices WHERE id=1") == [("192.0.2.31",)]


def test_a_controller_that_does_not_answer_is_an_error_in_words(tmp_path):
    db = tmp_path / "r.db"
    _seed(db)
    with pytest.raises(rescan.RescanError, match="did not answer"):
        asyncio.run(rescan.rescan_device(_Panel(NOW, address="192.0.2.99"), str(db), None,
                                         _config(), INST))
    with pytest.raises(rescan.RescanError, match="not in the inventory"):
        asyncio.run(rescan.rescan_device(_Panel(NOW), str(db), None, _config(), 4242))
    assert _rows(db, "SELECT COUNT(*) FROM points")[0][0] == 8       # nothing written


def test_plan_alone():
    known = [{"id": 1, "object_type": "analog-value", "object_instance": 1,
              "name": "SITE.CT1.X", "system": "CT", "enabled": 1}]
    objs = [DiscoveredObject("analog-value", 1, name="SITE.CT1.X"),
            DiscoveredObject("device", INST, name="PXC")]
    p = rescan.plan(known, objs, device_instance=INST, equipment=EQUIPMENT, complete=True)
    assert not p["placed"] and not p["renamed"] and not p["gone"]   # CT stays CT
    assert p["other_new"] == 1 and p["new"][0]["system"] is None    # the device object
    p = rescan.plan(known, [], device_instance=INST, equipment=EQUIPMENT, complete=False)
    assert p["gone"] == []                                         # incomplete: no claims


def test_the_object_list_says_whether_it_is_complete():
    class _L:
        def __init__(self, count, items):
            self.count, self.items = count, items

        async def read_property(self, address, oid, prop, property_array_index=None):
            if property_array_index == 0:
                return self.count
            if property_array_index is None:
                return list(self.items)
            return self.items[property_array_index - 1] \
                if property_array_index <= len(self.items) else AbortPDU()

    for count, items, complete in ((2, ["a,1", "b,2"], True),     # counted, all there
                                   (AbortPDU(), ["a,1"], False),  # nobody said how many
                                   (3, ["a,1", "b,2"], False)):   # one element missing
        st = {}
        asyncio.run(_read_object_list(_L(count, items), "x", "device,1", stats=st))
        assert st["complete"] is complete, (count, items, st)


# ── the API ─────────────────────────────────────────────────────────────────────────────
def _app(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    db = tmp_path / "r.db"
    _seed(db)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "site: {code: S, name: S}\n"
        "bacnet:\n  local_address: '<COLLECTOR_IP>/24'\n"
        "  device_instance: '<LOGGER_DEVICE_INSTANCE>'\n  poll: {interval_s: 120}\n"
        "  safety: {allow_writes: false, allow_cov: false}\n"
        f"storage: {{path: '{str(db).replace(chr(92), '/')}'}}\n"
        "equipment:\n  device_systems: {2001: CT}\n"
        "  point_systems:\n"
        "    - {device: 2001, match: 'SITE.CT2*', system: CT-2, kind: plant}\n",
        encoding="utf-8")
    from hvactrend.api.app import create_app
    return create_app(str(cfg), enable_bacnet=False), db


def _wait(client, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = client.get("/api/rescan").json()
        if st["state"] != "running":
            return st
        time.sleep(0.02)
    raise AssertionError("the rescan never finished")


def test_the_rescan_api_runs_one_job_and_reports_it(tmp_path):
    from fastapi.testclient import TestClient
    app, db = _app(tmp_path)
    with TestClient(app) as c:
        ctl = c.get("/api/rescan/controllers").json()
        assert ctl["bacnet"] is False
        assert [(x["device_instance"], x["units"]) for x in ctl["controllers"]] == [
            (INST, ["CT", "CT-1"])]
        assert c.post("/api/rescan", params={"device": INST}).status_code == 503   # no stack
        assert c.post("/api/rescan", params={"device": -1}).status_code in (400, 503)

        panel = _Panel(NOW, hold=True)
        app.state.bacnet = panel
        r = c.post("/api/rescan", params={"device": INST})
        assert r.status_code == 200 and r.json()["state"] == "running"
        assert c.post("/api/rescan", params={"device": INST}).status_code == 409
        panel.hold = False
        st = _wait(c)
        assert st["state"] == "done", st
        assert {n["name"] for n in st["result"]["new"]} == {"SITE.CT2.NEWPOINT",
                                                            "SITE.CHW.NEWTHING"}
        assert st["done"] == st["total"] == len(NOW)

        r = c.post("/api/rescan", params={"device": 4242})       # not in the inventory
        st = _wait(c)
        assert st["state"] == "error" and "not in the inventory" in st["error"]
    assert _unit(db, "SITE.CT2.NEWPOINT") == "CT-2"


def test_a_remote_client_cannot_reach_the_rescan(tmp_path):
    from tests.test_browser import _remote_client
    app, _db = _app(tmp_path)
    with _remote_client(app) as rc:
        assert rc.post("/api/rescan", params={"device": INST}).status_code == 404
        assert rc.get("/api/rescan").status_code == 404
        assert rc.get("/api/rescan/controllers").status_code == 404
