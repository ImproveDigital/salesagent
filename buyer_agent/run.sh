#!/usr/bin/env bash
# Run the buyer agent harness against a sales agent (local, dev, prod, ...).
#
# Usage:
#   buyer_agent/run.sh use dev                  # remember "dev" as the current target
#   buyer_agent/run.sh                          # run against the current target, ask for a goal
#   buyer_agent/run.sh "Find display products for testbrand.com"
#   buyer_agent/run.sh --env prod "..."         # one-off override of the current target
#   buyer_agent/run.sh "..." --tools get_products,create_media_buy --show-history
#   buyer_agent/run.sh targets                  # list configured targets
#
# Targets are defined by prefixed variables, in the shell or in an env file
# (default: buyer_agent/.env, override with BUYER_AGENT_ENV):
#   LOCAL_SALES_AGENT_MCP_URL=http://localhost:8000/mcp/
#   LOCAL_SALES_AGENT_TOKEN=...
#   DEV_SALES_AGENT_MCP_URL=https://.../mcp/
#   DEV_SALES_AGENT_TOKEN=...
#   GEMINI_API_KEY=...                          # shared by all targets
#   GEMINI_MODEL=...                            # optional
#
# Target resolution order: --env flag, SALES_ENV, buyer_agent/.context (set by
# "use"), then an interactive picker. Variables already exported in the shell
# take precedence over the env file. Running against "prod" asks for confirmation.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${BUYER_AGENT_ENV:-$REPO_ROOT/buyer_agent/.env}"
CONTEXT_FILE="$REPO_ROOT/buyer_agent/.context"

# --- load env file (buyer agent keys only, shell wins) -----------------------
if [[ -f "$ENV_FILE" ]]; then
    while IFS= read -r line; do
        key="${line%%=*}"
        value="${line#*=}"
        value="${value%\"}"; value="${value#\"}"
        value="${value%\'}"; value="${value#\'}"
        if [[ -z "${!key:-}" ]]; then
            export "$key=$value"
        fi
    done < <(grep -E '^([A-Z0-9_]+_SALES_AGENT_(MCP_URL|TOKEN)|GEMINI_API_KEY|GEMINI_MODEL)=' "$ENV_FILE" || true)
fi

# --- discover targets from *_SALES_AGENT_MCP_URL ------------------------------
targets=()
while IFS= read -r name; do
    targets+=("$(echo "${name%_SALES_AGENT_MCP_URL}" | tr '[:upper:]' '[:lower:]')")
done < <(env | grep -oE '^[A-Z0-9_]+_SALES_AGENT_MCP_URL' | sort -u)

if (( ${#targets[@]} == 0 )); then
    echo "No targets configured. Add e.g. LOCAL_SALES_AGENT_MCP_URL and LOCAL_SALES_AGENT_TOKEN to $ENV_FILE" >&2
    exit 1
fi

is_target() {
    local t
    for t in "${targets[@]}"; do [[ "$t" == "$1" ]] && return 0; done
    return 1
}

# --- subcommands and flags ----------------------------------------------------
target="${SALES_ENV:-}"
passthrough=()

case "${1:-}" in
    use)
        [[ -n "${2:-}" ]] || { echo "usage: buyer_agent/run.sh use <target>" >&2; exit 1; }
        is_target "$2" || { echo "Unknown target '$2'. Known: ${targets[*]}" >&2; exit 1; }
        echo "$2" > "$CONTEXT_FILE"
        echo "Current target set to '$2'"
        exit 0
        ;;
    targets)
        current="$(cat "$CONTEXT_FILE" 2>/dev/null || true)"
        for t in "${targets[@]}"; do
            url_var="$(echo "$t" | tr '[:lower:]' '[:upper:]')_SALES_AGENT_MCP_URL"
            marker=" "; [[ "$t" == "$current" ]] && marker="*"
            printf '%s %-8s %s\n' "$marker" "$t" "${!url_var}"
        done
        exit 0
        ;;
esac

while (( $# )); do
    case "$1" in
        --env)
            [[ -n "${2:-}" ]] || { echo "--env needs a target name" >&2; exit 1; }
            target="$2"; shift 2
            ;;
        --env=*)
            target="${1#--env=}"; shift
            ;;
        *)
            passthrough+=("$1"); shift
            ;;
    esac
done

# --- resolve target: flag > SALES_ENV > .context > picker ---------------------
if [[ -z "$target" && -f "$CONTEXT_FILE" ]]; then
    target="$(tr -d '[:space:]' < "$CONTEXT_FILE")"
fi

if [[ -z "$target" ]]; then
    if (( ${#targets[@]} == 1 )); then
        target="${targets[0]}"
    else
        echo "Which sales agent should the buyer talk to?"
        select target in "${targets[@]}"; do
            [[ -n "$target" ]] && break
        done < /dev/tty
        [[ -n "$target" ]] || { echo "No target chosen." >&2; exit 1; }
    fi
fi

is_target "$target" || { echo "Unknown target '$target'. Known: ${targets[*]}" >&2; exit 1; }

prefix="$(echo "$target" | tr '[:lower:]' '[:upper:]')"
url_var="${prefix}_SALES_AGENT_MCP_URL"
token_var="${prefix}_SALES_AGENT_TOKEN"

missing=()
[[ -n "${!token_var:-}" ]] || missing+=("$token_var")
[[ -n "${GEMINI_API_KEY:-}" ]] || missing+=("GEMINI_API_KEY")
if (( ${#missing[@]} )); then
    echo "Missing required variables: ${missing[*]}" >&2
    echo "Set them in the shell or in $ENV_FILE" >&2
    exit 1
fi

export SALES_AGENT_MCP_URL="${!url_var}"
export SALES_AGENT_TOKEN="${!token_var}"

echo "[harness] target: $target  ($SALES_AGENT_MCP_URL)"

if [[ "$target" == "prod" ]]; then
    read -r -p "[harness] This is PRODUCTION. The agent can create real media buys. Type 'yes' to continue: " answer < /dev/tty
    [[ "$answer" == "yes" ]] || { echo "Aborted."; exit 1; }
fi

cd "$REPO_ROOT"
exec uv run python -m buyer_agent.main "${passthrough[@]+"${passthrough[@]}"}"
