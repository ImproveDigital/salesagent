---
name: guard
description: >
  Create structural guard tests that enforce architecture principles on every
  `make quality` run. Guards are AST-scanning tests that prevent categories of
  violations automatically. Available guards: schema-inheritance,
  boundary-completeness, query-type-safety, no-error-dicts.
args: <guard-name-1> [guard-name-2] ...
---

# Structural Guard Creation

Create AST-scanning enforcement tests that catch architecture violations
automatically. Each guard runs on `make quality` and prevents regressions.

## Args

```
/guard <guard-name-1> [guard-name-2] ...
```

Guard names (space-separated). Each gets a test file at
`tests/unit/test_architecture_{guard_name}.py`.

## Available Guards

| Guard | What It Enforces |
|-------|-----------------|
| schema-inheritance | Schema classes extend correct adcp library base types |
| boundary-completeness | MCP/A2A/REST wrappers expose all _impl parameters |
| query-type-safety | DB queries use column types matching the column definition |
| no-error-dicts | _impl functions raise exceptions, never return error dicts |

Custom guard names are also supported — the research atom will determine
what to enforce based on the name and #1050/#1066 principles.

## Existing Structural Guards

These already exist (don't recreate):
- `test_no_toolerror_in_impl.py` — No ToolError in _impl functions
- `test_transport_agnostic_impl.py` — No transport imports in _impl
- `test_impl_resolved_identity.py` — _impl accepts ResolvedIdentity

## Protocol

For each guard name, walk these steps in conversation:

1. **research** — define exactly what the guard should detect (the AST pattern, the rule)
2. **scan** — write a scratch script to find current violations across the codebase
3. **write-guard** — create `tests/unit/test_architecture_{guard_name}.py` following the existing structural-test pattern
4. **mark-known** — populate the allowlist with current violations (each gets a `# FIXME` comment at the source location)
5. **verify** — `make quality` passes; new violations would fail the guard
6. **commit**

### Done when all guards committed

All guards passing in `make quality`.

## Key Principles

- **Allowlists shrink, never grow.** New violations fail the guard immediately.
- **Guards follow existing patterns.** Read the 3 existing structural tests first.

## See Also

- `/surface` — Create entity test suites (what the guards protect)
- `/remediate` — Fill entity test stubs (fix the violations guards find)
