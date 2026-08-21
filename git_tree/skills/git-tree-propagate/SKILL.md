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
If the user wants to update only `<branch>` itself without cascading into its descendants, add
`--no-descendants` (same flag on `git tree rebase`). Leave it off by default.

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

With every conflicted file resolved and staged, run the `remedy` exactly as given (if the
original call passed `--no-descendants`, the remedy carries it too, so do not drop it):

```sh
git tree propagate <branch>
```

No `-y`: a resume skips confirmation. Two things that do not work:

- `git rebase --continue` by hand. git-tree finishes the rebase itself, records the fork commit,
  and lands the branch on its parent's current tip. Doing it by hand skips all of that.
- A bare `git tree propagate` from inside the conflicted worktree. HEAD is detached mid-rebase, so
  it fails with `fatal: not on a branch (detached HEAD)` and no hint. Always name the branch.

Repeat this section for each further conflict. A resume finishes the interrupted rebase and then
rebases the branch onto its parent's current tip, so commits the parent gained while you were
resolving are picked up in the same run. That second step can conflict too; resolve it the same
way.

## 4. Finish

- **Stashed work.** If the worktree was dirty when the cascade started, git-tree stashed it and a
  resume does not restore it. The conflict message names the exact stash commit when this
  applies: run the `git stash apply <sha>` it prints, from that worktree. Use the SHA it gives
  you rather than `stash@{0}`, which is shared across every worktree in the repo and may point
  at a different one by now.

Confirm `pending_from_parent` is 0 for the branches you touched.

## Refusals

- `kind=unresolved_conflicts`: files still conflicted, or resolved but not `git add`ed.
- `stopped with unstaged changes that are not a conflict`: the most likely one to hit on a resume.
  The conflict is resolved and staged, but some other tracked file in that worktree is modified and
  unstaged, so git refuses to continue and skipping would discard it. The message lists exactly
  those unstaged files and prints the stash command with them named, safe to run verbatim:

  ```sh
  git -C <worktree> stash push -- <file>...
  ```

  Then re-run the resume. Keep the pathspec: a bare `git stash push` also takes the staged conflict
  resolution, leaving an empty index and an empty replay. And do not `git add` these files:
  `--continue` commits whatever is staged as the commit being replayed, so that folds unrelated
  work into someone else's commit.
- `an operation in progress that rebasing would discard`: a merge, cherry-pick, or revert is
  half-done in that worktree. Finish or abort it there, then re-run. Note this fires even when
  the user has already staged their resolutions, which is why nothing else caught it.
- `a rebase not started by git-tree is in progress` (a descendant), or `git-tree did not start it`
  (the branch you named): git-tree will not drive a hand-started rebase, including a `git rebase
  -i` stopped at an `edit`. The second form names why it was disowned, one of: it is interactive,
  its base cannot be read (a `git am`), its base is neither the tree-parent nor an ancestor of it,
  or the branch has no tree-parent. Finish it or `git rebase --abort` in that worktree, then
  re-run. Do not read the base as the reason unless the message says so: a base that is an
  ancestor of the tree-parent is normal and expected while a cascade is paused.
- `would invalidate a rebase already in progress below it`: from `git tree rebase`, when a branch
  under the one being rebased is paused mid-cascade. Rewriting the branch above would strand it,
  so nothing has moved yet. Finish the paused branch with the `git tree propagate <branch>` the
  message names, then re-run the rebase.
- `These branches have corrupted submodule state`: follow the `git-tree-doctor` skill.
- `These branches need worktrees`: `git worktree add <path> <branch>` for each, then re-run.
