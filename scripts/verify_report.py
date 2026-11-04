"""Check what a report SAYS against what the data SHOWS.

    python scripts/verify_report.py                 # the latest report
    python scripts/verify_report.py --id 42
    python scripts/verify_report.py --all --limit 5

Read-only. Exit code 0 = PASS, 1 = WARN, 2 = FAIL, so it can gate a scheduled run.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hvactrend.ai.verify import verify_report  # noqa: E402
from hvactrend.config import ConfigError, load_config  # noqa: E402
from hvactrend.storage.sqlite_dal import SQLiteDAL  # noqa: E402

_ICON = {"pass": "  ok ", "warn": " WARN", "fail": " FAIL"}


def _show(r: dict) -> str:
    if r.get("error"):
        print(r["error"], file=sys.stderr)
        return "fail"
    print("=" * 78)
    print(f"report #{r['report_id']} ({r['kind']})  "
          f"{r['window_local'][0]} -> {r['window_local'][1]}"
          + ("   [SPANS TWO DAYS]" if r["spans_days"] else ""))
    print(f"VERDICT: {r['verdict']}   {r['checked']} checks · "
          f"{r['failed']} failed · {r['warned']} warned")
    print("=" * 78)
    seen = set()
    for want in ("fail", "warn", "pass"):
        for c in r["checks"]:
            if c["status"] != want:
                continue
            key = (c["check"], c["detail"][:80])
            if key in seen:
                continue                     # same claim in prose and in its finding
            seen.add(key)
            print(f"[{_ICON[want]}] {c['check']}")
            print(f"          {c['detail']}")
            if want != "pass":
                print(f"          claim: \"{c['claim'][:150]}\"")
    print()
    return r["verdict"].lower()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--id", type=int, help="verify one report by id")
    ap.add_argument("--all", action="store_true", help="verify the most recent reports")
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    dal = SQLiteDAL(cfg.storage.path, cfg.get("storage").get("schema_file"))
    try:
        if args.id:
            ids = [args.id]
        else:
            n = args.limit if args.all else 1
            ids = [r[0] for r in dal._conn.execute(
                "SELECT id FROM reports WHERE status='complete' "
                "ORDER BY id DESC LIMIT ?", (n,))]
        if not ids:
            print("no completed reports to verify")
            return 0
        worst = "pass"
        for rid in ids:
            v = _show(verify_report(dal, rid))
            if v == "fail" or (v == "warn" and worst == "pass"):
                worst = v
        return {"pass": 0, "warn": 1, "fail": 2}[worst]
    finally:
        dal.close()


if __name__ == "__main__":
    raise SystemExit(main())
