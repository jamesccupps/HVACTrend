# Changelog

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
