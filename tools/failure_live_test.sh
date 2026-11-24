#!/usr/bin/env bash
# 4.3B: αυτοματοποιημένη δοκιμή Linux, ίδια 16 βήματα με Windows (οι έλεγχοι έντασης γράφουν SKIP).
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=python3
if [[ -x .venv/bin/python ]]; then PYTHON=.venv/bin/python; fi
"$PYTHON" -m unittest discover -s tests -v
"$PYTHON" tools/failure_live_test.py
