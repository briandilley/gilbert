---
name: merge-pr
description: Merge a GitHub pull request into main. Checks out the PR locally, runs the full test suite, runs the validate-architecture audit, and only then squash-merges on GitHub and cleans up. Use when the user says "merge PR #N", "merge this PR", or "/merge-pr".
---

# Merge a Pull Request

End-to-end PR merge: verify locally, audit, then squash-merge and clean up. **Never** merge on GitHub before the local checks pass.

## Inputs

- PR number, e.g. `42`. If the user didn't provide one, try `gh pr view --json number -q .number` to detect a PR for the current branch; if that fails, ask.

## Step-by-step

### 1. Inspect the PR

```bash
gh pr view <N> --json number,title,state,isDraft,mergeable,mergeStateStatus,baseRefName,headRefName,headRepositoryOwner,reviewDecision,statusCheckRollup,url
```

Refuse to proceed if any of these are true (report which one and stop):

- `state` is not `OPEN`.
- `isDraft` is true.
- `mergeable` is `CONFLICTING`.
- `reviewDecision` is `CHANGES_REQUESTED`.
- `statusCheckRollup` has a check in state `FAILURE` / `ERROR` / `CANCELLED` (a `PENDING` check is OK to note but the user decides whether to wait).
- `baseRefName` is not `main`.

If everything is clean except `mergeable` is `UNKNOWN`, retry once after a short pause — GitHub computes it lazily.

### 2. Save the current branch and check out the PR

```bash
START_BRANCH=$(git rev-parse --abbrev-ref HEAD)
git fetch origin
gh pr checkout <N>
```

This puts the PR branch on a local tracking branch. Record the branch name; you'll delete it at the end.

### 3. Sync submodules

`std-plugins/` is a submodule. The PR may bump it.

```bash
git submodule update --init --recursive
```

### 4. Merge `main` into the PR branch locally (dry run for conflicts)

The point is to surface conflicts the user would otherwise hit only after pushing the merge button.

```bash
git fetch origin main
git merge --no-edit --no-ff origin/main
```

If the merge fails:
1. Abort with `git merge --abort`.
2. Switch back to `$START_BRANCH`.
3. Report the conflicting files and stop — let the user resolve on the PR branch.

If the merge succeeds with no changes (already up to date), continue.

### 5. Install / sync dependencies

```bash
uv sync
```

Necessary because the PR may add deps or bump the submodule with new plugin deps.

### 6. Run the full test suite

```bash
uv run pytest -x -q
```

If anything fails, stop. Report the failures verbatim. Do **not** attempt to fix tests as part of the merge — that's a separate task the user has to authorize. Switch back to `$START_BRANCH` before stopping if the user wants to keep working elsewhere.

### 7. Run the architecture audit

Invoke the `validate-architecture` skill in **audit mode** over the current tree (which now includes the PR changes merged with `main`). Report any violations.

- If the audit finds violations, stop and report. Same rule as tests: do not auto-fix as part of the merge. The user decides whether to push fixes onto the PR branch or proceed anyway.

### 8. Squash-merge on GitHub

Only reached if tests and audit are clean.

```bash
gh pr merge <N> --squash --delete-branch
```

`--delete-branch` removes the remote branch. The commit message defaults to the PR title + body; if the user wants a custom message, ask before this step.

### 9. Local cleanup

```bash
git checkout main
git pull --ff-only origin main
git submodule update --init --recursive
# delete the local PR branch (it was just a checkout copy)
git branch -D <pr-branch-name> 2>/dev/null || true
```

If `$START_BRANCH` is not `main` and still exists, ask the user whether to switch back to it.

### 10. Report

One-paragraph summary:

- PR # and title, the squash commit SHA (`git log -1 --format=%H` on main).
- Test result (count or "all passed").
- Audit result ("clean" or N violations resolved before merge).
- Branches deleted.

## Failure handling

- If steps 4–7 fail, leave the working tree on the PR branch unless the user said to abort. Don't merge on GitHub. Don't auto-fix. Report and wait.
- Never `git push --force`, never bypass branch protection, never use `--admin` on `gh pr merge` without an explicit user request.
- If `gh` is not authenticated (`gh auth status` fails), stop and tell the user to run `gh auth login` themselves with the `!` prefix.

## What this skill does NOT do

- Does not fix failing tests.
- Does not fix architecture violations.
- Does not approve the PR or dismiss reviews.
- Does not rewrite the PR's commit history.
- Does not merge into branches other than `main`.

Each of those is a separate, user-authorized task.
