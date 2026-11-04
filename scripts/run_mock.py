#!/usr/bin/env python
"""Run a standalone mock BACnet controller for manual testing (no real hardware).

    python scripts/run_mock.py                       # 127.0.0.1:47809, instance 999001
    python scripts/run_mock.py --address 0.0.0.0:47808 --instance 999001

Then, from another shell, point discovery at it:
    python scripts/discover.py --directed 127.0.0.1:47809 --no-cache

This mirrors the "build the test environment first / mock PXC" discipline. It is a
dev tool, separate from the shipped read-only package.
"""

import argparse
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))          # for `tests.mock_bacnet`
sys.path.insert(0, str(_ROOT / "src"))

from tests.mock_bacnet import serve_forever  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Run a mock BACnet controller.")
    p.add_argument("--address", default="127.0.0.1:47809")
    p.add_argument("--instance", type=int, default=999001)
    p.add_argument("--name", default="MockPXC")
    args = p.parse_args()
    try:
        asyncio.run(serve_forever(args.instance, args.name, args.address))
    except KeyboardInterrupt:
        print("\nmock stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
