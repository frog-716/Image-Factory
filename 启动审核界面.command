#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PORT="$(.venv/bin/python -c '
import socket

for port in range(8790, 8891):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            continue
        print(port)
        break
else:
    raise SystemExit("找不到可用的本机审核端口（8790–8890）。")
')"
exec .venv/bin/python -m factory --config config.local.json --state var/live \
  human-ui --port "$PORT" --open
