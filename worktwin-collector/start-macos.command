#!/bin/zsh
set -e
cd "$(dirname "$0")"
if [[ ! -d .venv ]]; then
  echo "首次启动：准备 Python 环境"
  python3 -m venv .venv
fi
source .venv/bin/activate
python -m pip install -e .
echo "本地管理界面：http://127.0.0.1:8765"
python -m worktwin serve --open
