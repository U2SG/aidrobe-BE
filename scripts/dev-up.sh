#!/usr/bin/env bash
# Start the zero-cost local stack (fake providers, local MinIO) and wait until
# the API is ready. Requires Docker (OrbStack or Docker Desktop).
set -euo pipefail
cd "$(dirname "$0")/.."

export GIT_SHA="${GIT_SHA:-$(git rev-parse --short HEAD 2>/dev/null || echo unknown)}"
compose=(docker compose -f docker-compose.yml -f docker-compose.local.yml)

"${compose[@]}" up -d --build postgres redis minio minio-init migrate api worker

api="http://localhost:8000"
for _ in $(seq 1 60); do
  if curl -fsS "$api/readyz" >/dev/null 2>&1; then
    echo "ready: $(curl -fsS "$api/version")"
    exit 0
  fi
  sleep 2
done
echo "API did not become ready; recent logs:" >&2
"${compose[@]}" logs --tail 50 api worker migrate >&2
exit 1
