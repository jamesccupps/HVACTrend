#!/usr/bin/env bash
# Run HVACTrend in this terminal (Ctrl+C to stop). To run it in the background and after
# reboots instead, see docs/install.md ("Run as a service").
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "HVACTrend is not installed yet: run ./install.sh first."; exit 1
fi
if [ ! -f config/config.yaml ]; then
  echo "There is no config/config.yaml yet: run .venv/bin/python scripts/configure.py"; exit 1
fi
exec .venv/bin/python scripts/serve.py "$@"
