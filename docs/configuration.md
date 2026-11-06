# Configuration

All settings live in `config/config.yaml`, which the setup wizard writes from
`config/config.example.yaml`. Both carry a comment on every setting.

- **When changes apply:** at the next server restart. The watchdog reads the file fresh every
  5 minutes.
- **The Settings page** can change many of these without editing the file. Changes are stored
  in the database, override the file, and also apply on restart.
- **Never from the pages:** the BACnet identity, the read-only safety switches, storage and
  retention, and any setting that names a file.
- **Secrets** (API keys, the phone-notification topic, the UI token) are never in this file.
  They are entered on the Settings page and kept in `config/secrets.env`.

## site

| Key | Default | |
|---|---|---|
| `code` | `MAIN` | Short code for the building |
| `name` | `My Building` | Shown on the pages and in the AI summary |
| `location` | `""` | City / region, for the AI summary |
| `timezone` | `America/New_York` | Informational. Times display in the **computer's** time zone, so set the computer to the building's. |

## bacnet

| Key | Default | |
|---|---|---|
| `local_address` | `<COLLECTOR_IP>/24` | This computer's address on the BACnet network, with prefix length. Must be filled in. |
| `device_instance` | `<LOGGER_DEVICE_INSTANCE>` | HVACTrend's own BACnet instance: any unused number up to 4194302. Must be filled in. |
| `object_name` | `HVACTrend-Logger` | HVACTrend's own BACnet device name |
| `poll.interval_s` | `120` | How often every logged point is read |
| `poll.stagger_s` | `5` | Pause between controllers within a cycle |
| `poll.rpm_batch` | `10` | Points per ReadPropertyMultiple request |
| `poll.device_budget_s` | `45` | Longest one controller may take per cycle. A slow one cannot hold up the rest. |
| `poll.priority_interval_s` | `600` | How often priority arrays are read, to see overrides; 0 = off |
| `safety.allow_writes` / `allow_cov` | `false` | Leave false. Writes are refused in code whatever these say. |

## equipment

Which controller runs which unit: see [commissioning.md](commissioning.md#2-the-equipment-map).
The keys are `device_systems`, `point_systems`, `sc_dac_map`, `building` and `targets`.

## storage

| Key | Default | |
|---|---|---|
| `path` | `data/hvactrend.db` | The database |
| `retention.raw_days` | `365` | Every reading, kept this long. Floor: 7. |
| `retention.rollup_5m_days` | `365` | 5-minute averages |
| `retention.rollup_1h_days` | `null` | Hourly averages; null = for ever |
| `retention.poll_health_days` | `30` | Per-controller poll diagnostics |
| `retention.alerts_days` | `180` | Resolved alerts |
| `snapshot_interval_s` | `86400` | A daily zip of `config/` (never `secrets.env`) in `data/snapshots` |
| `backup.interval_s` | `604800` | A full, verified copy of the database, weekly. 0 = off. |
| `backup.keep` | `4` | Copies kept. Each is a full database: check the disk. |
| `backup.offsite_dir` | `""` | A folder that syncs off this computer, for a compressed copy of each backup |

A retention change that would delete a large share of the record in one pass is refused, and
logged. That protects against a typo; set `retention.allow_large_prune: true` for one run if
you really mean it.

## weather

Outdoor conditions, for the charts, the exports, the dashboard and the AI summary.

| Key | Default | |
|---|---|---|
| `refresh_interval_s` | `3600` | 0 = off |
| `open_meteo.enabled`, `latitude`, `longitude` | off | Free, no key |
| `tempest.enabled`, `cloud.station_id` | off | A WeatherFlow Tempest station; its token goes on the Settings page |

## rules

The alarm rules: see [alerts.md](alerts.md#the-alarm-rules).

| Key | Default | |
|---|---|---|
| `enabled` | `true` | Evaluate the rules, every 5 minutes, in the watchdog |
| `push_critical` | `true` | CRITICAL alarms to the phone (needs a topic) |
| `push_action` | `false` | ACTION alarms too. Turn on after replaying a month. |
| `<rule>` | (built-in) | Building-wide thresholds, e.g. `sat-setpoint: {tiers: {action: {delta: 6}}}` |
| `overrides` | `{}` | Per unit: `AHU-2: {sat-setpoint: {tiers: {action: null}}}` |

## alerts

The watchdog and phone notifications: see [alerts.md](alerts.md).

| Key | Default | |
|---|---|---|
| `stale_after_s` | `900` | No new readings this long: "data has stopped" |
| `device_stale_after_s` | `1800` | One controller silent this long |
| `poll_failed_hold_s` | `240` | A failed poll is sent only if still failing next run |
| `checkin`, `checkin_time` | `true`, `07:00` | The morning check-in |
| `reminders`, `reminder_time` | `true`, `12:00` | The daily reminder of alarms still on |
| `ntfy_server` | `https://ntfy.sh` | Or your own ntfy server |
| `enabled` | `false` | The old statistical pre-screen. Leave off. |

## site_knowledge

Points to ignore, rules that are not possible here, overrides held on purpose: see
[commissioning.md](commissioning.md#site-knowledge).

## api

| Key | Default | |
|---|---|---|
| `host` | `127.0.0.1` | This computer only. `0.0.0.0` lets other computers open the trend viewer. |
| `port` | `8000` | |
| `remote_viewer_only` | `true` | Other computers get the trend viewer and the Realtime page only: no settings, secrets, backups or point selection |
| `local_hosts` | `[]` | Extra addresses, or networks (`100.64.0.0/10`), to treat as this computer: they get every page. Only a network where every device is yours. Not settable from the Settings page |
| `allowed_hosts` | `[]` | Extra names the pages answer to (a DNS alias people type). This computer's own names and addresses always work. Not settable from the Settings page |
| `auth.enabled` | `false` | Require the UI token (Settings page) for any change |

## realtime

The Realtime page's caps. Its readings are kept in their own table, apart from the 2-minute
record.

| Key | Default | |
|---|---|---|
| `max_points` | `20` | Points running at once, whoever started them |
| `max_per_controller` | `10` | Points running at once on one controller |
| `max_hours` | `24` | The longest one run |
| `intervals_s` | `[5, 10, 15, 30, 60]` | The rates on offer, in seconds |

## ai

The optional AI summary: a daily and weekly plain-language account of what the alarm rules
found.
- **Install:** `pip install -e ".[ai]"`.
- **Key:** an Anthropic API key, entered on the Settings page.

| Key | Default | |
|---|---|---|
| `enabled` | `false` | |
| `providers.claude.model` | `claude-opus-5-5` | |
| `schedule.daily_time`, `weekly_day`, `weekly_time` | `06:00`, `Mon`, `06:30` | |
| `prompts.template_dir` | `config/prompts` | `daily.md` and `weekly.md`: edit to taste |

It sends the rules' findings and summary statistics for the period to Anthropic. It never
sends secrets, and it costs a few US dollars a month. A failed summary is reported in the
check-in.

## analysis

| Key | Default | |
|---|---|---|
| `occupancy.weekday_start` / `weekday_end` | `5` / `18` | The building's usual occupied hours, local. Per-unit schedules are set on the dashboard. |
| `occupancy.weekend_occupied` | `false` | |
| `baseline.rebuild_interval_s` | `86400` | Re-classify point roles daily |
| `limits.from_plant` | `true` | Use the controllers' own trended limit points |
| `economizer.*` | ASHRAE values | Changeover thresholds, for context |
| `schedule_detect.*` | | "What hours does each unit really run?" (Dashboard) |

## logging

| Key | Default | |
|---|---|---|
| `level` | `INFO` | |
| `file` | `logs/hvactrend.log` | Rotated at 5 MB, 5 kept |

## Environment variables

| Variable | |
|---|---|
| `HVACTREND_CONFIG` | Another config file (default `config/config.yaml`) |
| `HVACTREND_SECRETS` | Another secrets file (default `config/secrets.env`) |
| `HVACTREND_NO_BACNET=1` | Viewer only: no BACnet, no collector (`serve.py --no-bacnet`) |
