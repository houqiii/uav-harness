#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/python-env.sh"
dashboard_sandbox=1
for dashboard_arg in "$@"; do
  case "$dashboard_arg" in
    --no-sandbox) dashboard_sandbox=0 ;;
    --sandbox) dashboard_sandbox=1 ;;
  esac
done
if [[ "$dashboard_sandbox" == 1 ]]; then
  .venv/bin/python scripts/prepare-drone4d.py --if-needed
fi
exec .venv/bin/python -m uav_harness.dashboard "$@"
