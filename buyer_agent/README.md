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

## Batch mode (non-interactive)

For scripted runs, evals and CI. Nobody is at the keyboard, so the harness applies fixed policies instead of asking:

```bash
buyer_agent/run.sh "Create a display media buy for testbrand.com" --batch --answer "50000" --answer "EUR"
```

- `ask_user` questions are answered from `--answer` values, in order. When they run out the model is told no answer is available and must finish with what it has.
- Write tools (`create_media_buy`, `update_media_buy`, ...) are approved automatically. Only run batch mode against a target where that is acceptable.
- The autonomous turn budget is not extended; reaching it ends the run.
- The model's first final answer ends the run. No follow-up is asked.
- The goal must be given on the command line.

The `prod` confirmation in `run.sh` still requires a terminal, so batch runs against `prod` fail in CI by design.

## Run log

Every run writes a JSONL file, one event per line, nothing truncated. Default location is `buyer_agent/runs/<timestamp>_<goal>.jsonl` (gitignored); override with `--log PATH`. The path is printed at the start and end of the run.

| event | fields |
|-------|--------|
| `run_start` | goal, target URL, model, batch, tools |
| `model_turn` | turn, text (final answers only), calls, usage |
| `tool_call` | turn, name, source, arguments, result, gate, error, duration_ms |
| `user_turn` | text of a follow-up goal |
| `model_error` | turn, error |
| `run_end` | outcome, session usage |

`gate` is `approved` or `declined` for write tools, `null` otherwise. `error` is `null` on success, otherwise `{type, message, code?}`. The console shows results shortened; the log holds them whole.

```bash
jq -c 'select(.event=="tool_call") | {turn,name,duration_ms,error}' buyer_agent/runs/*.jsonl
```

## Scenarios and evals

A scenario is a YAML file in `buyer_agent/scenarios/` holding a goal and expectations about the run log. The eval runner executes each scenario in batch mode as its own process, grades the log, and prints a pass table:

```bash
buyer_agent/run.sh evals                       # all scenarios, once each
buyer_agent/run.sh evals --runs 3              # pass rate over repeats
buyer_agent/run.sh evals buyer_agent/scenarios/out-of-scope.yaml
```

Logs, console output and `results.json` land in `buyer_agent/runs/evals/<timestamp>/`. Exit code is 1 if any run fails.

```yaml
name: create-media-buy
goal: Create a display media buy for testbrand.com with a budget of 5000 USD ...
answers: []               # scripted ask_user answers, optional
tools: [get_products]     # restrict exposed tools, optional
expect:
  outcome: completed      # ended with a final answer, not stopped or crashed
  calls:
    - tool: create_media_buy
      min: 1              # default 0
      max: 1              # default unlimited
      args_contain: {budget: 5000}   # subset match on some call's arguments
  tool_errors: none       # or a list of {tool, message_contains} that must occur
  final_text_contains: ["5,000"]
```

All checks are deterministic and read only the log. The grader is `buyer_agent/grader.py`. Scenarios that write (such as `create-media-buy`) create real objects on the target, so run them against `local`.

## Simulated user

Positional `--answer` values break when the model asks questions in a different order. The simulator fixes that: a second model, with its own prompt and history, answers each `ask_user` question from a fact sheet and nothing else. If the facts do not cover a question it says so rather than inventing.

```bash
buyer_agent/run.sh "Create a display media buy for testbrand.com" --batch --simulate \
    --fact "total budget=5000 USD" --fact "start date=14 days from today" --persona "A junior buyer, terse."
```

In a scenario file, a `facts:` mapping turns the simulator on; `persona:` is optional. Scripted `answers:` are used first, then the simulator. Set `SIMULATOR_MODEL` to use a cheaper model than the buyer's. Each simulated answer is printed as `[user] ... (simulated)` and recorded in the log as the `ask_user` result.
