#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -x .venv/bin/python ]]; then
  factory_python=.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
  factory_python=python3
else
  echo "找不到 Python 3；请先运行 bash scripts/setup.sh。" >&2
  exit 127
fi
"$factory_python" -m unittest discover -s tests -v
"$factory_python" -m compileall -q factory
