# How it works

## Two programs

```
                 BACnet/IP (reads only)
   controllers  <---------------------->  SERVER  (scripts/serve.py)
                                            - one shared BACnet application
                                            - the collector: polls every 2 min
                                            - the web pages and their API
                                            - rollups, retention, backups, weather,
                                              the AI summary, all on a schedule
                                                    |
                                                    v
                                              data/hvactrend.db  (SQLite, WAL)
                                                    ^
                                                    |
                                          WATCHDOG (scripts/watchdog.py, every 5 min)
                                            - is data still arriving?
                                            - the alarm rules -> incidents
                                            - phone notifications (ntfy)
```

**The server** owns the one BACnet application, which binds the UDP port once. The
collector, and the pages' live reads and discovery, share it on one event loop; nothing else
may bind the port while the server runs. Slow work never runs on that loop. Chart queries,
exports, backups and the AI summary's preparation each run in a worker thread, on their own
read-only database connection, so polling never waits on a page.

**The watchdog** is a separate, short-lived process. It only reads the database (plus writes
its own alerts and incidents), so it keeps working when the server is stuck or stopped, and
it never imports the collector. That way a broken server cannot stop the message saying so.
The alarm rules run inside it behind a fence: a rule that fails is reported in the check-in,
and never stops the "data has stopped" alert.

## Storage

One SQLite file, `data/hvactrend.db`:

| Table | |
|---|---|
| `samples` | Every reading: point, time (UTC), value, quality |
| `samples_5m`, `samples_1h` | Min / max / average / last / count per bucket, built hourly |
| `points`, `devices`, `systems` | The inventory, with each point's role and whether it is logged |
| `incidents` | Equipment alarms from the rules |
| `alerts` | Watchdog alerts (data stopped, controller silent) |
| `point_command` | Who is in control of each commandable point (priority arrays) |
| `weather`, `annotations`, `reports`, `settings`, `views` | |

**Layout and time**
- The sample tables are clustered on `(point, time)`, so a chart is a range read on one key.
- Times are stored in UTC and shown in local time.

**What a chart reads**
- Windows up to 2 days: every reading.
- Up to 14 days: 5-minute averages.
- Wider: hourly.
- Any stretch the averages have not reached yet is computed from the raw readings, so a
  chart always ends at "now".

**Backups**
- A weekly full backup uses SQLite's online backup, is verified (integrity check and row
  count), and is only then kept.

## Code layout

```
src/hvactrend/
  bacnet/        the read-only BACnet application, the guard, discovery, priority arrays
  collector/     the poll loop and the jobs scheduled on it
  storage/       the database layer (sqlite_dal.py) - every SQL statement lives here
  rules/         the alarm rules: catalog (thresholds), evaluate, engine (episodes), live
  api/           the FastAPI app and the static pages (api/static/*.html, no build step)
  ai/            point roles, weather, the AI summary; the older statistical layer (off)
  commissioning.py  the equipment map
  notify.py      ntfy delivery; checkin.py the morning check-in
  export.py      Export data; coverage.py the Point Coverage review
scripts/         serve, watchdog, configure (setup wizard), discover, commission, demo, ...
tests/           pytest suite, plus tests/js for the trend viewer's JavaScript
```

## Tests

```
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest -q          # no hardware, no network
node --test tests/js/viewer.test.mjs       # the trend viewer's logic (needs Node 18+)
```

The suite includes:
- the read-only guard;
- a guard that EXPLAINs every SQL statement the storage layer runs and fails on a whole-table
  scan of the sample tables;
- the alarm rules over generated histories;
- DST handling;
- the notification messages.
