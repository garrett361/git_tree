---
name: git-tree-plan
description: Plan a task in a self-contained way, then hand it off to a fresh git-tree worktree with a plan file for a brand-new agent to implement, with no shared context. Use when the user wants to spin off planned work into its own worktree, says "hand this off", "spin this off", "set up a worktree for this and write a plan file", or similar. Not for ordinary plan-mode use where the same session will implement the plan itself.
---

# Plan work, then hand it off to a fresh worktree

Two phases: plan as if for a stranger, then hand off to a worktree instead of implementing.
The value of invoking this at the start, not the end, is that phase 1 never accumulates
context a fresh reader could not recover — there is no late, lossy rewrite from
conversation-shaped to document-shaped.

## 1. Plan, written for a stranger

If not already in plan mode, enter it. Follow the normal explore, design, and review
workflow, but hold every draft to one extra bar throughout, not only at the end: could a
fresh agent with **zero access to this conversation** pick this plan up and execute it
correctly?

- No "as discussed above" or "per your last message" — restate the reasoning inline.
- Give file paths and function names in full each time they matter, not just the first time.
- Resolve open questions with the user before they would otherwise become an implicit
  assumption a fresh reader could not recover.

## 2. On approval, hand off — do not implement

Once the plan is approved:

1. Check the tree first:

   ```sh
   git tree --json
   ```

   If the intended parent branch is behind a squash-merged dependency, follow the
   `git-tree-land` skill to clean that up before branching. If `--json` reports anything
   broken — orphans, cycles, corrupted submodules, a stuck rebase — follow the
   `git-tree-doctor` skill rather than working around it.

2. Confirm the branch name with the user if it is not already clear, and the parent branch
   to fork from (usually `main`). Create the worktree with the explicit-parent form, so it
   runs from any cwd:

   ```sh
   git tree branch <path> <name> --parent <parent>
   ```

   Name `<path>` `<repo-dir-name>-<branch-with-slashes-removed>`, as a sibling of the
   parent's own worktree directory.

3. Write the approved plan into `<path>/PLAN.md`. If `PLAN.md` already exists there, ask the
   user what filename to use instead of overwriting it. Done right, this write is close to a
   direct copy of phase 1's output, not a rewrite.

4. Report the worktree path, the branch name, and that the plan file is in place. **Stop.**

## Rules

- **Never implement the plan after the handoff.** The fresh agent does that, in its own
  session, with its own clean context.
- **Do not assume the parent worktree is clean.** A submodule mid-rebase, an unrelated dirty
  index, or other unfamiliar state should be investigated and flagged to the user, not
  silently worked around, unless the user explicitly authorizes a specific resolution.
