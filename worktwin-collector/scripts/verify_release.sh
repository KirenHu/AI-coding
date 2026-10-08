#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
python -m pytest -q
PYTHONPATH=. python scripts/acceptance_benchmark.py
PYTHONPATH=. python scripts/http_acceptance.py
if command -v node >/dev/null 2>&1; then node --check worktwin/static/app.js; fi
printf '\nWorkTwin v0.5 core + HTTP verification: PASS\n'
