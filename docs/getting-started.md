# Getting started

This takes you from a download to a building that is being logged, with alarms on your phone.
Allow an hour for a first building; most of it is deciding what to log.

Commands are shown for Windows. On Linux or macOS use `.venv/bin/python` instead of
`.venv\Scripts\python`, forward slashes, and `./start.sh` instead of `start.bat`.

## 0. Before you start

- **A computer on the BACnet network.**
  - It needs to be on the same network (VLAN) as your BACnet/IP controllers, or a routed
    path to them.
  - Give it a fixed address: a static IP, or a DHCP reservation.
  - It records only while it is on, so a small always-on PC is ideal.
- **Python 3.11 or newer**, from [python.org](https://www.python.org/downloads/). On
  Windows, tick **Add python.exe to PATH**.
- **Permission to be on that network.** HVACTrend only reads, but it is still a new device
  on the controls network. Tell whoever looks after the BAS.

Want to look around first? `scripts/demo.py` runs HVACTrend on a made-up building (see the
README).

## 1. Install and configure

Double-click **`install.bat`** (Linux/macOS: `./install.sh`). It:
- creates a private Python environment in `.venv`;
- installs HVACTrend into it (nothing system-wide, no administrator rights);
- starts the **setup wizard**.

The wizard asks:

| Question | What to answer |
|---|---|
| Building name, short code, city | Anything. The name appears on the pages and in the AI summary. |
| Which address is on the BACnet network | The computer's address on the controls network, from the list it shows |
| Prefix length | Usually 24 (a 255.255.255.0 mask). Check `ipconfig` if unsure. |
| BACnet device instance | A number no device on your network uses. The suggested random one almost always is free. |
| Web pages | "This computer only" to start. You can open them to the network later. |
| Latitude / longitude | Optional: free outdoor weather from Open-Meteo |

It writes `config\config.yaml` and creates the database in `data\`. You can change anything
in `config.yaml` later; [configuration.md](configuration.md) explains every setting.

## 2. Find your controllers

The server must not be running for this (only one program can use the BACnet port):

```
.venv\Scripts\python scripts\discover.py --identify
```

It sends a Who-Is and lists every device that answers: instance, address, name, vendor and
model. Note the instances of the controllers that run your equipment. If nothing answers, see
[install.md](install.md#troubleshooting).

## 3. Tell HVACTrend which controller runs which unit

Open `config\config.yaml` and fill in the `equipment` section. For example:

```yaml
equipment:
  device_systems:                 # a controller that runs one unit
    1001: "AHU-1"
    1002: "AHU-2"
    1010: {system: "RTU-1", kind: ahu}
  point_systems:                  # a plant controller that runs several units
    - {device: 2001, match: "*BLR1*", system: "BLR-1", kind: plant}
    - {device: 2001, match: "*BLR2*", system: "BLR-2", kind: plant}
```

[commissioning.md](commissioning.md) covers every form, including Trane Tracer SC
supervisory controllers. Then enumerate the controllers and group their points:

```
.venv\Scripts\python scripts\commission.py
```

It reads every object on each controller (a big controller takes a few minutes) and reports
how many points landed in each unit. Re-run it whenever you change the map; it keeps your
selections.

## 4. Start it and choose what to log

Double-click **`start.bat`** (Linux/macOS: `./start.sh`). Your browser opens on the pages;
the address is <http://localhost:8000> unless you chose another port.

1. **Point Selection**:
   1. Pick a unit on the left.
   2. Tick the points to log. Most people start with temperatures, setpoints, fan speeds and
      statuses, valves and dampers, and duct static.
   3. **Read visible values** reads them live, so you can check you have the right ones.
   4. To have HVACTrend suggest a starting set instead, run
      `.venv\Scripts\python scripts\select_trend_points.py`. It is a dry run; add `--commit`
      to apply.
2. Click **⟳ Rebuild model**. It classifies the new points (discharge air, mixed air, fan
   speed...), and the dashboard and alarm rules work from that.
3. **Point Coverage** shows, unit by unit, which alarm rules can judge it and which points
   are still missing for the rest.

Readings start arriving within two minutes. Open **Trend Viewer** and search for a point.

## 5. Phone notifications

On the **Settings** page, under *Alerts → your phone*:
1. Install the **ntfy** app (App Store / Google Play).
2. Click **Generate a private topic**, and keep the page open.
3. In the app, tap **+**, type the topic exactly as shown, leave the server as ntfy.sh, and
   subscribe.
4. Click **Send test message**. It should arrive within seconds.

The watchdog sends the alerts (next step); [alerts.md](alerts.md) lists what you will get
and how to tune it.

## 6. Keep it running

`start.bat` runs HVACTrend only while that window is open. To run it from boot, in the
background, with the watchdog every five minutes, run this as administrator:

```
powershell -ExecutionPolicy Bypass -File scripts\windows\install-service.ps1
```

On Linux, use the systemd units in `deploy/systemd/` ([install.md](install.md)).

## 7. Optional extras

- **Let others view the trends.**
  - Set `api.host: "0.0.0.0"` in `config.yaml` and restart.
  - Others on the network then open `http://<this computer>:8000` and get the trend viewer
    only.
  - See [security.md](security.md).
- **An off-computer backup.** Set `storage.backup.offsite_dir` to a folder that syncs
  elsewhere (OneDrive, Google Drive, a NAS share).
- **The AI summary.**
  - Install the extra: `.venv\Scripts\python -m pip install -e ".[ai]"`.
  - Enter an Anthropic API key on the Settings page.
  - Set `ai.enabled: true`.
- **Site knowledge.** Tell HVACTrend about points it should ignore, or overrides that are on
  purpose ([commissioning.md](commissioning.md#site-knowledge)).
