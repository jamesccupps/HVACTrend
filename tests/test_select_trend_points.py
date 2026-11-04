"""select_trend_points.py seeds NEW systems; it never silently re-curates an existing one.

2026-09-29 audit, A1. `--commit` used to reconcile every system to a name heuristic --
enable what it picks, disable everything else. Measured against the live building that
would have stopped logging 262 of 664 hand-curated points and switched on 213 others,
including dead sensors pruned by hand. DEPLOYMENT.md listed it as a commissioning step.
"""
import importlib.util
import sqlite3
from pathlib import Path

import pytest

from hvactrend.storage.sqlite_dal import SQLiteDAL

REPO = Path(__file__).resolve().parents[1]


def _script():
    spec = importlib.util.spec_from_file_location(
        "select_trend_points", REPO / "scripts" / "select_trend_points.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


PICKED = "Discharge Air Temperature|dac-1"     # the heuristic selects this...
DEAD = "Zone Temp|dac-1"                        # ...and this, a sensor pruned by hand
HAND = "Controller Uptime Counter|dac-1"        # hand-enabled; the heuristic ignores it


@pytest.fixture()
def setup(tmp_path):
    m = _script()
    # The test is only meaningful if the heuristic still classifies these three names the
    # way the scenario needs. Fail loudly on the setup rather than silently testing nothing.
    assert m.is_selected(PICKED) and m.is_selected(DEAD) and not m.is_selected(HAND), \
        "the name heuristic changed; pick new fixture names"

    db = tmp_path / "t.db"
    d = SQLiteDAL(db)
    d.apply_schema()
    c = d._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'DEMO','x')")
    c.execute("INSERT INTO buildings(id,site_id,code,name) VALUES (1,1,'MAIN','x')")
    c.execute("INSERT INTO systems(id,building_id,code,name) VALUES (1,1,'CUR','curated')")
    c.execute("INSERT INTO systems(id,building_id,code,name) VALUES (2,1,'NEW','new unit')")
    c.execute("INSERT INTO devices(id,site_id,device_instance,address,created_utc) "
              "VALUES (1,1,1,'192.0.2.1',0)")
    rows = [  # id, system, name, enabled
        (1, 1, PICKED, 1),     # curated: picked and on
        (2, 1, DEAD, 0),       # curated: picked, but deliberately OFF (dead sensor)
        (3, 1, HAND, 1),       # curated: hand-enabled, not picked
        (4, 2, PICKED, 0),     # new system: nothing enabled yet
        (5, 2, HAND, 0),
    ]
    for pid, sid, name, en in rows:
        c.execute("INSERT INTO points(id,device_id,system_id,object_type,object_instance,"
                  "name,enabled,created_utc) VALUES (?,1,?,'analog-input',?,?,?,0)",
                  (pid, sid, pid, name, en))
    d.close()

    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(f"""site: {{code: DEMO, name: Demo Building}}
bacnet:
  local_address: '<COLLECTOR_IP>/24'
  device_instance: '<LOGGER_DEVICE_INSTANCE>'
  poll: {{interval_s: 120}}
  safety: {{allow_writes: false, allow_cov: false}}
storage: {{path: '{str(db).replace(chr(92), chr(47))}'}}
""", encoding="utf-8")
    return m, db, str(cfg)


def _enabled(db):
    c = sqlite3.connect(db)
    try:
        return {r[0]: r[1] for r in c.execute("SELECT id, enabled FROM points")}
    finally:
        c.close()


def test_a_bare_commit_seeds_new_systems_and_leaves_curated_ones_alone(setup):
    m, db, cfg = setup
    assert m.main(["--config", cfg, "--commit"]) == 0
    en = _enabled(db)
    assert en[3] == 1, "a hand-enabled point was switched OFF by a bare --commit"
    assert en[2] == 0, "a sensor pruned by hand was switched back ON by a bare --commit"
    assert en[1] == 1
    assert en[4] == 1 and en[5] == 0, "the NEW system was not seeded from the heuristic"


def test_naming_a_system_adds_its_picks_and_removes_nothing(setup):
    m, db, cfg = setup
    m.main(["--config", cfg, "--system", "CUR", "--commit"])
    en = _enabled(db)
    assert en[2] == 1, "naming the system should add the heuristic's picks"
    assert en[3] == 1, "without --prune nothing may be disabled"


def test_prune_is_a_deliberate_one_system_reconcile(setup):
    m, db, cfg = setup
    m.main(["--config", cfg, "--system", "CUR", "--commit", "--prune"])
    assert _enabled(db)[3] == 0, "--prune on a named system disables what it does not pick"
    assert _enabled(db)[5] == 0


def test_prune_refuses_to_run_across_every_system(setup):
    m, _db, cfg = setup
    with pytest.raises(SystemExit):
        m.main(["--config", cfg, "--commit", "--prune"])


def test_a_dry_run_writes_nothing(setup):
    m, db, cfg = setup
    before = _enabled(db)
    m.main(["--config", cfg])
    m.main(["--config", cfg, "--system", "CUR", "--prune"])
    assert _enabled(db) == before
