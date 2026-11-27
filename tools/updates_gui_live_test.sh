#!/usr/bin/env bash
# Αυτόματη πραγματική δοκιμή Ρυθμίσεις → «Ενημερώσεις» (ίδια 15 βήματα με το .ps1) στο Linux
# ανάπτυξης. Κύριο σενάριο: tools\updates_gui_live_test.ps1 σε Windows (ο Manager).
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=python3
if [[ -x .venv/bin/python ]]; then PYTHON=.venv/bin/python; fi
"$PYTHON" -m unittest tests.test_updates_page tests.test_backup_updates tests.test_updates -v
"$PYTHON" tools/updates_gui_live_test.py
