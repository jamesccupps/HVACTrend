#!/usr/bin/env python
"""Set up HVACTrend for this building: writes config/config.yaml and creates the database.

    python scripts/configure.py                  # asks a few questions
    python scripts/configure.py --defaults \\
        --ip 192.168.1.50 --instance 3999001     # no questions (scripted installs)

Everything it writes is plain YAML in config/config.yaml, with the template's comments kept:
change anything there later. Run it again to start over; it asks before replacing an existing
config and keeps the old one as config.yaml.bak.
"""

from __future__ import annotations

import argparse
import random
import re
import socket
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

TEMPLATE = REPO / "config" / "config.example.yaml"
TARGET = REPO / "config" / "config.yaml"


# ── the part that is tested: fill the template, keep its comments ─────────────────────────
def _set(text: str, pattern: str, value: str) -> str:
    """Replace the value matched by group 2 of `pattern` (group 1 and 3 kept)."""
    new, n = re.subn(pattern, lambda m: m.group(1) + value + m.group(3), text, count=1,
                     flags=re.M)
    if n != 1:
        raise ValueError(f"the template has no line for {pattern!r}")
    return new


def _q(s: str) -> str:
    """A YAML double-quoted string."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(template: str, a: dict) -> str:
    """config.yaml text from the template and the answers:
    name, code, location, ip, prefix, instance, lan (bool), port, lat, lon (optional)."""
    t = template
    t = _set(t, r'^(  code: )("MAIN")(\s)', _q(a["code"]))
    t = _set(t, r'^(  name: )("My Building")(\s)', _q(a["name"]))
    t = _set(t, r'^(  location: )("")(\s)', _q(a.get("location") or ""))
    t = _set(t, r'^(  local_address: )("<COLLECTOR_IP>/24")(\s)',
             _q(f"{a['ip']}/{int(a.get('prefix') or 24)}"))
    t = _set(t, r'^(  device_instance: )("<LOGGER_DEVICE_INSTANCE>")(\s)', str(int(a["instance"])))
    t = _set(t, r'^(  building: \{code: "MAIN", name: )("Main building")(\})', _q(a["name"]))
    t = _set(t, r'^(  host: )("127\.0\.0\.1")(\s)', _q("0.0.0.0" if a.get("lan") else "127.0.0.1"))
    t = _set(t, r'^(  port: )(8000)(\s)', str(int(a.get("port") or 8000)))
    if a.get("lat") not in (None, "") and a.get("lon") not in (None, ""):
        t = _set(t, r'^(  open_meteo:\n    enabled: )(false)(\n)', "true")
        t = _set(t, r'^(    latitude: )(null)(\s)', str(float(a["lat"])))
        t = _set(t, r'^(    longitude: )(null)(\s)', str(float(a["lon"])))
    return t


def local_ipv4() -> list[str]:
    """This computer's IPv4 addresses, loopback excluded."""
    found: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in found:
                found.append(ip)
    except OSError:
        pass
    return found


def valid_ipv4(s: str) -> bool:
    parts = s.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


# ── the questions ─────────────────────────────────────────────────────────────────────────
def _ask(prompt: str, default: str = "", check=None) -> str:
    while True:
        got = input(f"{prompt} [{default}]: " if default != "" else f"{prompt}: ").strip()
        got = got or default
        if check is None or check(got):
            return got
        print("  That does not look right; try again.")


def interview() -> dict:
    print(__doc__.split("\n\n")[0].strip().replace("    ", ""))
    print("Press Enter to accept the [default].\n")
    a: dict = {}
    a["name"] = _ask("Building name", "My Building")
    code = re.sub(r"[^A-Za-z0-9]", "", a["name"]).upper()[:8] or "MAIN"
    a["code"] = _ask("Short code for it (letters and digits)", code,
                     lambda s: bool(re.fullmatch(r"[A-Za-z0-9_-]{1,16}", s)))
    a["location"] = _ask("City and state/country, for the AI summary (optional)", "")

    print("\nBACnet network")
    ips = local_ipv4()
    if ips:
        for i, ip in enumerate(ips, 1):
            print(f"  {i}) {ip}")
        pick = _ask("Which address is on the BACnet network? (number, or type an address)",
                    "1", lambda s: (s.isdigit() and 1 <= int(s) <= len(ips)) or valid_ipv4(s))
        a["ip"] = ips[int(pick) - 1] if pick.isdigit() else pick
    else:
        a["ip"] = _ask("This computer's address on the BACnet network", "", valid_ipv4)
    a["prefix"] = int(_ask("Its prefix length (24 means a 255.255.255.0 mask)", "24",
                           lambda s: s.isdigit() and 8 <= int(s) <= 30))
    suggestion = str(random.randint(3_900_000, 4_194_000))
    a["instance"] = int(_ask("A BACnet device instance for HVACTrend, unused on your network",
                             suggestion,
                             lambda s: s.isdigit() and 0 <= int(s) <= 4_194_302))

    print("\nWeb pages")
    print("  1) This computer only (recommended to start)")
    print("  2) Other computers on the network may open the trend viewer (read-only)")
    a["lan"] = _ask("Choose", "1", lambda s: s in ("1", "2")) == "2"
    a["port"] = int(_ask("Port", "8000", lambda s: s.isdigit() and 1 <= int(s) <= 65535))

    print("\nWeather (optional): the building's coordinates, for free Open-Meteo data")
    num = lambda s: s == "" or re.fullmatch(r"-?\d+(\.\d+)?", s) is not None  # noqa: E731
    a["lat"] = _ask("Latitude, e.g. 39.78 (Enter to skip)", "", num)
    a["lon"] = _ask("Longitude, e.g. -89.65", "", num) if a["lat"] else ""
    return a


def write_config(text: str, *, force: bool, interactive: bool) -> bool:
    if TARGET.exists() and not force:
        if not interactive:
            print(f"{TARGET} exists; left as it is (--force replaces it, keeping a .bak).")
            return False
        if input(f"\n{TARGET} exists. Replace it (the old one is kept as config.yaml.bak)? "
                 "[y/N]: ").strip().lower() != "y":
            print("Left it as it is.")
            return False
    if TARGET.exists():
        TARGET.with_suffix(".yaml.bak").write_bytes(TARGET.read_bytes())
    TARGET.write_text(text, encoding="utf-8")
    print(f"\nWrote {TARGET}")
    return True


def create_db() -> None:
    from hvactrend.config import load_config
    from hvactrend.storage.sqlite_dal import SQLiteDAL
    cfg = load_config(str(TARGET))
    path = REPO / cfg.storage.path
    dal = SQLiteDAL(path, REPO / cfg.get("storage").get("schema_file"))
    dal.apply_schema()
    dal.close()
    print(f"Database ready: {path}")


NEXT = """
Next steps (docs/getting-started.md has the details):
  1. Find your controllers (the server must not be running):
         {py} scripts/discover.py --identify
  2. Tell HVACTrend which controller runs which unit: the `equipment` section of
     config/config.yaml (docs/commissioning.md), then
         {py} scripts/commission.py
  3. Start it:  {start}
     and open http://localhost:{port}  ->  Point Selection: tick what to log.
  4. Settings page: phone notifications (ntfy) and, if you want it, the AI summary.
  5. To keep it running after reboots, install it as a service (docs/install.md).
"""


def windows_paths(text: str) -> str:
    """Backslashes in the paths to type, but not in a web address."""
    return re.sub(r"(?<![:/])/(?!/)", lambda m: "\\", text)


def main() -> int:
    p = argparse.ArgumentParser(description="Write config/config.yaml for this building.")
    p.add_argument("--defaults", action="store_true", help="no questions; use the flags below")
    p.add_argument("--name", default="My Building")
    p.add_argument("--code", default="MAIN")
    p.add_argument("--location", default="")
    p.add_argument("--ip")
    p.add_argument("--prefix", type=int, default=24)
    p.add_argument("--instance", type=int)
    p.add_argument("--lan", action="store_true", help="let other computers open the viewer")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--lat")
    p.add_argument("--lon")
    p.add_argument("--force", action="store_true", help="replace an existing config.yaml")
    args = p.parse_args()

    if args.defaults:
        ip = args.ip or (local_ipv4() or [None])[0]
        if not ip or not valid_ipv4(ip):
            print("--defaults needs --ip (this computer's address on the BACnet network).")
            return 2
        a = {"name": args.name, "code": args.code, "location": args.location, "ip": ip,
             "prefix": args.prefix, "instance": args.instance or random.randint(3_900_000, 4_194_000),
             "lan": args.lan, "port": args.port, "lat": args.lat, "lon": args.lon}
    else:
        try:
            a = interview()
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled; nothing written.")
            return 1

    text = render(TEMPLATE.read_text(encoding="utf-8"), a)
    if not write_config(text, force=args.force, interactive=not args.defaults):
        return 1
    create_db()
    win = sys.platform.startswith("win")
    text = NEXT.format(py=".venv/Scripts/python" if win else ".venv/bin/python",
                       start="start.bat" if win else "./start.sh", port=a["port"])
    print(windows_paths(text) if win else text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
