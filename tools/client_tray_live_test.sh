#!/usr/bin/env bash
# 5C1: tray και παράθυρο Client· ίδια 15 βήματα με Windows (offscreen Qt, loopback).
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=python3
if [[ -x .venv/bin/python ]]; then PYTHON=.venv/bin/python; fi
QT_QPA_PLATFORM=offscreen "$PYTHON" -m unittest tests.test_client_tray tests.test_ui_theme \
    tests.test_reconnect tests.test_failure_scenarios -v
"$PYTHON" tools/client_tray_live_test.py "$@"
