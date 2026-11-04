"""Guard the WITHOUT ROWID trap by checking the SQL the DAL ACTUALLY RUNS.

`samples`, `samples_5m` and `samples_1h` are clustered on (point_id, ts_utc|bucket_utc).
Any access that is not constrained by point_id -- or, for `samples` only, by idx_samples_ts
on ts_utc -- walks the whole table.

⚠️ EXPLAIN QUERY PLAN DOES NOT SAY "SCAN" FOR THE COMMON FORM OF THE MISTAKE. A bare
`MAX(bucket_utc)` plans as `SEARCH samples_5m USING PRIMARY KEY`, which reads as optimal,
and measures **1,126 ms against 10.7 ms** for the correlated per-point form on the live
database. That is why the trap was walked into three separate times, and why the test
written to pin it after the second time could not fail:

    tests/test_audit_fixes.py::test_extent_does_not_scan_the_rollup_tables  (removed)
      1. it EXPLAINed a hand-copied SQL STRING LITERAL, never what samples_time_extent()
         executes -- so reverting the production code could not fail it;
      2. its assertion, `"USING PRIMARY KEY" in plan`, matches the regression anyway.

So this guard does two things differently. The SQL is captured with `set_trace_callback`
while each DAL method really runs, and the assertion is on the ABSENCE of an unconstrained
plan line rather than the presence of a reassuring one. `test_guard_discriminates` proves
it catches the exact regression before anything else is trusted.

To extend: add the method to `calls` in test_no_dal_method_walks_a_sample_table. Every new
DAL method that touches a sample table belongs there.
"""
import re
import sqlite3

import pytest

from hvactrend.models import Quality, Sample
from hvactrend.storage.sqlite_dal import SQLiteDAL

_TABLES = ("samples", "samples_5m", "samples_1h")
_KW = {"where", "join", "left", "inner", "on", "group", "order", "limit", "union", "and",
       "set", "values", "select", "as"}

# DELIBERATE full scans. Each needs a reason and a measurement; keep this list short and
# argued, because every entry is a stall on the loop the BACnet collector shares.
ALLOW = [
    # /api/poll-health wants the exact total, not an estimate. Documented in samples_extent().
    # It runs in a worker thread on its own connection (samples_extent_readonly), NOT on the
    # loop: 10-23 s at 43.8 M rows (2026-10-20). Pinned by test_storage.py.
    ("samples_extent", re.compile(r"SELECT COUNT\(\*\) FROM samples\)")),
    # (enforce_retention's whole-table COUNT is no longer here: since 2026-10-27 it runs only
    # when a prune of MIN_PRUNE_GUARD_ROWS is pending, which the fixture never has, so a
    # return of the unconditional count fails this test.)
    # The picker's per-point good-sample count sums every hourly bucket: a full SCAN is the
    # RIGHT plan for "read every row". Since 2026-10-27 it runs once a day and on a
    # watermark that moved back (_hourly_sums_below); every other call adds only the new
    # buckets, per point on the key. It was every viewer load: 290 ms at 1.6 M rows.
    ("logged_points", re.compile(r"SELECT point_id, SUM\(n\) AS n FROM samples_1h")),
]


def _aliases(sql):
    """Map table aliases back to real table names, so `FROM samples_1h s` is still seen."""
    m = {t: t for t in _TABLES}
    for tbl, al in re.findall(r"\b(samples(?:_5m|_1h)?)\s+(?:AS\s+)?([A-Za-z_]\w*)", sql, re.I):
        if al.lower() not in _KW:
            m[al] = tbl
    return m


def unconstrained(conn, sql):
    """Plan lines that touch a sample table without a constraint. Empty == fine."""
    head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
    if head not in ("SELECT", "DELETE", "INSERT", "UPDATE", "WITH"):
        return []
    try:
        plan = conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
    except sqlite3.Error:
        return []
    names = _aliases(sql)
    bad = []
    for row in plan:
        d = row[3]
        m = re.match(r"^(SCAN|SEARCH) (\w+)(.*)$", d)
        # A lone MIN/MAX(ts_utc) on idx_samples_ts plans as an unconstrained SEARCH and
        # genuinely IS O(log n) -- that index leads with ts_utc. Only the CLUSTERED keys
        # are the trap, which is why the rollups (no timestamp index at all) are worse.
        if m and "idx_samples_ts" in m.group(3) and m.group(1) == "SEARCH":
            continue
        if m and m.group(2) in names and "(" not in m.group(3):
            bad.append(d)
    return bad


@pytest.fixture()
def dal(tmp_path):
    d = SQLiteDAL(tmp_path / "t.db")
    d.apply_schema()
    c = d._conn
    c.execute("INSERT INTO sites(id,code,name) VALUES (1,'DEMO','x')")
    c.execute("INSERT INTO buildings(id,site_id,code,name) VALUES (1,1,'MAIN','x')")
    c.execute("INSERT INTO systems(id,building_id,code,name) VALUES (1,1,'AC-1','x')")
    c.execute("INSERT INTO devices(id,site_id,device_instance,address,created_utc) "
              "VALUES (1,1,1,'192.0.2.1',0)")
    for pid in (1, 2, 3):
        c.execute("INSERT INTO points(id,device_id,system_id,object_type,object_instance,"
                  "name,enabled,created_utc,role,role_canonical) "
                  "VALUES (?,1,1,'analog-input',?,?,1,0,'DAT',1)", (pid, pid, f"P{pid}"))
    base = 3_600_000
    d.write_samples([Sample(point_id=p, ts_utc=base + i * 120, value_num=1.0 + i,
                            quality=Quality.GOOD) for p in (1, 2, 3) for i in range(200)])
    d.build_rollups(base + 200 * 120)
    return d, base


def _run(dal, fn):
    """Every statement the connection executes while fn() runs."""
    seen = []
    dal._conn.set_trace_callback(seen.append)
    try:
        fn()
    finally:
        dal._conn.set_trace_callback(None)
    return seen


def _violations(d, calls):
    out = []
    for name, fn in calls:
        for sql in _run(d, fn):
            if any(n == name and rx.search(sql) for n, rx in ALLOW):
                continue
            for line in unconstrained(d._conn, sql):
                out.append(f"{name}: {line}   <- {sql[:110]}")
    return out


def test_guard_discriminates(dal):
    """Prove the guard can fail before trusting a green run from it.

    This is the check the previous pin was missing: re-inject the exact 2026-09-15
    regression form and require that it is caught, then require the fix is not."""
    d, _ = dal
    assert unconstrained(d._conn, "SELECT MIN(bucket_utc) FROM samples_1h"), \
        "the guard does not catch the bare aggregate — it would pass the regression"
    assert not unconstrained(
        d._conn, "SELECT (SELECT MIN(bucket_utc) FROM samples_1h s "
                 "WHERE s.point_id = p.id) FROM points p"), \
        "the guard rejects the correct correlated form"


def test_no_dal_method_walks_a_sample_table(dal):
    d, base = dal
    now = base + 400 * 120
    policy = {"raw_days": 730, "rollup_5m_days": 365, "rollup_1h_days": 3650,
              "poll_health_days": 30, "alerts_days": 180}
    calls = [
        ("rollup_watermark", d.rollup_watermark),
        ("samples_time_extent", d.samples_time_extent),
        ("samples_extent", d.samples_extent),
        ("logged_points", d.logged_points),
        ("build_rollups", lambda: d.build_rollups(now)),
        ("enforce_retention", lambda: d.enforce_retention(now, policy)),
        ("query_series raw", lambda: d.query_series(1, base, now)),
        ("query_series 5m", lambda: d.query_series(1, base, now, rollup="5m")),
        ("query_series 1h", lambda: d.query_series(1, base, now, rollup="1h")),
        ("tail_buckets", lambda: d.tail_buckets(1, base, now, "1h")),
        ("latest_value", lambda: d.latest_value(1)),
        ("system_overview", lambda: d.system_overview(1, now_utc=now)),
        ("fleet_overview", lambda: d.fleet_overview(["DAT"], now_utc=now)),
        # the daily check-in (2026-09-29): a day's count must ride idx_samples_ts
        ("samples_count_between", lambda: d.samples_count_between(base, now)),
    ]
    bad = _violations(d, calls)
    assert not bad, "unconstrained access to a sample table:\n  " + "\n  ".join(bad)
