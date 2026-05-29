#!/usr/bin/env bash
set -euo pipefail

# SageMaker starts inference containers with the single argument `serve` and
# expects the model server on port 8080. Locally we use the default CMD on 8000.
if [ "${1:-}" = "serve" ]; then
    exec uvicorn src.api:app --host 0.0.0.0 --port 8080
fi

exec "$@"
