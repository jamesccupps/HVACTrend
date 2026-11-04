#!/usr/bin/env python
"""Run the HVACTrend web app (browser UI + live reads + embedded collector).

    python scripts/serve.py                 # host and port from api.host / api.port in the config
    python scripts/serve.py --port 8080     # or override them here
    python scripts/serve.py --no-bacnet     # viewer only, on a computer off the BACnet network

The default api.host is 127.0.0.1: the web UI is reachable from this computer only. Set it to
0.0.0.0 to let other computers on the network open the trend viewer (read-only; see
docs/security.md).

Binds the one shared BACnet application to bacnet.local_address, so keep that current (give
this computer a static address or a DHCP reservation). Do NOT also run
scripts/run_collector.py at the same time: both would bind the BACnet port.
"""

import argparse
import os
import sys
from pathlib import Path

# Anchor to the repo root so relative config/db/schema paths resolve no matter where
# this is launched from (Task Scheduler defaults the working dir to System32).
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
os.chdir(_REPO)


def _api_config() -> dict:
    """The `api` block of the config, read without validating the rest: serving the UI must
    not depend on the BACnet section being filled in."""
    try:
        import yaml
        path = Path(os.environ.get("HVACTREND_CONFIG", "config/config.yaml"))
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data.get("api") or {}
    except (OSError, ValueError, ImportError):
        return {}


def main() -> int:
    p = argparse.ArgumentParser(description="Serve the HVACTrend web app.")
    p.add_argument("--host", default=None,
                   help="default: api.host in the config (127.0.0.1 = this computer only, "
                        "0.0.0.0 = other computers can open the viewer)")
    p.add_argument("--port", type=int, default=None, help="default: api.port, else 8000")
    p.add_argument("--open", action="store_true", help="open the pages in the browser")
    p.add_argument("--no-bacnet", action="store_true",
                   help="viewer/browse only: skip the BACnet bind + collector "
                        "(trend viewer works off stored samples; live reads 503). "
                        "Lets you view trends on a machine that isn't on the BACnet VLAN.")
    args = p.parse_args()

    if args.no_bacnet:
        # create_app reads this env var to stay in browse-only mode (no UDP bind).
        os.environ["HVACTREND_NO_BACNET"] = "1"

    host, port = args.host, args.port
    if host is None or port is None:
        api = _api_config()
        host = host or str(api.get("host") or "127.0.0.1")
        port = port or int(api.get("port") or 8000)

    import uvicorn
    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}"
    print(f"HVACTrend: {url}"
          + ("  (other computers: this machine's address, read-only viewer)"
             if host == "0.0.0.0" else ""))
    if args.open:
        import threading
        import webbrowser
        threading.Timer(3.0, webbrowser.open, args=(url,)).start()
    uvicorn.run("hvactrend.api.app:create_app", factory=True, host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
