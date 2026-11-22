#!/usr/bin/env bash
# Αυτόματη πραγματική δοκιμή 4.2C2 (ίδια 8 βήματα με το .ps1) στο Linux ανάπτυξης.
# Κύριο σενάριο: tools\manager_key_live_test.ps1 σε Windows.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=python3
if [[ -x .venv/bin/python ]]; then PYTHON=.venv/bin/python; fi
"$PYTHON" -m unittest discover -s tests -p test_manager_key.py -v
"$PYTHON" tools/manager_key_live_test.py
