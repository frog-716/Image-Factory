#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -c 'import sys; assert sys.version_info >= (3,10), "需要 Python 3.10 或更新版本"'
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
printf '\n安装完成。下一步：source .venv/bin/activate\n再运行：python -m factory demo\n此脚本未安装或改动你的 larkcli，未连接飞书，未调用生图模型。\n'
