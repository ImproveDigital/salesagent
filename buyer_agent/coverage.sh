#!/usr/bin/env bash
# Run the sales agent natively under coverage.py so buyer agent scenarios produce a code coverage report.
#
#   buyer_agent/coverage.sh up        # isolated Postgres + migrate + seed + server under coverage
#   source buyer_agent/.coverage-stack.env
#   buyer_agent/run.sh --env cov evals          # or any run / replay against the "cov" target
#   buyer_agent/coverage.sh report    # stop the server gracefully, print the report, write HTML
#   buyer_agent/coverage.sh down      # remove the Postgres container
#
# The Docker image has no dev dependencies and its Postgres is not exposed, so the server runs
# natively here against a throwaway database from the agent-db skill. The demo seed creates the
# principal "ci-test-principal" with token "ci-test-token", which becomes the "cov" target.
# Other server settings come from the repo root .env. Coverage config is [tool.coverage] in pyproject.toml.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COV_DIR="$REPO_ROOT/buyer_agent/runs/coverage"
DATA_FILE="$COV_DIR/.coverage"
PID_FILE="$COV_DIR/server.pid"
LOG_FILE="$COV_DIR/server.log"
STACK_ENV="$REPO_ROOT/buyer_agent/.coverage-stack.env"
AGENT_DB="$REPO_ROOT/.claude/skills/agent-db/agent-db.sh"
PORT="${COV_PORT:-18080}"
cd "$REPO_ROOT"

up() {
    mkdir -p "$COV_DIR"
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "[coverage] server already running (pid $(cat "$PID_FILE")). Run 'report' first." >&2; exit 1
    fi
    if [[ -f .env ]]; then set -a; source .env; set +a; fi
    eval "$("$AGENT_DB" up)"                     # exports DATABASE_URL for the throwaway Postgres
    echo "[coverage] database: $DATABASE_URL"
    uv run python scripts/ops/migrate.py
    uv run python scripts/ops/seed_demo_data.py
    export ADCP_SALES_PORT="$PORT" ADCP_PORT="$PORT"
    rm -f "$DATA_FILE"
    echo "[coverage] starting server on :$PORT under coverage, log: $LOG_FILE"
    uv run coverage run --data-file="$DATA_FILE" scripts/run_server.py > "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    for _ in $(seq 1 60); do
        if uv run python scripts/healthcheck.py "$PORT" > /dev/null 2>&1; then
            cat > "$STACK_ENV" <<ENV
export COV_SALES_AGENT_MCP_URL=http://localhost:$PORT/mcp/
export COV_SALES_AGENT_TOKEN=ci-test-token
ENV
            echo "[coverage] ready. Next:"
            echo "    source $STACK_ENV"
            echo "    buyer_agent/run.sh --env cov evals"
            return
        fi
        sleep 1
    done
    echo "[coverage] server did not become healthy in 60s. See $LOG_FILE" >&2; exit 1
}

stop_server() {
    [[ -f "$PID_FILE" ]] || { echo "[coverage] no server pid file" >&2; return 1; }
    local pid; pid="$(cat "$PID_FILE")"
    if kill -0 "$pid" 2>/dev/null; then
        # SIGINT so run_server's KeyboardInterrupt path runs and coverage writes its data on exit.
        pkill -INT -P "$pid" 2>/dev/null || true
        kill -INT "$pid" 2>/dev/null || true
        for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
        kill -0 "$pid" 2>/dev/null && { echo "[coverage] server did not stop in 30s" >&2; return 1; }
    fi
    rm -f "$PID_FILE"
}

report() {
    stop_server || true
    [[ -f "$DATA_FILE" ]] || { echo "[coverage] no coverage data at $DATA_FILE" >&2; exit 1; }
    uv run coverage report --data-file="$DATA_FILE" | tail -n 30
    uv run coverage html --data-file="$DATA_FILE" -d "$COV_DIR/html" > /dev/null
    uv run coverage json --data-file="$DATA_FILE" -o "$COV_DIR/coverage.json" > /dev/null
    echo "[coverage] HTML: $COV_DIR/html/index.html   JSON: $COV_DIR/coverage.json"
}

down() {
    stop_server 2>/dev/null || true
    "$AGENT_DB" down
    rm -f "$STACK_ENV"
}

case "${1:-}" in
    up) up ;;
    report) report ;;
    down) down ;;
    *) echo "usage: buyer_agent/coverage.sh up|report|down" >&2; exit 1 ;;
esac
