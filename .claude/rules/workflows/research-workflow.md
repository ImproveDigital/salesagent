# Research Workflow

## When Research is Needed

Before implementing, research is warranted when:
- The work involves unfamiliar code paths
- External library APIs need verification (doc-first rule)
- Architecture decisions need to be made
- Requirements are ambiguous

## Research Process

### 1. Explore the Codebase
- Read files involved in the change
- Trace execution paths
- Check existing tests for examples
- Look for similar implementations

### 2. Check Documentation
**Doc-First Rule**: For external libraries, check docs before relying on training data.

Available documentation sources:
- **Ref MCP**: Search library docs (FastMCP, SQLAlchemy, Flask, Pydantic, etc.)
- **DeepWiki MCP**: Ask questions about GitHub repositories
- **CLAUDE.md**: Project patterns and architecture
- **`/docs` directory**: Detailed project documentation

### 3. Record Findings (Optional)

For non-trivial research, save an artifact under `.claude/research/<short-slug>.md`:

```markdown
# Research: <topic>

## Findings
- Key finding 1
- Key finding 2

## Relevant Code
- `path/to/file.py:line` — description
- `path/to/other.py:line` — description

## Architecture Decisions
- Decision 1: <rationale>

## Implementation Notes
- Start with: <suggested first step>
- Watch out for: <potential pitfalls>
```

## Tips

- Keep research artifacts concise (not exhaustive documentation)
- Focus on what the implementer needs to know
- Include specific file paths and line numbers
- Note any risks or edge cases discovered
- If research takes > 15 minutes, it's probably too broad — narrow scope
