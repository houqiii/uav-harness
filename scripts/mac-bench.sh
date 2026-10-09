#!/usr/bin/env bash
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"

# Prefer the version used by the project's lock file. Never touch system Python.
if [[ ! -x .venv/bin/python ]]; then
  bench_python="${PYTHON_BIN:-}"
  if [[ -z "$bench_python" ]]; then
    for candidate in python3.12 python3.11 /opt/homebrew/opt/python@3.12/bin/python3.12 /usr/local/opt/python@3.12/bin/python3.12 /Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 python3; do
      if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        bench_python="$candidate"
        break
      fi
    done
  fi
  if [[ -z "$bench_python" ]] && command -v uv >/dev/null 2>&1; then
    bench_python="$(uv python find --no-project --no-python-downloads 3.12 2>/dev/null || true)"
    if [[ -z "$bench_python" ]]; then
      uv python install 3.12
      bench_python="$(uv python find --no-project --no-python-downloads 3.12)"
    fi
  fi
  if [[ -z "$bench_python" ]] && command -v brew >/dev/null 2>&1; then
    echo '正在通过 Homebrew 安装 Python 3.12…'
    brew install python@3.12
    bench_python="$(brew --prefix python@3.12)/bin/python3.12"
  fi
  if [[ -z "$bench_python" ]]; then
    echo '需要 Python 3.11+。请先安装 Python 3.12，或安装 Homebrew/uv 后重新运行。' >&2
    exit 1
  fi
  "$bench_python" -m venv .venv
fi

if ! .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo '现有 .venv 的 Python 低于 3.11，请换用 Python 3.12 创建虚拟环境。' >&2
  exit 1
fi

bench_digest="$(shasum -a 256 requirements.lock pyproject.toml | shasum -a 256 | cut -d ' ' -f 1)"
if [[ ! -f .venv/.bench-installed ]] || [[ "$(cat .venv/.bench-installed)" != "$bench_digest" ]]; then
  .venv/bin/python -m pip install -r requirements.lock
  .venv/bin/python -m pip install -e . --no-deps
  printf '%s\n' "$bench_digest" > .venv/.bench-installed
fi

exec .venv/bin/python -m uav_harness.bench_console "$@"
