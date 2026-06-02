#!/usr/bin/env bash
# Reset the local Docker storyboard stack between protocol runs.
#
# Full storyboard assessments mutate seller state and reuse scenario
# idempotency keys. Running MCP and A2A in one process without a reset can make
# the second protocol observe cached or post-update state from the first one.

set -euo pipefail

LOCKFILE_HASH="$(shasum -a 256 uv.lock | awk '{print $1}')"
export LOCKFILE_HASH

docker compose down -v
docker compose up -d --wait --wait-timeout "${COMPOSE_WAIT_TIMEOUT:-180}"
