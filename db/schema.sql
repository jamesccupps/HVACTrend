-- HVACTrend — SQLite schema (Phase 0 scaffold)
-- Pinned per trend-logger-plan.md §4 (data model) and §11 (settings).
-- This file is the SINGLE SOURCE OF TRUTH for the schema. The DAL applies it
-- verbatim (scripts/init_db.py). Migrations append to schema_migrations; never
-- edit an already-applied migration in place.
--
-- Conventions
--   * Time columns are INTEGER Unix epoch SECONDS in UTC. Display-local is a
--     presentation concern only.
--   * Time-series is LONG / NARROW (samples), never a column-per-point wide table,
--     so arbitrary points are added with zero schema change.
--   * Real BACnet values -> value_num; Boolean / Enumerated / Multistate also carry
--     value_text (resolved state-text). status-flags / reliability / out-of-service
--     are stored EVERY sample — this is what auto-detects the stuck-sensor
--     / sentinel case (the -58 degF reading).
--
-- READ-ONLY posture: nothing in this
-- schema stores, represents, or enables a write / command / priority-array entry
-- to a BACnet device. There is deliberately NO command or priority table.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ── Meta / migrations ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_utc INTEGER NOT NULL,
    note        TEXT
);

-- ── Topology: Site -> Building -> System -> Device -> Point ─────────
-- Built multi-site-ready from day one: one database can hold several buildings.
CREATE TABLE IF NOT EXISTS sites (
    id    INTEGER PRIMARY KEY,
    code  TEXT NOT NULL UNIQUE,                 -- e.g. 'MAIN'
    name  TEXT NOT NULL,
    tz    TEXT NOT NULL DEFAULT 'America/New_York',  -- display timezone
    notes TEXT
);

CREATE TABLE IF NOT EXISTS buildings (
    id      INTEGER PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    code    TEXT NOT NULL,
    name    TEXT NOT NULL,
    notes   TEXT,
    UNIQUE (site_id, code)
);

-- A "system" is a LOGICAL equipment grouping (AHU-1, Condenser Loop, Boiler Plant).
-- One Siemens PXC hosts MANY systems, so system is NOT 1:1 with device.
CREATE TABLE IF NOT EXISTS systems (
    id          INTEGER PRIMARY KEY,
    building_id INTEGER NOT NULL REFERENCES buildings(id) ON DELETE CASCADE,
    code        TEXT NOT NULL,                  -- 'AHU-1', 'COND-LOOP', 'BOILER-PLANT'
    name        TEXT NOT NULL,
    kind        TEXT,                           -- 'ahu' | 'tower' | 'boiler' | 'pump' | 'hv' | ...
    notes       TEXT,
    UNIQUE (building_id, code)
);

-- A BACnet device (controller). Addressed by Device Instance; the stack handles
-- routing. Routed Trane UCs carry a remote network number + MSTP MAC and
-- reference the SC/PXC that routes to them.
CREATE TABLE IF NOT EXISTS devices (
    id              INTEGER PRIMARY KEY,
    site_id         INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    device_instance INTEGER NOT NULL,           -- BACnet Device object instance
    name            TEXT,
    address         TEXT,                       -- BACnet address string (IP, or 'net:mac')
    network_number  INTEGER,                    -- NULL = local network; else remote (routed)
    mac_address     TEXT,                       -- MSTP MAC for routed devices
    router_instance INTEGER,                    -- device_instance of the routing SC/PXC, if routed
    vendor_id       INTEGER,
    vendor_name     TEXT,
    model_name      TEXT,
    firmware        TEXT,
    is_router       INTEGER NOT NULL DEFAULT 0, -- 1 for SC1/SC2/PXC acting as a BACnet router
    rpm_supported   INTEGER,                    -- capability flag; NULL = unknown
    poll_budget_per_min INTEGER,                -- per-trunk read budget when this device routes MS/TP
                                                -- (SC1/SC2); NULL = uncapped. MSTP protection
    segmentation    INTEGER,                    -- BACnet segmentation-supported enum
    last_seen_utc   INTEGER,
    created_utc     INTEGER NOT NULL,
    notes           TEXT,
    UNIQUE (site_id, device_instance)
);
CREATE INDEX IF NOT EXISTS idx_devices_site ON devices(site_id);

-- Points. `source` distinguishes real BACnet objects from derived/virtual and
-- weather points so §6 (weather) and §7 (virtual points) live in the SAME
-- time-series as real ones, right alongside them.
CREATE TABLE IF NOT EXISTS points (
    id               INTEGER PRIMARY KEY,
    device_id        INTEGER REFERENCES devices(id) ON DELETE CASCADE,  -- NULL for derived/weather
    system_id        INTEGER REFERENCES systems(id) ON DELETE SET NULL,
    source           TEXT NOT NULL DEFAULT 'bacnet',   -- 'bacnet' | 'derived' | 'weather'
    object_type      TEXT,                    -- 'analogInput','binaryValue','trendLog', ...
    object_instance  INTEGER,
    name             TEXT,                    -- BACnet object-name
    display_name     TEXT,                    -- operator override for the UI
    description      TEXT,
    units            INTEGER,                 -- BACnet EngineeringUnits enum
    units_text       TEXT,                    -- resolved label ('degrees-Fahrenheit')
    poll_interval_s  INTEGER,                 -- NULL -> use global interval
    cov_enabled      INTEGER NOT NULL DEFAULT 0,   -- optional COV, off by default
    trendlog_harvest INTEGER NOT NULL DEFAULT 0,   -- ReadRange harvest on/off
    enabled          INTEGER NOT NULL DEFAULT 1,   -- logging on/off
    -- Semantic role (DAT, MAT, RAT, DAT_ACTIVE_SP, HTG_VALVE, COOLING_STAGES, ...),
    -- classified from the object name by ai/roles.py. Drives physics-aware analysis
    -- (derived metrics, correlation checks, mode rules). NULL = unclassified/OTHER.
    role             TEXT,
    -- Prefer this point as the canonical source for its role (e.g. the wired |dac-N_UIP~
    -- sensor over a logical twin). Set by the role classifier; read-only analysis flag.
    role_canonical   INTEGER NOT NULL DEFAULT 0,
    -- Deterministic out-of-range alarm limits.
    -- These are OUR computed limits, never writes to the device. May be seeded from the
    -- object's BACnet high-limit/low-limit properties during discovery (still read-only).
    alarm_enabled    INTEGER NOT NULL DEFAULT 0,   -- out-of-range alarm on/off for this point
    alarm_hi         REAL,                    -- high limit; value above -> out-of-range
    alarm_lo         REAL,                    -- low limit; value below -> out-of-range
    alarm_deadband   REAL,                    -- hysteresis to stop alarm chatter at the limit
    spec             TEXT,                    -- JSON: derived formula / weather map / COV params
    created_utc      INTEGER NOT NULL,
    UNIQUE (device_id, object_type, object_instance)
);
CREATE INDEX IF NOT EXISTS idx_points_device ON points(device_id);
CREATE INDEX IF NOT EXISTS idx_points_system ON points(system_id);

-- Enumerated / multistate / binary state-text.
CREATE TABLE IF NOT EXISTS point_enum_states (
    point_id    INTEGER NOT NULL REFERENCES points(id) ON DELETE CASCADE,
    state_value INTEGER NOT NULL,            -- present-value index
    state_text  TEXT NOT NULL,
    PRIMARY KEY (point_id, state_value)
) WITHOUT ROWID;

-- ── Time-series ────────────────────────────────────────────────────
-- Long/narrow, clustered by (point_id, ts_utc) for fast per-point range scans.
-- Composite PK dedupes on retry (idempotent writes at a fixed interval).
CREATE TABLE IF NOT EXISTS samples (
    point_id       INTEGER NOT NULL REFERENCES points(id) ON DELETE CASCADE,
    ts_utc         INTEGER NOT NULL,         -- epoch seconds, UTC
    value_num      REAL,                     -- numeric present-value
    value_text     TEXT,                     -- resolved state-text (enum/binary/multistate)
    status_flags   TEXT,                     -- BACnet status-flags as text 'in-alarm;fault'
    reliability    INTEGER,                  -- BACnet reliability enum
    out_of_service INTEGER,                  -- 0/1
    quality        TEXT NOT NULL DEFAULT 'good',  -- derived: good|stale|fault|comm-loss
    PRIMARY KEY (point_id, ts_utc)
) WITHOUT ROWID;
-- Speeds retention pruning ("DELETE ... WHERE ts_utc < cutoff") across all points.
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts_utc);

-- Downsampled rollups: min/max/avg/last/count per point per bucket.
CREATE TABLE IF NOT EXISTS samples_5m (
    point_id   INTEGER NOT NULL REFERENCES points(id) ON DELETE CASCADE,
    bucket_utc INTEGER NOT NULL,             -- start of 5-min bucket, epoch seconds UTC
    v_min  REAL,
    v_max  REAL,
    v_avg  REAL,
    v_last REAL,
    n      INTEGER NOT NULL,
    -- Samples of ANY quality in this bucket; `n` counts only 'good'.
    -- n_total > 0 AND n = 0 means RECORDED AND FAULTED THROUGHOUT, which is
    -- information -- and is not the same thing as no row at all. Without it, a
    -- failed sensor becomes indistinguishable from a collector outage the moment
    -- raw retention expires.
    n_total    INTEGER,
    PRIMARY KEY (point_id, bucket_utc)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS samples_1h (
    point_id   INTEGER NOT NULL REFERENCES points(id) ON DELETE CASCADE,
    bucket_utc INTEGER NOT NULL,             -- start of 1-hour bucket, epoch seconds UTC
    v_min  REAL,
    v_max  REAL,
    v_avg  REAL,
    v_last REAL,
    n      INTEGER NOT NULL,
    -- Samples of ANY quality in this bucket; `n` counts only 'good'.
    -- n_total > 0 AND n = 0 means RECORDED AND FAULTED THROUGHOUT, which is
    -- information -- and is not the same thing as no row at all. Without it, a
    -- failed sensor becomes indistinguishable from a collector outage the moment
    -- raw retention expires.
    n_total    INTEGER,
    PRIMARY KEY (point_id, bucket_utc)
) WITHOUT ROWID;

-- ── Equipment metadata: design specs / setpoints / relationships ─
-- e.g. BAC 1500 submittal: design_ewt=98, design_lwt=85, design_ewb=74, approach=11.
CREATE TABLE IF NOT EXISTS equipment_meta (
    id         INTEGER PRIMARY KEY,
    system_id  INTEGER REFERENCES systems(id) ON DELETE CASCADE,
    point_id   INTEGER REFERENCES points(id) ON DELETE CASCADE,  -- optional point-level spec
    key        TEXT NOT NULL,                -- 'design_ewt','design_approach','setpoint'
    value_num  REAL,
    value_text TEXT,
    unit       TEXT,
    source     TEXT,                         -- 'submittal' | 'nameplate' | 'manual'
    notes      TEXT
);
CREATE INDEX IF NOT EXISTS idx_equipment_meta_system ON equipment_meta(system_id);

-- ── AI outputs ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS reports (
    id               INTEGER PRIMARY KEY,
    kind             TEXT NOT NULL,          -- 'daily' | 'weekly' | 'manual'
    period_start_utc INTEGER NOT NULL,
    period_end_utc   INTEGER NOT NULL,
    provider         TEXT,                   -- 'ollama' | 'claude'
    model            TEXT,
    prose            TEXT,                   -- narrative
    status           TEXT NOT NULL DEFAULT 'complete',  -- pending|complete|error
    created_utc      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_kind ON reports(kind, period_end_utc);

-- Structured findings ledger. `fingerprint` is a stable id for the SAME issue so
-- it can be tracked over time.
CREATE TABLE IF NOT EXISTS findings (
    id             INTEGER PRIMARY KEY,
    report_id      INTEGER REFERENCES reports(id) ON DELETE SET NULL,
    system_id      INTEGER REFERENCES systems(id) ON DELETE SET NULL,
    point_id       INTEGER REFERENCES points(id) ON DELETE SET NULL,
    fingerprint    TEXT,                     -- stable dedupe/track key
    severity       TEXT,                     -- 'info'|'low'|'medium'|'high'|'critical'
    timeframe      TEXT,                     -- 'immediate'|'this-week'|'monitor' (action urgency)
    equipment      TEXT,
    metric         TEXT,
    summary        TEXT,
    recommendation TEXT,
    evidence       TEXT,                     -- JSON
    status         TEXT NOT NULL DEFAULT 'open',   -- open|acknowledged|closed
    first_seen_utc INTEGER,
    last_seen_utc  INTEGER,
    created_utc    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_findings_fingerprint ON findings(fingerprint);
CREATE INDEX IF NOT EXISTS idx_findings_status ON findings(status, severity);

-- Health scores. A DETERMINISTIC 0-100 score + letter
-- grade per system and a building-level rollup, computed every report run (and on
-- demand for the between-report pulse) from the pre-screen signals. Persisted so the
-- grade TRENDS over time (status tab) and the report can narrate "AC-8 held a C all
-- week". `detail` carries the JSON deduction breakdown (which anomalies cost points),
-- so the score is always explainable. report_id is NULL for a live/on-demand snapshot.
CREATE TABLE IF NOT EXISTS health_scores (
    id          INTEGER PRIMARY KEY,
    report_id   INTEGER REFERENCES reports(id) ON DELETE CASCADE,
    scope       TEXT NOT NULL,               -- 'building' | 'system'
    system_id   INTEGER REFERENCES systems(id) ON DELETE SET NULL,
    system_code TEXT,                        -- denormalized for display without a join
    score       REAL NOT NULL,               -- 0-100
    grade       TEXT,                        -- 'A'..'F'
    detail      TEXT,                        -- JSON: deductions, anomaly counts
    ts_utc      INTEGER NOT NULL             -- snapshot time (report window end, or now)
);
CREATE INDEX IF NOT EXISTS idx_health_scope ON health_scores(scope, system_code, ts_utc);

-- Active alerts (between-report proactive monitoring). The daily/weekly reports only
-- run once a day/week; this table holds high/critical conditions the continuous check
-- raises in between so a fault (fans down, sensor faulted, comm loss) is caught in
-- minutes, not up to 24 h later. Fingerprint-tracked like findings (the same live
-- condition updates in place + bumps last_seen instead of duplicating), auto-resolved
-- when the condition clears. `notified_utc` lets an optional notifier avoid re-sending.
CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,               -- 'fault'|'comm-loss'|'out-of-range'|'collector-stale'|...
    severity     TEXT NOT NULL,               -- 'high'|'critical' (only these alert)
    system_code  TEXT,
    point_id     INTEGER REFERENCES points(id) ON DELETE SET NULL,
    point_name   TEXT,
    message      TEXT NOT NULL,               -- human-readable one-liner
    fingerprint  TEXT NOT NULL,               -- stable dedupe/track key (kind:system:point)
    -- who raised/owns this alert. Each evaluator only auto-resolves ITS OWN source, so the
    -- in-app pre-screen check can't clear the external watchdog's 'collector down' alert
    -- (and vice versa) just because that condition isn't in its own detector set.
    source       TEXT NOT NULL DEFAULT 'prescreen',  -- 'prescreen' | 'watchdog'
    status       TEXT NOT NULL DEFAULT 'open',-- open|resolved
    created_utc  INTEGER NOT NULL,            -- first raised
    last_seen_utc INTEGER NOT NULL,           -- last cycle the condition was still present
    resolved_utc INTEGER,                     -- when it cleared (status->resolved)
    notified_utc INTEGER                      -- when a notification was sent (NULL = not yet)
);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status, severity);
CREATE INDEX IF NOT EXISTS idx_alerts_fingerprint ON alerts(fingerprint);

-- ── Annotations / operational events ──────────────────────────────
CREATE TABLE IF NOT EXISTS annotations (
    id          INTEGER PRIMARY KEY,
    ts_utc      INTEGER NOT NULL,            -- event time (or range start)
    ts_end_utc  INTEGER,                     -- NULL for point-in-time events
    system_id   INTEGER REFERENCES systems(id) ON DELETE SET NULL,
    point_id    INTEGER REFERENCES points(id) ON DELETE SET NULL,
    kind        TEXT NOT NULL DEFAULT 'manual',  -- 'manual' | 'auto-step'
    author      TEXT,
    title       TEXT NOT NULL,
    body        TEXT,
    created_utc INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_annotations_ts ON annotations(ts_utc);

-- ── Tagging: Haystack / Brick-style semantic tags ──────────────────
CREATE TABLE IF NOT EXISTS tags (
    id     INTEGER PRIMARY KEY,
    name   TEXT NOT NULL UNIQUE,            -- 'ahu','discharge','temp','sp','cmd','tower', ...
    scheme TEXT NOT NULL DEFAULT 'custom'   -- 'haystack' | 'brick' | 'custom'
);
CREATE TABLE IF NOT EXISTS point_tags (
    point_id INTEGER NOT NULL REFERENCES points(id) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (point_id, tag_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS system_tags (
    system_id INTEGER NOT NULL REFERENCES systems(id) ON DELETE CASCADE,
    tag_id    INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (system_id, tag_id)
) WITHOUT ROWID;

-- ── Saved views / dashboards ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS views (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL DEFAULT 'trend',   -- 'trend' | 'dashboard' | 'overview'
    spec        TEXT NOT NULL,               -- JSON: point ids, axes, colors, layout
    created_utc INTEGER NOT NULL,
    updated_utc INTEGER NOT NULL
);

-- ── Logger health ─────────────────────────────────────────────
-- Per poll-cycle, per-device: success, latency, dropped reads, method used. This
-- is what lets you TRUST the data (comm-loss shows up here before the AI report).
CREATE TABLE IF NOT EXISTS poll_health (
    id              INTEGER PRIMARY KEY,
    device_id       INTEGER REFERENCES devices(id) ON DELETE CASCADE,
    ts_utc          INTEGER NOT NULL,
    ok              INTEGER NOT NULL,        -- cycle succeeded?
    latency_ms      INTEGER,
    reads_attempted INTEGER,
    reads_ok        INTEGER,
    method          TEXT,                    -- 'rpm' | 'single'
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_poll_health_device ON poll_health(device_id, ts_utc);

-- ── Settings (YAML + DB, plan §3/§11) ────────────────────────────────────────
-- YAML is the version-controlled baseline (config/config.example.yaml). The UI
-- writes runtime overrides here as dotted keys that mirror the YAML structure.
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,           -- dotted, e.g. 'appearance.theme'
    value_json  TEXT NOT NULL,
    updated_utc INTEGER NOT NULL
);

-- ── Learned baselines (ported from the old AHU program's domain model) ────────
-- Per-point NORMAL model, partitioned by occupancy (a fan at 0% is normal at 2 AM,
-- alarming at 2 PM). Computed from OUR OWN accumulated samples by ai/baseline.py on
-- a rolling window (self-refreshing so shoulder-season transitions don't blow up
-- z-scores). This is the deviation reference the pre-screen needs — the alarm_hi/lo
-- limits are hand-set and usually empty; these are learned. Read-only analysis.
CREATE TABLE IF NOT EXISTS baseline (
    point_id     INTEGER NOT NULL REFERENCES points(id) ON DELETE CASCADE,
    mode         TEXT NOT NULL,             -- 'occ' | 'unocc'  (occupancy partition)
    mean         REAL,
    std          REAL,                      -- floored (~0.4) so z-scores stay finite
    n            INTEGER NOT NULL DEFAULT 0, -- good samples in the window
    changed_frac REAL,                      -- n_changed/n_polls; ~0 = stuck/flatline
    long_slope   REAL,                      -- multi-window regression slope = slow drift
    window_start_utc INTEGER,
    window_end_utc   INTEGER,
    updated_utc  INTEGER NOT NULL,
    PRIMARY KEY (point_id, mode)
) WITHOUT ROWID;

-- ── Weather — outdoor conditions for HVAC performance context ───────
-- Typed table (weather has a fixed, known schema) rather than generic samples, and
-- overlaid in the viewer on its own axis, independent of the point picker. wet-bulb /
-- dewpoint / enthalpy are COMPUTED from temp+RH (source-independent), so Open-Meteo
-- (default, no token) and an optional on-site Tempest yield the same derived set.
CREATE TABLE IF NOT EXISTS weather (
    ts_utc      INTEGER PRIMARY KEY,     -- hour-aligned observation time (UTC)
    oat_f       REAL,
    rh_pct      REAL,
    wet_bulb_f  REAL,                    -- computed (Stull) — the real cooling driver
    dewpoint_f  REAL,                    -- computed (Magnus)
    enthalpy    REAL,                    -- Btu/lb dry air (computed)
    wind_mph    REAL,
    solar_wm2   REAL,                    -- global horizontal irradiance
    pressure_mb REAL,
    source      TEXT,                    -- 'open-meteo' | 'tempest'
    created_utc INTEGER NOT NULL
) WITHOUT ROWID;

-- Cross-point relationship baselines (control-loop coupling): the learned linear fit
-- between two role-paired points (setpoint↔DAT, OAT↔MAT, fan-speed↔duct-static). A
-- large residual today = a loop fault a per-point band can't see (fan ramped but
-- pressure didn't follow). x_point/y_point are point ids; kind names the pairing.
CREATE TABLE IF NOT EXISTS baseline_corr (
    id           INTEGER PRIMARY KEY,
    system_id    INTEGER REFERENCES systems(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,             -- 'sp_dat' | 'oat_mat' | 'fan_press' | ...
    x_point_id   INTEGER REFERENCES points(id) ON DELETE CASCADE,
    y_point_id   INTEGER REFERENCES points(id) ON DELETE CASCADE,
    slope        REAL,
    intercept    REAL,
    residual_std REAL,                      -- spread about the fit; the z denominator
    n            INTEGER NOT NULL DEFAULT 0,
    updated_utc  INTEGER NOT NULL,
    UNIQUE (system_id, kind)
);

-- Rule-based alarm incidents. ONE row per
-- condition episode, not per detection: first seen, last breach, the worst tier reached,
-- and how it ended. The old AI layer wrote 1,524 alert rows for 117 conditions. At most one
-- OPEN row per fingerprint (rule:unit:point). Rows that opened together on several units share
-- a group_key, so one cause is one message. `feedback` is the operator's useful / noise mark: the
-- labelled record every later phase is measured against.
CREATE TABLE IF NOT EXISTS incidents (
    id              INTEGER PRIMARY KEY,
    rule            TEXT NOT NULL,          -- rules/catalog.py id, e.g. 'boiler-rise'
    system_code     TEXT NOT NULL,
    point_key       TEXT NOT NULL DEFAULT '',   -- point id for a per-point rule, '' for a unit
    point_label     TEXT,
    fingerprint     TEXT NOT NULL,          -- rule:system:point_key
    tier            TEXT NOT NULL,          -- watch | action | critical (the worst reached)
    status          TEXT NOT NULL DEFAULT 'open',   -- open | resolved
    title           TEXT NOT NULL,
    detail          TEXT,
    started_utc     INTEGER NOT NULL,       -- when the breach began
    opened_utc      INTEGER NOT NULL,       -- when a tier's minutes were up
    last_breach_utc INTEGER,
    resolved_utc    INTEGER,
    resolve_reason  TEXT,                   -- 'cleared' | 'not judged'
    peak            REAL,
    occurrences     INTEGER NOT NULL DEFAULT 1,
    group_key       TEXT,
    notified_utc    INTEGER,                -- last pushed to the phone -- NULL = never
    notified_tier   TEXT,                   -- the tier it was pushed at; a worse one pushes again
    cleared_notified_utc INTEGER,           -- its all-clear pushed
    feedback        TEXT,                   -- 'useful' | 'noise'
    feedback_utc    INTEGER,
    updated_utc     INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_incidents_open_fp ON incidents(fingerprint)
    WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_incidents_opened ON incidents(opened_utc);

-- Who is in control of each logged commandable point (bacnet/priority.py): its BACnet
-- priority array, READ every bacnet.poll.priority_interval_s, never written. The operator,
-- 2026-10-06: would the program know if they had something overridden at the operator
-- workstation? An operator command is a write into one of 16 priority slots (normally 8);
-- the status flag "overridden" does not report it. One row per point: the first slot that
-- is not empty (level 1..16, NULL = every slot empty, the relinquish default) and its value.
CREATE TABLE IF NOT EXISTS point_command (
    point_id     INTEGER PRIMARY KEY REFERENCES points(id) ON DELETE CASCADE,
    has_array    INTEGER NOT NULL DEFAULT 1,   -- 0: the device says it has no priority array
    level        INTEGER,                      -- 1..16; NULL = nothing commanded
    value_num    REAL,
    value_text   TEXT,
    since_utc    INTEGER,                      -- this LEVEL first seen
    since_exact  INTEGER NOT NULL DEFAULT 0,   -- 0: it was already so at the first read
    checked_utc  INTEGER NOT NULL              -- last successful read
);
-- Every change of level, and every value change at an override level: who overrode what,
-- when. The program's own value changes are not logged (they are the present value's trend).
CREATE TABLE IF NOT EXISTS point_command_log (
    point_id    INTEGER NOT NULL REFERENCES points(id) ON DELETE CASCADE,
    ts_utc      INTEGER NOT NULL,
    level       INTEGER,
    value_num   REAL,
    value_text  TEXT,
    PRIMARY KEY (point_id, ts_utc)
) WITHOUT ROWID;
