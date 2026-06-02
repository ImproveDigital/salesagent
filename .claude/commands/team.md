---
name: team
description: Launch a coordinated agent team to work on parallel tasks
arguments:
  - name: prompt
    description: What the team should do (e.g., "convert these 5 test files to use factories in parallel")
    required: true
---

# Launch Agent Team: $ARGUMENTS

Use the `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` feature to spawn parallel general-purpose agents on independent units of work. Best when the work decomposes into non-overlapping file scopes.

**Prerequisite:** This command depends on the experimental teams flag and the `TeamCreate` / `SendMessage` deferred tools. If `ToolSearch: "select:TeamCreate"` returns no schema, the flag isn't enabled in this environment — fall back to spawning multiple `Agent` calls in parallel (one message, multiple tool uses) without team coordination, or stop and tell the user.

**Scope:** Best for file-scoped, mostly-independent work — refactors, test rewrites, mechanical changes. For DB-touching work where teammates need their own Postgres, see `.claude/skills/agent-db/` and include setup instructions in the teammate prompt; teammates share the working directory and won't get isolated stacks automatically.

## Protocol

### Step 1: Load team tools
```
ToolSearch: "select:TeamCreate"
ToolSearch: "select:TaskCreate"
ToolSearch: "select:SendMessage"
```

### Step 2: Plan the work
Decompose the user's prompt into parallel work items.

**Grouping strategy:**
- Group work that touches the **same file** into one teammate (avoids merge conflicts)
- Each teammate gets one file or a small set of non-overlapping files
- Shared resources (allowlists, config files) need either coordinated updates or a reconciliation pass after teammates finish

### Step 3: Create the team
```
TeamCreate: team_name="<descriptive-name>", description="<what the team does>"
```

### Step 4: Spawn teammates
For each work item, spawn a general-purpose teammate with a self-contained prompt:
```
Task:
  team_name: "<team-name>"
  name: "<short-label>"
  subagent_type: "general-purpose"
  prompt: |
    <Full briefing: goal, files in scope, what NOT to touch, definition of done>

    Run quality gates before reporting back:
      make quality

    Report: files changed, tests added/changed, final commit hash (if you committed).
```

**NOTE: `isolation: "worktree"` is a no-op for team agents.** All teammates share the same working directory and branch. Ensure parallel teammates touch non-overlapping files to avoid conflicts.

### Step 5: Monitor and coordinate
- Teammates send messages when they complete or get stuck
- Messages are delivered automatically — no polling needed
- Use SendMessage to communicate with teammates by name

### Step 6: Verify and commit
After all teammates complete:
1. Run `./run_all_tests.sh` on the combined result (NOT just `make quality` — the full suite including e2e and ui is mandatory for changes that span multiple files)
2. Review JSON results in `test-results/<ddmmyy_HHmm>/` if terminal output is lost
3. Squash or organize commits if needed
4. Push only if the user requests it

**Test Integrity — ZERO TOLERANCE**: If any test fails in the combined result, do NOT skip it or rationalize it. See CLAUDE.md "Test Integrity Policy". Every failure must be fixed or reported as a blocker.

## User's Request

$ARGUMENTS
