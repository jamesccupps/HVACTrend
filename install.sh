#!/usr/bin/env bash
# Install HVACTrend on Linux or macOS: a private Python environment (.venv), the program,
# and your building's configuration (the setup wizard writes config/config.yaml).
#
#   ./install.sh            # install, then run the setup wizard
#   ./install.sh --ai       # also the optional AI-summary client
#   ./install.sh --no-wizard
#
# Nothing is installed system-wide and no root is needed. To keep it running after reboots,
# see docs/install.md ("Run as a service": systemd unit files are in deploy/systemd/).
set -euo pipefail
cd "$(dirname "$0")"

AI=0
WIZARD=1
for arg in "$@"; do
  case "$arg" in
    --ai) AI=1 ;;
    --no-wizard) WIZARD=0 ;;
    *) echo "unknown option: $arg"; exit 2 ;;
  esac
done

PY=""
for cand in python3.13 python3.12 python3.11 python3 python; do
  if command -v "$cand" >/dev/null 2>&1 &&
     "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$cand"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11 or newer was not found. Install it (e.g. 'sudo apt install python3.11"
  echo "python3.11-venv', or from python.org) and run this again."
  exit 1
fi
echo "HVACTrend installer - using $("$PY" -c 'import sys; print(sys.executable)')"

if [ ! -x .venv/bin/python ]; then
  echo "Creating the private environment (.venv)..."
  "$PY" -m venv .venv
fi
echo "Installing HVACTrend and its dependencies (a minute or two)..."
.venv/bin/python -m pip install --quiet --upgrade pip
if [ "$AI" = 1 ]; then .venv/bin/python -m pip install --quiet -e ".[ai]"
else .venv/bin/python -m pip install --quiet -e .; fi
echo "Installed."

if [ "$WIZARD" = 1 ]; then
  echo
  exec .venv/bin/python scripts/configure.py
fi
echo "Next: .venv/bin/python scripts/configure.py   (writes config/config.yaml)"
