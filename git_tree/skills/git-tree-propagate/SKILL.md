---
name: git-tree-propagate
description: Cascade a branch's changes to its descendants with git-tree, and drive any merge conflict to resolution interactively with the user. Use when propagating a stacked-branch change, when a git tree propagate or git tree rebase stopped on a conflict, or when asked to resume or finish a stuck cascade.
---

# Propagate a git-tree stack and resolve conflicts with the user

`git tree propagate <branch>` replays every descendant of `<branch>` onto its parent, replaying
only each branch's own commits. It is also the only resume verb: re-running it after a conflict
finishes the interrupted rebase and continues the cascade.

## 1. Check the stack

```sh
git tree --json
```

`propagate <branch>` updates the **descendants** of `<branch>`, never `<branch>` itself. If a
branch fell behind its parent, name the **parent**. Run from the child and it prints
`No descendants to propagate to.` and exits 0, having done nothing. `pending_from_parent` shows
which branches have work to pick up.

## 2. Run it

```sh
git tree propagate <branch> -y
```

Use `--dry-run` first if the user wants a preview. Do not pass `--no-auto-rerere` unless asked.

Exit 0 finished the cascade. Exit 3 stopped on a conflict.

## 3. Resolve a conflict

The error names the branch, its worktree, the conflicted files, and a `remedy` command, exposed
under `--json` as `error.branch`, `error.worktree`, `error.conflicted_files`, `error.remedy`.

**Never `git add` a resolution the user has not approved.** Per file:

1. Show the conflicted hunks from the worktree named in the error.
2. Say which side is which, by branch name. Rebase inverts the usual reading: `ours` is the parent
   being replayed onto, `theirs` is the commit from the branch being replayed. Do not use the bare
   words without saying which branch each refers to.
3. Propose a resolution and state what it discards.
4. When asking for approval, say that the decision is binding downstream: rerere records it and
   replays it automatically wherever the same conflict recurs later in the cascade, without asking
   again. The user decides once.
5. Only after explicit approval, write the file and `git add` it in that worktree.

With every conflicted file resolved and staged, run the `remedy` exactly as given:

```sh
git tree propagate <branch>
```

No `-y`: a resume skips confirmation. Two things that do not work:

- `git rebase --continue` by hand. git-tree finishes the rebase itself and records the fork commit
  at the base actually replayed onto. Doing it by hand skips that bookkeeping.
- A bare `git tree propagate` from inside the conflicted worktree. HEAD is detached mid-rebase, so
  it fails with `fatal: not on a branch (detached HEAD)` and no hint. Always name the branch.

Repeat this section for each further conflict.

## 4. Finish

- **Stashed work.** If the worktree was dirty when the cascade started, git-tree stashed it and a
  resume does not restore it. The conflict message names the exact stash commit when this
  applies: run the `git stash apply <sha>` it prints, from that worktree. Use the SHA it gives
  you rather than `stash@{0}`, which is shared across every worktree in the repo and may point
  at a different one by now.
- **A parent that moved during the pause.** A resume records the fork commit at the base actually
  replayed onto, not the parent's current tip, so the branch can land behind the parent and look
  finished. Re-run `git tree propagate <parent>`.

Confirm `pending_from_parent` is 0 for the branches you touched.

## Refusals

- `kind=unresolved_conflicts`: files still conflicted, or resolved but not `git add`ed.
- `a rebase not started by git-tree is in progress`: git-tree will not drive a hand-started
  rebase. Finish it or `git rebase --abort` in that worktree, then re-run.
- `These branches have corrupted submodule state`: follow the `git-tree-doctor` skill.
- `These branches need worktrees`: `git worktree add <path> <branch>` for each, then re-run.
