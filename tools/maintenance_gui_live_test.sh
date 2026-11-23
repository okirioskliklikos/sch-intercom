#!/usr/bin/env bash
# Αυτόματη πραγματική δοκιμή «Πιστοποιητικά» + «Συντήρηση» (ίδια 14 βήματα με το .ps1)
# στο Linux ανάπτυξης. Κύριο σενάριο: tools\maintenance_gui_live_test.ps1 σε Windows.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=python3
if [[ -x .venv/bin/python ]]; then PYTHON=.venv/bin/python; fi
"$PYTHON" -m unittest discover -s tests -p test_backup.py -v
"$PYTHON" -m unittest discover -s tests -p test_maintenance.py -v
"$PYTHON" tools/maintenance_gui_live_test.py
