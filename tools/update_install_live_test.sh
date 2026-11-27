#!/usr/bin/env bash
# Φάσεις 5B2b/5B2c: εναλλαγή έκδοσης, rollback, λήψη μόνο των αρχείων που άλλαξαν (17 βήματα), loopback.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=python3
if [[ -x .venv/bin/python ]]; then PYTHON=.venv/bin/python; fi
"$PYTHON" -m unittest tests.test_update_switch tests.test_updates tests.test_update_delta tests.test_client_install tests.test_release_package -v
"$PYTHON" tools/update_install_live_test.py
