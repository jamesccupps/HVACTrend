# Security and safety

HVACTrend runs on a building's controls network, so it is built around two promises: **it
cannot change anything in the building**, and **it does not open the building up to anyone
else**.

## Read-only BACnet

- **Only reads.** The program sends ReadProperty and ReadPropertyMultiple, plus Who-Is for
  discovery, and answers Who-Is and Who-Has about itself with I-Am and I-Have. Nothing else.
  (The guard also allows ReadRange, for a trend-log reader that nothing calls yet.)
- **Allowlist-first.**
  - Every request the BACnet stack sends goes through a guard
    (`src/hvactrend/bacnet/readonly.py`), which sees the actual request. It allows the reads;
    a service it does not recognise is refused, so it fails closed.
  - The application object HVACTrend talks to the network through has **no write method at
    all**: there is nothing in the program that could issue a write.
- **Nothing can write to HVACTrend either.** Like any BACnet device it has a device object,
  which other devices can read. A WriteProperty to it is refused (write access denied), and
  WritePropertyMultiple and SubscribeCOV are rejected. Its own timeouts live on that object, so
  a write from elsewhere on the network could otherwise make every read fail.
- **Disruptive services are blocked unconditionally, with no setting to enable them:**
  - ReinitializeDevice;
  - DeviceCommunicationControl;
  - AtomicWriteFile;
  - CreateObject and DeleteObject;
  - private transfers.

  The program checks this at every start.
- **The config's switches are defence in depth.**
  - `bacnet.safety.allow_writes` and `allow_cov` stay false.
  - They cannot be changed from the web pages.
  - Setting `allow_writes` by hand only logs a warning. Still nothing can write, because there
    is no write method to call.
- **Overrides are read, never written.** HVACTrend reads each commandable point's priority
  array to show who is in control. It never commands, relinquishes or releases a point.
- **Tests pin all of this** (`tests/test_safety.py`). A change that adds a write path fails
  the suite.

Polling load:
- one ReadPropertyMultiple per 10 points;
- one controller at a time, with a pause between them;
- a per-controller time budget.

At the default 2-minute interval that is well within what BACnet/IP controllers and MS/TP
routers handle.

## Network exposure

**Web pages**
- **Default:** `api.host: "127.0.0.1"` means the pages answer only on the computer HVACTrend
  runs on.
- **Shared:** with `api.host: "0.0.0.0"`, other computers on the network can open
  `http://<this computer>:8000`.
- **What others get:** with `api.remote_viewer_only: true` (the default), they get only the
  **trend viewer**, read-only, the **Realtime page**, and the data those need. Everything
  else answers 404 to them:
  - Point Selection, Settings, the dashboard and reports;
  - secrets and backups;
  - any change to the setup.
- **Realtime trends** can be started and stopped from those computers. Starting one only
  reads, through the same read-only stack, and the `realtime` caps limit how many points run
  at once, how many per controller, and for how long.

**Names, other sites, sizes**
- **Only this computer's names.** The pages answer only to this computer's own names and
  addresses (and any in `api.allowed_hosts`); any other name gets 421. That stops a web page
  opened on this computer from re-pointing its own name at 127.0.0.1 (DNS rebinding) to act
  as the operator.
- **No changes from other sites.** A request that changes something is refused when the
  browser says it comes from another site, so a page elsewhere cannot start a report, a
  sweep or a backup. Everything that reads the controllers is a POST for the same reason.
- **Small bodies, no framing.** Request bodies over 512 KB are refused, and the pages may
  not be framed by another site.
- **Not settable from the page:** `api.host`, `api.port`, `api.auth`, `api.remote_viewer_only`,
  `api.local_hosts` and `api.allowed_hosts` are changed in `config.yaml` only.

**Login**
- **Off by default.** The assumption is that the controls network is the boundary: anyone on
  it can already talk BACnet to the controllers directly, which is far more than HVACTrend
  allows.
- **To require one:** set `api.auth.enabled: true` and set a UI token on the Settings page.
  Every change then needs the token.

**Not for the internet.** The web server is plain HTTP and is not hardened for it. Do not
forward a port to it. For access from outside, use the VPN you already use for the BAS.

## Secrets

These are entered on the Settings page, which shows only their last four characters:
- the Anthropic API key;
- the Tempest token;
- the UI token;
- the ntfy topic.

They are kept in `config/secrets.env`, which:
- is excluded by `.gitignore`;
- is excluded from every snapshot and backup archive, which are also scanned for the secret
  *values* before they are kept;
- is never written to `config.yaml` or the database;
- accepts only one printable line per value.

Treat the ntfy topic like a password: anyone who knows it can read your notifications.

## What leaves the computer

| What | Where | When |
|---|---|---|
| Phone notifications | ntfy.sh, or your ntfy server | Only once you set a topic |
| Weather requests | Open-Meteo / WeatherFlow | Only when enabled |
| The AI summary's input: alarm findings, unit statistics, weather | Anthropic | Only when `ai.enabled` and a key is set |
| Backup copies | Your `offsite_dir` folder | Only when set |

Nothing else, and no telemetry.

## Reporting a vulnerability

Please report security issues privately through GitHub's
[private vulnerability reporting](https://github.com/jamesccupps/HVACTrend/security/advisories/new)
rather than a public issue. See [SECURITY.md](../SECURITY.md).
