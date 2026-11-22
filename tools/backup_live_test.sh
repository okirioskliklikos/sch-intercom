#!/usr/bin/env bash
# Αυτόματη δοκιμή backup σε προσωρινούς φακέλους Linux.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=python3
if [[ -x .venv/bin/python ]]; then PYTHON=.venv/bin/python; fi
"$PYTHON" -m unittest discover -s tests -p test_backup.py -v
"$PYTHON" tools/backup_live_test.py
