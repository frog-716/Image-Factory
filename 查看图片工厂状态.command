#!/bin/zsh
set -euo pipefail
PROJECT_ROOT=${0:A:h}
cd "$PROJECT_ROOT"
exec "$PROJECT_ROOT/.venv/bin/python" -m factory \
  --config "$PROJECT_ROOT/config.local.json" \
  --state "$PROJECT_ROOT/var/live" \
  runner-status
