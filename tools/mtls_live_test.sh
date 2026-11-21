#!/usr/bin/env bash
# Αυτόματη δοκιμή 4.2A1/4.2A2 — πλευρά Director (Linux). Βλ. tools/mtls_live_test.py.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
exec "$PY" tools/mtls_live_test.py director "$@"
