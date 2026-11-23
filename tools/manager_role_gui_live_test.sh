#!/usr/bin/env bash
# Αυτόματη πραγματική δοκιμή GUI Κύριου/Εφεδρικού (ίδια 16 βήματα με το .ps1) στο Linux
# ανάπτυξης. Κύριο σενάριο: tools\manager_role_gui_live_test.ps1 σε Windows.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=python3
if [[ -x .venv/bin/python ]]; then PYTHON=.venv/bin/python; fi
"$PYTHON" -m unittest discover -s tests -p test_manager_role_gui.py -v
"$PYTHON" tools/manager_role_gui_live_test.py
