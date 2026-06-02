# Session Completion Checklist

## Before Saying "Done" or "Complete"

### 1. Run Quality Gates
```bash
make quality
```
All checks must pass. If they fail, fix the issues before committing.

### 2. Commit
```bash
git add <specific-files>
git commit -m "feat/fix/refactor: description"
```

Use Conventional Commit prefixes (see CLAUDE.md "Commit Messages & PR Titles") so changes appear in release notes.

### 3. Verify Clean State
```bash
git status
```
Confirm the working tree is clean (or only has expected untracked files).

### 4. File Follow-ups (If Needed)
For anything discovered but not completed, file a GitHub issue:
```bash
gh issue create --title "..." --body "..."
```
Include enough context for the next session to pick up the work.
