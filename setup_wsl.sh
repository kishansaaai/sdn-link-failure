#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$(readlink -f -- "$0")")"
python_bin="${PYTHON_BIN:-python3.10}"
"$python_bin" -m venv .venv
.venv/bin/python -m pip install pip==24.0 setuptools==57.5.0 wheel==0.42.0
.venv/bin/python -m pip install --no-build-isolation -r requirements.txt -r requirements-dev.txt
printf '%s\n' 'Ready. Start with .venv/bin/python start_ryu.py'
