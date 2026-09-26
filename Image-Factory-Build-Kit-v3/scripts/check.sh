#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Prefer user's existing virtualenv. Never delete or recreate it.
PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
  for candidate in "$ROOT/../.venv/bin/python" python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1; then
      PY="$candidate"; break
    fi
  done
fi
if [[ -z "$PY" ]] || ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)'; then
  printf '未找到 Python 3.10+。让 Codex 选择项目现有 .venv 的 Python，或用 PYTHON=/完整路径/python bash scripts/check.sh。不要删除旧 .venv。\n' >&2
  exit 2
fi
cd "$ROOT"
"$PY" scripts/verify_pack.py
PYTHONPATH="$ROOT/reference" "$PY" -m unittest discover -s reference/tests -v
