#!/usr/bin/env bash
# Φάση 5: δοκιμή του Client σε καθαρά Windows (docs/phase5.md §7.1).
#   tools/field_test.sh director     Director δοκιμής (Manager σε προσωρινό φάκελο + κανάλι 50569)
#   tools/field_test.sh selfcheck    έλεγχος του script στο Linux (loopback, προσομοίωση εγκαταστάτη)
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=python3
if [[ -x .venv/bin/python ]]; then PYTHON=.venv/bin/python; fi
ROLE="${1:-director}"
if [[ $# -gt 0 ]]; then shift; fi
if [[ "$ROLE" == "selfcheck" ]]; then
    "$PYTHON" -m unittest tests.test_field_test -v
fi
exec "$PYTHON" tools/field_test.py "$ROLE" "$@"
