#!/bin/zsh
set -euo pipefail
PROJECT_ROOT=${0:A:h}
cd "$PROJECT_ROOT"
if [[ ! -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
  print -u2 "未找到 .venv/bin/python，请先执行 bash scripts/setup.sh"
  exit 2
fi
exec "$PROJECT_ROOT/.venv/bin/python" -m factory \
  --config "$PROJECT_ROOT/config.local.json" \
  --state "$PROJECT_ROOT/var/live" \
  runner-loop
