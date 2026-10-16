#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/python-env.sh"
.venv/bin/python scripts/prepare-drone4d.py --if-needed
exec .venv/bin/python -m uav_harness.dashboard --sandbox "$@"
