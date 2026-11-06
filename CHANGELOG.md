# Changelog

## Unreleased — fixes from an external review

**Security**
- The pages answer only to this computer's own names and addresses, plus any in the new
  `api.allowed_hosts`. A web page opened on this computer could otherwise use DNS rebinding
  to act as the operator: change or delete the phone-alert topic, or download the database.
- Requests that change something are refused when the browser says they come from another
  site. The three reads of the controllers that were GETs are now POSTs: live point reads,
  device object reads, and the coverage page's live check.
- Request bodies over 512 KB are refused, and the pages can't be framed by another site.
- The Settings page can no longer change `api.remote_viewer_only`, `api.local_hosts` or
  `api.allowed_hosts`.
- The Tempest token no longer appears in a weather error, which went to the log.
- A point name that starts with `=`, `+`, `-` or `@` is kept as text in CSV exports, not run
  as a spreadsheet formula.

**Alarms**
- The watchdog sends a last-resort push when it can't run: an unreadable `config.yaml`
  (including a YAML syntax error), a database that won't open, or checks that crash. It
  needs only the topic, sends at most once an hour, and says when it's running again.
- The watchdog's own alerts go out before the rules pass, and it waits up to 30 s for a busy
  database.
- Readings stamped in the future no longer switch off the data-stopped alarm. They raise
  their own alert.

**Robustness**
- The weather fetch runs off the event loop. On a network without internet it used to
  freeze polling and the pages for 15–25 s every hour.
- One priority reply that won't decode no longer loses a whole pass of override readings.
  A live read survives one too.
- A controller reporting ±Inf or NaN is recorded as a fault, not a number.
- If UDP 47808 is already taken by another BACnet program, startup says so instead of
  "running" with every read timing out.
- Report generation by hand checks `ai.enabled` and limits the window to 7 days. It also
  runs one report at a time, shared with the scheduler, and refuses an oversized prompt.
  The Anthropic SDK no longer retries inside the program's own retries.
- One discovery, device read or rescan at a time.
- `secrets.env` is written atomically, and a byte-order mark from Notepad is ignored.
- Rediscovering a controller no longer blanks its name, model or router flags.
- The "auth token" bar appears only when a token is actually wrong, not for "BACnet offline".
- `__version__` comes from the package metadata.

## 0.2.0 — rescan a controller

- **Rescan a controller from Point Selection** (⟲ Rescan controller). Reads one controller's
  object list again on the server's own BACnet connection, so nothing is stopped.
  - New points go on their unit's list, unticked, placed by the equipment map.
  - Renamed points keep their logging and history.
  - Points with a unit are never moved, and nothing is deleted. Points the controller no
    longer has are listed, but only after a complete read of its object list.
  - A controller whose address changed is found again by Who-Is.
  - [docs/commissioning.md](docs/commissioning.md#after-a-programming-change-rescan-a-controller).
- **Fixed:** ⟳ Rebuild model said "Rebuild failed" after every successful rebuild while the
  statistical baselines are off (the default).
- **Fixed:** a side-stream separator (a Lakos, say) is no longer taken for a condenser pump.
  Its pump's run status had been classified as a condenser pump status, so a filter pump that
  runs all the time counted as proof that the condenser pumps were on; its DP had shared the
  loop DP's role. It gets roles of its own: `CW_FILTER_STATUS`, `CW_FILTER_DP`,
  `CW_FILTER_ALARM`.
- Every page's script is now checked to parse, in the test suite.

## 0.1.0 — first public release

HVACTrend has run in one building since July 2026; this is its first release for others.

- **Read-only BACnet/IP trend logging.**
  - ReadPropertyMultiple batches with a per-object fallback.
  - A per-controller time budget.
  - Reading quality: good, faulted or no response.
  - Priority-array reads, to show overrides.
- **Trend viewer.**
  - Zoom and pan, and calendar days.
  - Compare against the previous day or week; plot a reading against its setpoint.
  - X-Y scatter, weather overlay and occupied-hours shading.
  - Notes, saved views and export.
  - Phone layout.
- **Unit dashboard, Point Selection and Point Coverage pages.**
- **Rule-based alarms**, evaluated by an independent watchdog:
  - discharge air and duct static against setpoint;
  - a boiler enabled but not heating;
  - impossible sensor readings;
  - a controller's own alarm points.
- **Phone notifications through ntfy:**
  - data stopped, a controller not responding, points missing;
  - equipment alarms and their all-clears;
  - a morning check-in and a daily reminder of alarms still on.
- **Backups.** A weekly verified database backup, an optional off-computer copy, and a daily
  configuration snapshot.
- **Optional AI summary** (Claude), narrating what the alarm rules found.
- **Setup.**
  - Installers: `install.bat` / `install.ps1`, and `install.sh`.
  - A setup wizard (`scripts/configure.py`).
  - Windows scheduled-task service scripts, and systemd units.
  - A demo building (`scripts/demo.py`).
- **Safe defaults.**
  - The web pages are local to the computer.
  - Other computers get the read-only viewer only.
  - Nothing is sent anywhere until you set it up.
