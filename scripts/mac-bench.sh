#!/usr/bin/env bash
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"

# Prefer the version used by the project's lock file. Never touch system Python.
if [[ ! -x .venv/bin/python ]]; then
  bench_python="${PYTHON_BIN:-}"
  if [[ -z "$bench_python" ]]; then
    for candidate in python3.12 python3.11 python3; do
      if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        bench_python="$candidate"
        break
      fi
    done
  fi
  if [[ -z "$bench_python" ]]; then
    echo '需要 Python 3.11+，建议先运行 brew install python@3.12，然后重新启动本脚本。' >&2
    exit 1
  fi
  "$bench_python" -m venv .venv
fi

bench_digest="$(shasum -a 256 requirements.lock pyproject.toml | shasum -a 256 | cut -d ' ' -f 1)"
if [[ ! -f .venv/.bench-installed ]] || [[ "$(cat .venv/.bench-installed)" != "$bench_digest" ]]; then
  .venv/bin/python -m pip install -r requirements.lock
  .venv/bin/python -m pip install -e . --no-deps
  printf '%s\n' "$bench_digest" > .venv/.bench-installed
fi

exec .venv/bin/python -m uav_harness.bench_console "$@"
