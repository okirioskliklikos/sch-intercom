#!/usr/bin/env bash
# 4.4: δοκιμή σε πραγματικό δίκτυο (Linux). Βλ. tools/network_live_test.py.
#   Director:  tools/network_live_test.sh director --clients 2
#   Client:    tools/network_live_test.sh client [--director-host IP]
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=python3
if [[ -x .venv/bin/python ]]; then PYTHON=.venv/bin/python; fi
ROLE="${1:-client}"
shift || true
exec "$PYTHON" tools/network_live_test.py "$ROLE" "$@"
