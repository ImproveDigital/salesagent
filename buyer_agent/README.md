# Buyer agent

A Gemini-driven media buyer that talks to the sales agent over MCP. Used to test the sales agent end to end.

## Setup

Create `buyer_agent/.env` (gitignored) from the template and fill it in:

```bash
cp buyer_agent/.env.template buyer_agent/.env
```

```bash
# buyer_agent/.env
LOCAL_SALES_AGENT_MCP_URL=http://localhost:8000/mcp/
LOCAL_SALES_AGENT_TOKEN=<advertiser token>       # Admin UI -> Advertisers -> API Token

DEV_SALES_AGENT_MCP_URL=https://<dev-host>/mcp/
DEV_SALES_AGENT_TOKEN=<advertiser token>

GEMINI_API_KEY=<your key>                        # shared by all targets
# GEMINI_MODEL=<model id>                        # optional
```

Each `<NAME>_SALES_AGENT_MCP_URL` / `<NAME>_SALES_AGENT_TOKEN` pair defines a target. Add a `PROD_` pair the same way. Variables exported in the shell override the file.

## Run

```bash
buyer_agent/run.sh targets                      # list targets
buyer_agent/run.sh use local                    # pick the current target
buyer_agent/run.sh                              # asks for a goal
buyer_agent/run.sh "Find display products for testbrand.com"
buyer_agent/run.sh --env dev "..."              # one-off target override
buyer_agent/run.sh "..." --tools get_products,create_media_buy --show-history
```

After each answer the harness asks for a follow-up. Press Enter or type `exit` to finish. Running against `prod` asks for confirmation.
