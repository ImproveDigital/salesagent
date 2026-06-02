# Bug Reporting & Fix Workflow

## When You Find a Bug

### 1. Capture the Bug
Either fix it immediately (if small and in scope) or file a GitHub issue:
```bash
gh issue create --title "Bug: <concise description>" --label bug --body "$(cat <<'EOF'
## Observed
What actually happens.

## Expected
What should happen.

## Reproduction
Steps to trigger.

## Affected area
Which files/components.
EOF
)"
```

### 2. Validate Against Patterns

Before fixing, check:
- Does this violate a CLAUDE.md critical pattern?
- Is this an AdCP spec compliance issue? (Check `tests/unit/test_adcp_contract.py`)
- Is this a regression from a recent change? (Check `git log --oneline -20`)

### 3. Write Regression Test First

```bash
uv run pytest tests/unit/test_<area>.py::test_<bug_description> -x
# Confirm it fails for the right reason
```

The test should demonstrate the bug clearly, be minimal (test one thing), and follow existing patterns in the file.

### 4. Fix the Bug

- Fix the root cause, not symptoms
- Keep the fix minimal and focused
- Don't refactor surrounding code (separate PR)

### 5. Quality Gates
```bash
make quality
```

Verify: new test passes, no existing tests broken, formatting and linting clean.

### 6. Commit
```bash
git add <specific-files>
git commit -m "fix: <description of what was fixed>"
```

If a GitHub issue exists, reference it in the commit body (`Fixes #123`).
