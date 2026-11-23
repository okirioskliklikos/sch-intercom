#!/usr/bin/env bash
# 4.2D1: αυτοματοποιημένη δοκιμή Linux, ίδια 20 βήματα με Windows.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=python3
if [[ -x .venv/bin/python ]]; then PYTHON=.venv/bin/python; fi
"$PYTHON" -m unittest discover -s tests -v
"$PYTHON" tools/manager_role_live_test.py
