---
name: git-tree-doctor
description: Diagnose and repair a broken git-tree stack, including orphaned parents, dependency cycles, missing or stale worktrees, corrupted submodules, and stuck rebases. Use when git tree reports orphans or cycles, when a git tree command refuses with a precondition error, or when worktrees or the branch tree appear broken.
---

# Diagnose and repair a git-tree stack

```sh
git tree --json
```

This reports the whole forest even when the current branch is not in a tree, and it includes
broken branches the normal display omits. Read `cycles`, `orphans`, and per branch
`orphaned_parent`, `cyclic`, `worktree`, `dirty`, `conflicted`, `rebase_in_progress`.

**Report the diagnosis before changing anything.** These fixes delete worktrees and rewrite tree
structure. Apply one at a time and re-run `git tree --json` after each, since one broken edge
often masks another. Never hand-edit `branch.<name>.tree-parent-branch` or
`branch.<name>.tree-fork-commit`.

## `orphaned_parent: <name>`

The configured parent no longer exists, so the edge was dropped. **Repair the edge first.**
`git tree rebase` cannot do it: it reads the configured old parent straight from git config and
fails with `Old parent <name> does not exist.` before touching anything.

Ask which branch it should hang from, then repair the edge:

- `git tree attach <new-parent>` records the edge, rewriting no history, so it is correct whether
  or not the branch already sits on that parent. This one acts on the **current** branch, so run
  it from that branch's own worktree.
- `git tree detach <branch> -y` instead, to leave it as its own root. It names its branch, so it
  runs from anywhere.

If the commits also need to move onto the new parent, run
`git tree rebase <new-parent> <branch> -y` as a second step, *after* `attach` has repaired the
edge. That one names its branch too.

## `cyclic: true`, non-empty `cycles`

Branches point at each other in a loop, which only comes from hand-edited config. Break it by
detaching one member, chosen with the user, then attach or rebase it onto the right parent:

```sh
git tree detach <branch> -y
```

git-tree prunes cycle edges rather than failing, so the rest of the forest keeps working.

## `worktree: null` on a registered branch

Cascading commands require worktrees:

```sh
git worktree add <path> <branch>
```

If the directory was deleted without pruning, git still holds a stale registration and
`git tree rebuild` refuses, printing the recovery:

```sh
git worktree prune
git worktree add <path> <branch>
```

`rebuild` recreates a corrupted worktree in place. It cannot resurrect a deleted directory.

## `These branches have corrupted submodule state`

A submodule's `.git` pointer no longer resolves. `propagate` and `rebase` refuse before touching
anything, because `git status` itself crashes in this state.

```sh
git tree rebuild <branch> -y
```

This deletes and recreates the worktree from the branch tip, keeping the branch ref and tree
config, then re-initializes submodules. **Uncommitted work in that worktree is lost**, so it
refuses rather than delete something it cannot account for. It refuses when the worktree is
dirty, when a submodule holds uncommitted content (including a populated directory that was
never initialized, or a `.gitmodules` it cannot parse), when a rebase is in progress, and when
it cannot read the worktree at all.

Each refusal names what it found. Treat it as a question for the user, not an obstacle: look in
the named path, rescue anything they want, and only then re-run with `--force`, which destroys
exactly what the refusal described. It also refuses if your shell is inside the target worktree,
so `cd` out first.

## `rebase_in_progress: true`

Two cases, distinguished by what git-tree says when you re-run the cascade:

- A git-tree cascade waiting to resume: follow the `git-tree-propagate` skill.
- `a rebase not started by git-tree is in progress`: git-tree will not drive a hand-started
  rebase. Finish it or `git rebase --abort` in the named worktree, then re-run.

A branch mid-rebase with no tree-parent cannot be resumed by git-tree either. Finish it with
`git rebase --continue` in its worktree.

`git tree rebase` refuses while any branch below the one named is mid-rebase, naming it: rewriting
the branch above would strand it. Finish that branch first, then re-run the rebase.

## `conflicted` above 0 with no rebase in progress

Usually a stalled merge. Resolve it with plain git in that worktree, with the user's approval on
each resolution, before running any git-tree command over that branch.

## Not breakage

- `Not on a tree-branch.`: the branch was never registered. Use `git tree --json` for the forest,
  or register the branch you are on with `git tree attach <parent>`. (`git tree branch` does not
  do this: it creates or adopts a *child* branch at a new worktree path.)
- `confirmation required; pass -y/--yes`: expected under `--json`, which never prompts. Re-run
  with `-y`. Resume commands are the exception and need no `-y`.
- `(stale - run propagate first)` from `push`: the branch is behind its parent. Propagate, then
  push.
