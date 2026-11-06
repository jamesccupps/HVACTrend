# Contributing to HVACTrend

Thank you for helping. Bug reports, point names the classifier gets wrong, and pull requests
are all welcome.

## Reporting a problem

Please open an issue with:
- what you expected, and what happened;
- the relevant lines of `logs/hvactrend.log`;
- your OS and Python version;
- your controllers' make and model, if it is about BACnet.

**Point names the program does not recognise** (a wrong or missing role on the dashboard or
in the rules) are especially useful. Paste a few names as they appear on the Point Selection
page.

**Do not paste** `config/secrets.env`, your ntfy topic, or anything from the Settings page's
key fields. Security problems go through private reporting ([SECURITY.md](SECURITY.md)).

## Development setup

```bash
git clone https://github.com/jamesccupps/HVACTrend.git
cd HVACTrend
python -m venv .venv
.venv/bin/pip install -e ".[dev,ai]"     # Windows: .venv\Scripts\pip ...
.venv/bin/python -m pytest -q            # no hardware, no network
node --test tests/js/viewer.test.mjs     # the trend viewer's logic (Node 18+)
node --test tests/js/realtime.test.mjs   # the Realtime page's charts
.venv/bin/python scripts/demo.py         # a made-up building on http://localhost:8001
```

## Hard rules

These protect people's buildings. A pull request that breaks one will not be merged.

1. **BACnet is strictly read-only.**
   - No WriteProperty, no priority-array writes, no COV switched on quietly.
   - `bacnet/readonly.py` is allowlist-first, and the application façade has **no write
     method**. Adding one breaks the project's central promise.
   - `tests/test_safety.py` pins this. Never weaken those tests to make a build pass.
2. **One BACnet application, one event loop.**
   - The collector and the pages' live reads share the single `ReadOnlyBACnetApplication`,
     which binds the UDP port once.
   - Never start a second stack.
   - Never block that loop: database work over more than a handful of rows runs in a worker
     thread on its own read-only connection.
3. **Alerts come from the watchdog, never the server.**
   - The watchdog must not import the collector.
   - It imports the alarm rules only inside a fence, so that a broken module cannot silence
     the "data has stopped" alert. A test makes the rules package unimportable.
4. **Secrets never touch a tracked file.**
   - Keys and tokens are entered on the Settings page and live in `config/secrets.env`.
   - Backups and snapshots exclude that file, and scan their contents for its values.
5. **No real building's addresses, instances or names in the repository.** The example config
   ships placeholders only.

## How the code is written

- **Timestamps:** UTC epoch seconds in storage; local time only for display.
- **Schema:**
  - `db/schema.sql` is the source of truth.
  - Columns are added in `SQLiteDAL._ensure_columns` (additive, idempotent); never rewritten
    in place.
- **Dependencies:**
  - The core imports with the standard library alone.
  - Heavy dependencies (bacpypes3, fastapi, anthropic, httpx) are imported lazily.
  - bacpypes3 is pinned exactly.
- **Comments say why, not what.** Many record a measurement or an incident that explains a
  choice. Keep them when you change the code they explain.
- **Tests:**
  - Behaviour changes come with a test.
  - A new test must be **proven to fail** against the old behaviour: break the fix in a scratch
    copy and watch it fail.
  - `tests/js/` extracts functions from the shipped `trends.html` and runs them in Node, with
    no build step.

## Lessons that cost real downtime

1. **Load the page.**
   - The pages are plain HTML and JavaScript with no build step. A syntax error anywhere
     kills the whole script, while the server still returns 200 and every Python test passes.
   - After editing `api/static/*.html`, open the page and read the browser console. The JS
     suite parses every page's scripts, but that is not the same as a loaded page.
2. **Clustered keys.**
   - `samples`, `samples_5m` and `samples_1h` are `WITHOUT ROWID` on `(point_id, time)`.
     Per-point work goes through the point list.
   - A bare `MAX(bucket_utc)` walks the whole table even though EXPLAIN says
     `SEARCH ... USING PRIMARY KEY`.
   - Every DAL method that touches a sample table goes in `tests/test_plan_guard.py`.
3. **Never schedule from process uptime.** The service restarts on failure and at every
   boot. When a job is due comes from durable state: files on disk, watermarks in the
   database.
4. **Fix the siblings.** When you fix a rule in one place, find every place that makes the
   same decision. Prefer one shared helper, plus a test that fails if a caller stops using it.
5. **Measure before optimising.** Use a read-only connection (`file:...?mode=ro`) on a real
   database.
6. **Line endings are per file.** The tree mixes CRLF and LF; `.gitattributes` keeps every
   file byte for byte. Preserve each file's convention.
7. **Some points answering is not proof a controller was fully enumerated.** Check the count.
8. **The AI narrates; it does not detect.**
   - The summary is handed what the rules found, each under an id, and an action citing none
     of them is dropped.
   - The older statistical layer (`ai/context.py`, `ai/prescreen.py`, `alerts.enabled`,
     `ai.baselines`) is off for good reasons. It flapped, ignored schedules, and stalled the
     event loop.
9. **Ask about design intent.** A threshold, a schedule, or what a point means depends on how
   the building is supposed to run. That comes from its sequences of operations, not from
   guessing at the trend data.

## Pull requests

- Keep each pull request to one change, with its tests.
- Say what you verified, and how. For BACnet behaviour, say which controllers you tested
  against: a change to polling is not done until it has run against real hardware.
- Run `pytest` and the JS suite. Load any page you touched.
