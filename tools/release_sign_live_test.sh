#!/usr/bin/env bash
# Φάση 5B1: αυτόματη πραγματική δοκιμή υπογραφής εκδόσεων (ίδια 10 βήματα με το .ps1).
# Χρειάζεται internet (embeddable Python + wheels Windows) ή --tree <έτοιμο δέντρο>.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=python3
if [[ -x .venv/bin/python ]]; then PYTHON=.venv/bin/python; fi
"$PYTHON" -m unittest tests.test_release_package -v
"$PYTHON" tools/release_sign_live_test.py "$@"
