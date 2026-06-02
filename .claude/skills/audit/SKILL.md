---
name: audit
description: >
  Run a repeatable code review audit on migration changes. Inventories files by
  architectural layer, reviews each layer against #1050/#1066 principles, and
  produces a written report with findings. Re-run after remediation batches to
  track progress.
args: [audit-target]
---

# Migration Audit

Repeatable code review workflow for auditing migration changes. Produces
a structured review document with findings.

## Args

```
/audit [audit-target]
```

The audit target (optional, defaults to "full"):
- **Branch**: `KonstantinMirin/adcp-v3-upgrade` — review all changes on branch
- **Commit range**: `main..HEAD` — review commits in range
- **"full"**: Audit entire codebase against architecture principles

## What It Produces

1. **Change inventory** by architectural layer (schema, business, boundary, transport, adapter, database, test)
2. **Layer-specific reviews** against #1050/#1066 checklists
3. **Consolidated report** in `docs/code-reviews/`
4. **GitHub issues** for findings worth tracking (use `gh issue create`)

## Protocol

Walk the audit linearly in conversation:

1. **inventory-changes** — list files modified in the target range, group by architectural layer
2. **review-per-layer** — apply the layer checklist (below) to each group, capture findings
3. **consolidate** — write the report to `docs/code-reviews/<descriptive-name>.md`
4. **file-issues** — for findings worth tracking, run `gh issue create --title "..." --body "..."`
5. **commit-report**

### Done when report committed

Audit report in `docs/code-reviews/`, follow-up GitHub issues filed where appropriate.

## Layer Review Checklists

Each layer has a specific checklist (see formula for full details):

| Layer | Key Checks |
|-------|-----------|
| Schema | Correct base class, no field duplication, exclude=True on internals |
| Business | No ToolError, no transport imports, ResolvedIdentity, no error dicts |
| Boundary | All _impl params exposed, version compat at boundary only |
| Transport | Shared _impl pattern, no business logic duplication |
| Database | SQLAlchemy 2.0, JSONType, correct column types |
| Adapter | No protocol code, proper error propagation |

## Re-Running After Remediation

The audit is designed to be re-run:
```
/audit main..HEAD    # after first batch
/audit main..HEAD    # after second batch (findings should decrease)
```

Compare reports across runs to track progress.

## See Also

- `/surface` — Create entity test suites for coverage gaps found in audit
- `/guard` — Structural guards enforce the principles audit checks for
- `/remediate` — Fill test stubs to fix the issues audit finds
