#!/usr/bin/env bash
# Αυτόματη δοκιμή σελίδας «Ασφάλεια» — πλευρά Director (Linux, ανοίγει το GUI).
# Στα Windows: powershell -ExecutionPolicy Bypass -File tools\mtls_live_test.ps1
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
exec "$PY" tools/security_gui_live_test.py director "$@"
