#!/usr/bin/env python
"""Create (or upgrade) the HVACTrend SQLite DB from db/schema.sql.

Usage:
    python scripts/init_db.py [--db data/hvactrend.db] [--schema db/schema.sql]

Idempotent: schema.sql uses CREATE TABLE IF NOT EXISTS throughout.
"""

import sys
from pathlib import Path

# Make `hvactrend` importable when run directly from the repo (no install).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hvactrend.storage.sqlite_dal import _cli_init_db  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(_cli_init_db())
