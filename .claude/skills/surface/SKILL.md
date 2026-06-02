---
name: surface
description: >
  Map the complete test surface for domain entities by cross-referencing
  test-obligations with existing tests. Produces one canonical test module per
  entity with real tests + skip stubs for gaps. Every obligation maps to exactly
  one test. Run this before /remediate.
args: <entity-name-1> [entity-name-2] ...
---

# Entity Test Surface Mapping

Create canonical test suites per domain entity by mapping all test obligations
to real tests or stubs. This gives complete visibility into what's tested and
what's missing.

## Args

```
/surface <entity-name-1> [entity-name-2] ...
```

Entity names (space-separated). Each gets a test file at
`tests/unit/test_{entity_name}.py` (hyphens → underscores).

## Entity → Obligations Mapping

| Entity | Primary Obligations | Output File |
|--------|-------------------|-------------|
| product | UC-001 | test_product.py |
| media-buy | UC-002, UC-003 | test_media_buy.py |
| delivery | UC-004 | test_delivery.py |
| creative-formats | UC-005 | test_creative_formats.py |
| creative | BR-UC-006 | test_creative.py |
| properties | BR-UC-007 | test_properties.py |
| audience-signals | BR-UC-008 | test_audience_signals.py |
| performance | BR-UC-009 | test_performance.py |
| capabilities | BR-UC-010 | test_capabilities.py |
| pricing-option | business-rules, constraints | test_pricing_option.py |
| transport-boundary | #1050/#1066 principles | test_transport_boundary.py |

All entities also check cross-cutting files: `business-rules.md`, `constraints.md`.

## What You Get

For each entity, a test module containing:
- **Real tests**: Ported from existing (possibly scattered) test files
- **Skip stubs**: `@pytest.mark.skip(reason="STUB: [obligation-ID]")` for every gap
- **Obligation traceability**: Each test/stub references its obligation ID

Open `test_creative.py` → see 89 test names → 30 real, 59 stubs → know exactly what's missing.

## Protocol

For each entity, walk these steps in conversation:

| # | Step | What It Does |
|---|------|-------------|
| 1 | gather-obligations | Read `docs/test-obligations/` + `business-rules.md` + `constraints.md` for the entity, list every obligation ID with its scenario |
| 2 | audit-existing | Find every existing test that covers any of those obligations — note file:line for each |
| 3 | review | Verify the audit: missed tests? Mis-tagged obligations? Cross-cutting tests counted twice? |
| 4 | triage | Decide for each obligation: covered (port to suite) vs gap (stub) — flag any that need user clarification |
| 5 | generate-suite | Write `tests/unit/test_{entity}.py` with real ports + `@pytest.mark.skip(reason="STUB: <obligation-ID>")` stubs |
| 6 | verify | `make quality`; every obligation appears exactly once; tagged tests run cleanly |
| 7 | commit | Commit the suite |

### Done when all entity suites committed

Coverage summary generated.

### Long-run state (multi-entity)

If you're running this for several entities and the conversation grows long, persist the obligation→test mapping for each completed entity to `.claude/scratch/surface-{entity}.md` so subsequent entities (or follow-up work) can re-read it without scrolling.

## Naming Rules

Test names describe **behavior**, not bugs:
- `test_create_media_buy_rejects_missing_brand` (behavior)
- NOT: `test_bug_fix_123` or `test_v2_compat` (incident-driven)

## See Also

- `/remediate` — Fill the stubs this skill creates
- `/guard` — Structural guards that prevent new violations
