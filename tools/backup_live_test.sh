#!/usr/bin/env bash
# Αυτόματη πραγματική δοκιμή 4.2C (ίδια 18 βήματα με το .ps1) στο Linux ανάπτυξης.
# Κύριο σενάριο: tools\backup_live_test.ps1 σε Windows.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=python3
if [[ -x .venv/bin/python ]]; then PYTHON=.venv/bin/python; fi
"$PYTHON" -m unittest discover -s tests -p test_backup.py -v
"$PYTHON" tools/backup_live_test.py
