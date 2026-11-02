#!/usr/bin/env bash
# Local dev without Docker: starts a local Postgres (must have pgvector) and the app.
set -e
cd "$(dirname "$0")/.."
sudo service postgresql start >/dev/null 2>&1 || true
PY="${PYTHON:-python3}"
[ -x /tmp/venv/bin/python ] && PY=/tmp/venv/bin/python
exec "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT:-8080}"
