---
name: git-tree-land
description: Clean up a git-tree stack after a branch was squash-merged upstream, hoisting its children onto the parent and dropping the merged branch. Use when a PR at the bottom of a stack was squash-merged or merged into main, or when asked to rebase a stack onto the newly merged main.
---

# Land a squash-merged branch and re-home its children

A squash-merged branch's work reaches its parent as one new commit, not as the original commits.
Run these steps in order. Each ordering constraint below is load-bearing: getting one wrong
silently destroys work or tears down worktrees.

## 1. Identify the branches

```sh
git tree --json
```

Name three things to the user before touching anything:

- the **merged branch**, whose PR was squash-merged
- its **parent**, from that branch's `parent` field, usually `main`
- its **children**, from its `children` field, each with its own `worktree` path

Every child and descendant needs a worktree that exists, has healthy submodules, and has no
unresolved conflicts or hand-started rebase. Uncommitted changes do not block the cascade:
git-tree stashes a dirty worktree and pops it afterward, though the pop can conflict and a
conflict mid-cascade leaves the stash unpopped, so committing first is still tidier.

## 2. Update the parent first

```sh
git -C <parent-worktree> pull --ff-only
```

Confirm the parent now contains the squashed commit before going further. Each child records a
`tree-fork-commit` marking where its own work begins, and step 3 replays only commits after it, on
the assumption that everything before it is already in the parent. Rebase a child while the parent
still lacks the squashed commit and the merged branch's work is excluded from the replay while
absent from the parent, which is to say it is gone.

## 3. Rebase each child onto the parent

Name the child as the second argument, so this runs from anywhere, once per direct child:

```sh
git tree rebase <parent> <child> -y
```

The target must be a local branch. `origin/main`, a tag, or a raw commit is refused. Order among
children does not matter, and each child's subtree is independent. Do every direct child.

On a conflict the cascade stops and resumes with `git tree propagate <child>`, never by re-running
`rebase`. Follow the `git-tree-propagate` skill to resolve it with the user.

## 4. Drop the merged branch

Only after every child has moved. Confirm its `children` is empty in `git tree --json`, then:

```sh
git tree remove <merged-branch> -y    # removes the worktree, keeps the branch ref
git tree detach <merged-branch> -y    # unregisters from the tree, keeps worktree and ref
```

`remove` refuses if your shell is inside the worktree being deleted or you are on that branch, so
`cd` elsewhere first.

## Ordering constraints

- Dropping the merged branch before its children move: `detach` orphans them into a separate tree
  still based on the merged branch, and `remove` tears down the worktrees of the whole subtree,
  children included.
- Rebasing a child before the parent has pulled: drops the merged work, per step 2.

Never hand-edit `branch.<name>.tree-fork-commit` or `branch.<name>.tree-parent-branch`.
`git tree rebase` maintains both.
