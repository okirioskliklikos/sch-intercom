#!/usr/bin/env bash
# Αυτόματη πραγματική δοκιμή «Κλίμακα περιβάλλοντος» (ίδια 12 βήματα με το .ps1)
# στο Linux ανάπτυξης. Κύριο σενάριο: tools\ui_scale_live_test.ps1 σε Windows.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=python3
if [[ -x .venv/bin/python ]]; then PYTHON=.venv/bin/python; fi
"$PYTHON" -m unittest tests.test_ui_scale tests.test_ui_theme tests.test_maintenance -v
"$PYTHON" tools/ui_scale_live_test.py
