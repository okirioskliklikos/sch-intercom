#!/usr/bin/env bash
# Φάση 5B2b: αυτόματη πραγματική δοκιμή εναλλαγής έκδοσης και rollback (13 βήματα), loopback.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=python3
if [[ -x .venv/bin/python ]]; then PYTHON=.venv/bin/python; fi
"$PYTHON" -m unittest tests.test_update_switch tests.test_updates tests.test_client_install -v
"$PYTHON" tools/update_install_live_test.py
