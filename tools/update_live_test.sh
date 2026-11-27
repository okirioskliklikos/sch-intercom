#!/usr/bin/env bash
# Φάση 5B2a: αυτόματη πραγματική δοκιμή διανομής ενημερώσεων (15 βήματα PASS/FAIL), loopback.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=python3
if [[ -x .venv/bin/python ]]; then PYTHON=.venv/bin/python; fi
"$PYTHON" -m unittest tests.test_updates tests.test_release_package tests.test_keepalive -v
"$PYTHON" tools/update_live_test.py
