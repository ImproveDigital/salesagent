---
name: research
description: Research a feature, bug, or change before implementation
arguments:
  - name: topic
    description: What to research (e.g., "how get_products handles category filters", "fix-XYZ acceptance criteria")
    required: true
---

# Research Topic: $ARGUMENTS

## Instructions

You are researching **$ARGUMENTS** before implementation begins.

### Step 1: Read the AdCP Specification (Spec-First)
If the topic touches schemas, data models, targeting, protocol behavior, tool inputs/outputs, or buyer-facing fields — **read the spec before exploring the codebase**. The spec defines what's correct; the code is our current (possibly wrong) implementation of it.

**Source of truth**: Local adcp repo at `/Users/konst/projects/adcp`. Read the relevant schema and doc files directly — do NOT rely on training data or assumptions about the spec. Online repo is adcontextprotocol/adcp.

Key locations:
- **JSON schemas**: `/Users/konst/projects/adcp/static/schemas/source/`
  - Core types: `core/targeting.json`, `core/package.json`, `core/product.json`, `core/media-buy.json`, `core/frequency-cap.json`, etc.
  - Enums: `enums/` (pricing models, statuses, etc.)
  - Media buy operations: `media-buy/` (create/update requests/responses, package-request)
  - Signals: `signals/`
- **Protocol docs**: `/Users/konst/projects/adcp/docs/` (protocols, media-buy lifecycle, reference)
- **Python types**: The `adcp` Python library may be ahead of the JSON schema. When they diverge, note the discrepancy in the research artifact.

What to extract from the spec:
1. **Read the relevant JSON schema(s)** — identify which fields are spec-defined, their types, and constraints
2. **Check required vs optional** — does the spec mandate fields we might treat as optional (or vice versa)?
3. **Check `additionalProperties`** — does the spec allow extra fields?
4. **Note any spec gaps** — if the work requires behavior the spec doesn't cover, document it explicitly. Don't invent spec compliance.

If the topic does NOT touch protocol-facing types (e.g., pure infra, CI, internal tooling), skip this step and note "N/A — no AdCP surface" in the research artifact.

### Step 2: Explore the Codebase
Based on the topic (and spec findings from Step 1):
1. Search for relevant code using Grep and Glob
2. Read the files that will need to be modified
3. Check existing tests for the affected area
4. Look for similar implementations to follow as patterns
5. **Compare what the code does against what the spec says** — note any divergences

### Step 3: Check Documentation (Doc-First Rule)
If the topic involves external libraries:
- Use Ref MCP to search library documentation
- Use DeepWiki MCP to ask questions about GitHub repos
- Check CLAUDE.md for project-specific patterns
- Check `/docs` directory for detailed documentation

### Step 4: Engineering Checklist
Run these checks against your findings. Each one should produce a concrete answer, not a shrug.

1. **DRY**: Does similar logic already exist? Search for functions doing comparable work. Extend, don't duplicate.
2. **Library idioms**: How does the primary library (Pydantic, SQLAlchemy, FastMCP, etc.) solve this? Check docs via Ref/DeepWiki before hand-rolling.
3. **Data flow trace**: Walk one concrete example from system boundary (buyer JSON) → Pydantic parsing → logic layer → data layer (DB write/read) → response serialization. Trace both the success path and a failure/rejection path. Note where types change or could break.
4. **Existing conventions**: Find 2-3 similar implementations in the codebase. Note the pattern. Your implementation should match.
5. **Test infrastructure**: What fixtures, factories, helpers already exist in `tests/`? What's reusable vs needs new?

### Step 5: Identify Architecture Decisions
Based on your research:
- What CLAUDE.md patterns apply?
- Are there multiple valid approaches?
- What are the risks or edge cases?

### Step 6: Create Research Artifact
Create a research file at `.claude/research/<short-slug>.md` with:

```markdown
# Research: <topic>

## Findings
- [Key findings from codebase exploration]

## AdCP Spec Verification
- [Which schema(s) checked: e.g., `core/targeting.json`, `media-buy/package-request.json`]
- [Field alignment: do our Pydantic models match?]
- [Any spec gaps or divergences noted]
- Or: "N/A — no AdCP surface"

## Relevant Code
- `path/to/file.py:line` — [what it does]

## CLAUDE.md Patterns
- [Which critical patterns apply and how]

## Architecture Decisions
- [Decisions and rationale]

## Implementation Plan
1. [First step]
2. [Second step]
3. [...]

## Risks & Edge Cases
- [Potential issues to watch for]
```
