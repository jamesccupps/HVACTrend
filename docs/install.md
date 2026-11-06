# Installation

- [Windows](#windows)
- [Linux and macOS](#linux-and-macos)
- [Run as a service](#run-as-a-service)
- [Upgrading](#upgrading)
- [Moving to another computer](#moving-to-another-computer)
- [Uninstalling](#uninstalling)
- [Troubleshooting](#troubleshooting)

## Windows

Windows 10 and 11 are the tested platforms.

1. **Install Python 3.11 or newer** from [python.org](https://www.python.org/downloads/).
   - Tick **Add python.exe to PATH**.
   - The "python" that ships with Windows is a Microsoft Store placeholder: the installer
     skips it, and so should you.
2. **Get HVACTrend.** Either `git clone https://github.com/jamesccupps/HVACTrend.git`, or
   *Code → Download ZIP* on GitHub, then unzip.
   - Put it somewhere permanent, e.g. `C:\HVACTrend`.
   - Avoid a folder that OneDrive or Dropbox syncs: the database must not be copied while
     it is being written.
3. **Double-click `install.bat`.**
   - It creates `.venv` (a private Python environment) and installs into it.
   - It then runs the setup wizard ([getting-started.md](getting-started.md#1-install-and-configure)).
   - Add the AI summary's client at the same time with
     `powershell -ExecutionPolicy Bypass -File install.ps1 -WithAI`.
4. **Allow the network.**
   - The first time HVACTrend binds BACnet, Windows Firewall may ask. Allow it on the network
     your controllers are on: BACnet/IP is UDP port 47808.
   - If the web pages are shared on the network (`api.host: "0.0.0.0"`), also allow inbound
     TCP 8000 (or your `api.port`).

To run it by hand: double-click `start.bat`. It stops when you close the window.

## Linux and macOS

Linux and macOS should work (everything is plain Python), but they are not what HVACTrend is
tested on. Reports are welcome.

```bash
sudo apt install python3.11 python3.11-venv     # Debian/Ubuntu; any Python >= 3.11 will do
git clone https://github.com/jamesccupps/HVACTrend.git /opt/hvactrend
cd /opt/hvactrend
./install.sh            # or ./install.sh --ai for the AI summary
./start.sh              # run by hand; Ctrl+C stops it
```

BACnet/IP uses UDP 47808, which is not a privileged port: no root needed.

## Run as a service

The server should run from boot, and the **watchdog** must run every 5 minutes. The watchdog
is a separate program on purpose, so that a server that has stopped cannot also silence the
alert saying so. It sends every phone notification.

### Windows

Run as administrator:

```
powershell -ExecutionPolicy Bypass -File scripts\windows\install-service.ps1
```

It registers two scheduled tasks that run as SYSTEM:
- **HVACTrend Server**: at boot, and restarted within a minute if it stops.
- **HVACTrend Watchdog**: every 5 minutes.

Check them in Task Scheduler. Remove both with `scripts\windows\uninstall-service.ps1`;
your data stays. Run the install script again after moving the folder.

### Linux (systemd)

Edit `User=` and the paths in `deploy/systemd/*`, then:

```bash
sudo cp deploy/systemd/hvactrend.service deploy/systemd/hvactrend-watchdog.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hvactrend.service hvactrend-watchdog.timer
journalctl -u hvactrend -f
```

## Upgrading

```
git pull                                            # or unzip the new version over the old
.venv\Scripts\python -m pip install -e .            # picks up new dependencies
```

Then restart: close and reopen `start.bat`, or restart the **HVACTrend Server** task
(`Stop-ScheduledTask` / `Start-ScheduledTask`). Your configuration and database are not
touched. The database upgrades itself on start; new settings take their defaults, and
`config/config.example.yaml` shows them.

## From your phone, anywhere (Tailscale)

[Tailscale](https://tailscale.com) joins your own devices into a private network, so your phone
can reach HVACTrend from anywhere without opening anything to the internet.

1. Install Tailscale on the HVACTrend computer and on your phone, signed in to the same
   account. Turn on MagicDNS and HTTPS certificates in the Tailscale admin console (DNS page).
2. On the HVACTrend computer, put its Tailscale name (`<computer>.<tailnet>.ts.net`) in
   `api.allowed_hosts`.
3. Give it an HTTPS address. On that computer:
   ```
   tailscale serve --bg 8000
   ```
   The pages are then at `https://<computer>.<tailnet>.ts.net`, with a real certificate, from
   any of your devices. It lasts across restarts; `tailscale serve reset` undoes it.
4. If every device on your tailnet is yours, add its networks to `api.local_hosts`, so your
   phone gets every page and not only the trend viewer and Realtime:
   ```yaml
   local_hosts: ["100.64.0.0/10", "fd7a:115c:a1e0::/48"]
   ```
   Then restart HVACTrend.

Never use **Tailscale Funnel** for this: it puts the pages on the public internet.

## Moving to another computer

Stop the server, then copy these to the new installation:
- `config/config.yaml`;
- `config/secrets.env`;
- `data/hvactrend.db`.

Or restore the newest backup from `data/backups` instead of the database. Then:
- update `bacnet.local_address` to the new computer's address;
- run `install.bat` there (it keeps an existing config).

## Uninstalling

Remove the service (above) if you installed it, then delete the folder. HVACTrend writes
nothing outside it, except the off-site backup folder if you set one.

## Troubleshooting

**Nothing answers `discover.py`.**
- **Address:** check `bacnet.local_address` is this computer's address on the controls
  network (`ipconfig`), with the right prefix.
- **Firewall:** UDP 47808 must be allowed inbound.
- **Port in use:**
  - The HVACTrend server must not be running; only one program can use the BACnet port.
  - The same goes for other BACnet software on this computer (a workstation, a BACnet
    explorer).
- **A controller on another subnet:** try `--directed 192.0.2.10` to ask it directly.
  Broadcasts do not cross routers unless a BBMD forwards them.

**"bacnet.local_address is still a placeholder".** `config.yaml` still has `<COLLECTOR_IP>` or
`<LOGGER_DEVICE_INSTANCE>`. Run the wizard: `.venv\Scripts\python scripts\configure.py`.

**The pages open but nothing is logged.**
- Point Selection: are any points ticked?
- The banner at the top and `logs/hvactrend.log` say whether BACnet bound.
- `/health` should show `"collector_running": true`.

**Readings stop for one controller.** The watchdog alerts you after 30 minutes. Check the
controller and its network. The Point Coverage page and the per-controller poll health show
what is failing.

**The browser shows an old page after an upgrade.** Hard-refresh (Ctrl+F5).

**Where are the logs?** `logs/hvactrend.log`, rotated at 5 MB. The watchdog prints only when
something is wrong; under Task Scheduler see its *Last Run Result*.
