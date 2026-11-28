#!/usr/bin/env bash
# 5C2: «Αλλαγή…» αίθουσας/ονόματος· ίδια 16 βήματα με Windows (offscreen Qt, loopback).
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=python3
if [[ -x .venv/bin/python ]]; then PYTHON=.venv/bin/python; fi
QT_QPA_PLATFORM=offscreen "$PYTHON" -m unittest tests.test_change_request tests.test_change_request_director tests.test_client_tray tests.test_enrollment tests.test_security_page tests.test_keepalive tests.test_ui_scale -v
"$PYTHON" tools/change_request_live_test.py "$@"
