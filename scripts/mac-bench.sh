#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/python-env.sh"
exec .venv/bin/python -m uav_harness.bench_console "$@"
