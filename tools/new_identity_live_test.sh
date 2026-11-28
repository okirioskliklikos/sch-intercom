#!/usr/bin/env bash
# 5C3: νέα ταυτότητα, κλώνοι με σύσταση, ανάκληση κλώνων· ίδια 16 βήματα με Windows (offscreen Qt, loopback).
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=python3
if [[ -x .venv/bin/python ]]; then PYTHON=.venv/bin/python; fi
QT_QPA_PLATFORM=offscreen "$PYTHON" -m unittest tests.test_new_identity tests.test_enrollment tests.test_security_page tests.test_keepalive tests.test_change_request tests.test_registry_seen tests.test_clone_and_full -v
"$PYTHON" tools/new_identity_live_test.py "$@"
