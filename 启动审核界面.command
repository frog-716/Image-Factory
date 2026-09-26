#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/python -m factory --config config.local.json --state var/live \
  human-ui --run V1-DEMO-KIDS-001 --port 8790 --open
