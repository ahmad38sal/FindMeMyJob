#!/usr/bin/env bash
# Entrypoint that resolves $PORT at runtime regardless of how it is invoked.
# Railway (and some platforms) run a configured start command in exec form,
# which does NOT expand environment variables — so a bare
# `uvicorn ... --port $PORT` receives the literal string "$PORT" and crashes.
# Running through this script guarantees the shell expands $PORT, with a
# sensible default for local docker runs.
set -euo pipefail

PORT="${PORT:-8000}"

exec uvicorn findmemyjob.main:app --host 0.0.0.0 --port "$PORT"
