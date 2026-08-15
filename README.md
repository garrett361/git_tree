# git-tree

[![CI](https://github.com/garrett361/git_tree/actions/workflows/ci.yml/badge.svg)](https://github.com/garrett361/git_tree/actions/workflows/ci.yml)

Manage a tree of stacked branches, one worktree per branch. Propagate a change to every descendant, rebasing only what's needed, and reshape the tree as you go.

git-tree models your branches as a tree: each branch stacks on a parent, and a parent can have any number of children. Each lives in its own git worktree, so the whole tree stays checked out at once. You can edit, build, and run code on several branches in parallel, with no stashing to switch between them. For example:

```
main
├── fused-rmsnorm-kernel      ~/worktrees/fused-rmsnorm-kernel      [⇡3]
│   ├── rmsnorm-triton-bench  ~/worktrees/rmsnorm-triton-bench
│   └── rmsnorm-unit-tests    ~/worktrees/rmsnorm-unit-tests        [!1]
└── fsdp2-sharding            ~/worktrees/fsdp2-sharding
    └── bf16-mixed-precision  ~/worktrees/bf16-mixed-precision      [2 new]
```

Amend the kernel on `fused-rmsnorm-kernel` and `git tree propagate` rebases `rmsnorm-triton-bench` and `rmsnorm-unit-tests` onto the new tip, replaying only each branch's own commits so upstream work never re-conflicts. Land a fix on `main` (say a dataloader change every branch depends on) and it cascades through the entire tree in one command.

git-tree also grows and reshapes the tree: create a child branch, split one branch into two, attach or detach a subtree, or rehome a stack whose base merged upstream. See [Examples](#examples) for what each command does to the structure.

By design git-tree is a **light wrapper around plain git**: it automates the bookkeeping of a cascading rebase and nothing more. Every non-trivial git command it runs is echoed with its output, so you always see what it did and can drop back to plain git at any point. Because it rewrites history, it's meant for stacks you own and force-push, not shared branches.

## Install

Direct from github:

```sh
uv tool install git+https://github.com/garrett361/git_tree
```

This creates an isolated venv, installs `git-tree`, and symlinks the executable to `~/.local/bin/git-tree`. Git auto-discovers it as `git tree`. Update later with `uv tool upgrade git-tree`.

For local development, install from a clone in editable mode instead:

```sh
uv tool install -e /path/to/git_tree
```

To make `git tree --help` work, install the man page once:

```sh
git tree manpage --install   # writes ~/.local/share/man/man1/git-tree.1
```

Without it, `git tree --help` fails (git looks for a man page); use `git tree -h` or `git-tree --help` instead.

### Agent skills

git-tree bundles three [Agent Skills](https://agentskills.io) covering the workflows that are easiest to get wrong: cleaning up after a squash-merge, resolving a conflict mid-cascade, and repairing a broken tree. Install them once:

```sh
git tree skills --install   # links into ~/.claude/skills and ~/.agents/skills
```

They install at user scope, so they apply in every repo you use git-tree in, not just this one. Claude Code invokes them as `/git-tree-land`, `/git-tree-propagate`, `/git-tree-doctor`; Codex as `$git-tree-land` and so on; both also load them on their own when the task matches. `git tree skills` lists what is bundled and where it would go, and `--dir DIR` installs somewhere else. The install symlinks, so a `git pull` in an editable clone updates both harnesses with no reinstall, and re-running it repairs links left dangling by a moved clone. It refuses rather than overwriting anything at those paths that git-tree did not write.

## Usage

```sh
git tree                               # show the current branch's tree
git tree --all                         # show every tree
git tree branch <path> <name>          # create or adopt a child branch with a worktree
git tree attach [parent]               # attach current branch to tree
git tree detach [branch]               # remove a branch from the tree (default: current; keeps branch + worktree)
git tree remove [branch]               # remove a subtree's worktrees + unregister its branches (keeps refs)
git tree rebuild [branch]              # rebuild a corrupted worktree from the branch tip (keeps branch + tree config)
git tree propagate [branch]            # cascade a branch's changes to descendants (default: current; also resumes after a conflict)
git tree rebase <target> [branch]      # reparent + rebase a branch onto <target>, then propagate (default: current)
git tree split                         # split current branch into parent + child
git tree push [branch]                 # push a branch + descendants (default: current; --force-with-lease)
git tree log                           # show a git log graph across the whole tree
git tree skills [--install]            # list the bundled agent skills; --install links them into your harnesses
git tree manpage [--install]           # emit the man page (roff); --install writes it to the man path
git tree completions <zsh|bash>        # emit the shell completion script
git tree --version                     # print git-tree <version>
```

> [!WARNING]
> These commands touch more than the current branch:
>
> - **`propagate`, `rebase`, `push`**: the named branch (default: current) and all its descendants (rebase or force-push the whole subtree). `push` always acts on the current branch.
> - **`remove`**: deletes worktrees and unregisters the entire subtree (branch refs are kept).
> - **`split --child`**: re-points the current branch's existing child branches onto the new branch.
>
> `propagate`/`rebase`/`push`/`remove` support `--dry-run` to preview first.

Interactive commands (`split`, and the confirmation-gated `propagate`/`rebase`/`push`/`remove`/`rebuild`/`detach`) take flags to run without prompting: `-y`/`--yes` skips a confirmation, and `--dry-run` previews `propagate`/`rebase`/`push`/`remove` first. `remove` and `rebuild` take `--force` to act despite anything they would otherwise refuse to destroy: uncommitted changes (in the worktree or a submodule), a rebase left in progress, or a worktree too broken to inspect. Each refusal names what it found, and `--force` destroys exactly that. See `git tree <cmd> -h` for each command's flags.

## Examples

Each example shows the tree before → after; `*` marks the branch you run the command from.

```
# On fused-rmsnorm-kernel, add a child branch to try an fp8 variant.

git tree branch ~/worktrees/rmsnorm-fp8 rmsnorm-fp8

  * fused-rmsnorm-kernel        * fused-rmsnorm-kernel
  ├── rmsnorm-triton-bench      ├── rmsnorm-triton-bench
  └── rmsnorm-unit-tests    →   ├── rmsnorm-unit-tests
                                └── rmsnorm-fp8  ← new
```

```
# On bf16-mixed-precision (which has a child bf16-eval), peel its earlier autocast
# commits up into a new base branch; the child stays beneath bf16-mixed-precision.

git tree split

  fsdp2-sharding                  fsdp2-sharding
  └── * bf16-mixed-precision      └── bf16-autocast               ← new base (earlier commits)
      └── bf16-eval           →       └── * bf16-mixed-precision  (later commits)
                                          └── bf16-eval           (unchanged)
```

```
# On bf16-mixed-precision, peel its later grad-clip commits down into a new child;
# its existing child bf16-eval follows onto the new branch.

git tree split --child

  fsdp2-sharding                  fsdp2-sharding
  └── * bf16-mixed-precision      └── * bf16-mixed-precision  (earlier commits, kept)
      └── bf16-eval           →       └── bf16-grad-clip      ← new child (later commits)
                                          └── bf16-eval       (followed the new branch)
```

```
# Move bf16-mixed-precision from fsdp2-sharding over to fused-rmsnorm-kernel.

git tree detach && git tree attach fused-rmsnorm-kernel

  main                                main
  ├── fused-rmsnorm-kernel            ├── fused-rmsnorm-kernel
  └── fsdp2-sharding              →   │   └── * bf16-mixed-precision
      └── * bf16-mixed-precision      └── fsdp2-sharding
```

```
# fsdp2-sharding is a feature branch of several commits with a stack on top. You PR it and
# it gets squash-merged into main. To re-home the stack onto the merged base:
#   1. git pull                         (in main's worktree: pick up the squash commit)
#   2. git tree rebase main bf16-mixed-precision
#                                       (re-parent onto main, drop fsdp2-sharding's merged
#                                        commits, propagate to bf16-eval)
#   3. git tree remove fsdp2-sharding   (tear down the merged, now-childless parent)

  main                                main
  └── fsdp2-sharding  (merged)    →   └── * bf16-mixed-precision  (re-homed onto main)
      └── * bf16-mixed-precision           └── bf16-eval           (propagated)
          └── bf16-eval
```

(After step 3, `fsdp2-sharding` is gone from the tree; its branch ref is kept.)

`git tree rebase` reparents a branch (the second argument, or the current branch) onto `<target>` and rebases the branch's own
commits there, excluding the old parent's now-redundant commits (via the recorded fork boundary,
so a squash-merged parent's commits drop out), then cascades the result to every descendant.
Think of it as three steps:

1. `git tree attach <target>`: record the new parent (no history change).
2. rebase the branch itself onto `<target>`.
3. `git tree propagate`: cascade the descendants.

The middle step is the part `attach` and `propagate` don't do.

### Resuming after a conflict

When a `propagate` or `rebase` hits a merge conflict, the cascade stops with one worktree
mid-rebase and prints the exact command to resume with. Resolve the conflicts and `git add`
them, then **re-run `git tree propagate <branch>`**, naming the branch you were operating on.
git-tree finishes the interrupted rebase for you (no raw `git rebase --continue`) and continues
the cascade, scoped to that branch's subtree. A `rebase` conflict resumes the same way: the
reparent is already recorded when it stops, so `git tree propagate <branch>` finishes the branch's
rebase onto its new target and cascades.

Example:

```
# You add commits to the `dataloader` worktree and propagate the change down.
# Rebasing `augment` conflicts, so the cascade stops mid-rebase.

git tree propagate dataloader

  * dataloader (new commits)     * dataloader
  └── augment              →      └── augment      ← CONFLICT: cascade stops
      └── sampler                     └── sampler  (not reached yet)

# Resolve the conflict in augment's worktree, `git add` the files,
# and finish with `git tree propagate dataloader` (no need for `git rebase --continue`).
# augment's rebase finishes and propagation continues to sampler.

git tree propagate dataloader

  * dataloader                   * dataloader
  └── augment              →      └── augment      (resolved + rebased)
      └── sampler                     └── sampler  (propagated)
```

## How it works

The tree lives entirely in git config: no external files, no commit labels, no hooks. Each
branch records its edge and fork point, and the tree's single remote lives on the root:

```
git config branch.<name>.tree-parent-branch <parent-branch>   # which branch it stacks on
git config branch.<name>.tree-fork-commit   <commit>          # where it forks from that parent
git config branch.<root>.remote             <remote>          # the tree's one remote (on the root)
```

The two keys answer two questions: `tree-parent-branch` is *which branch this one stacks
on*, and `tree-fork-commit` is *where it forked from that parent* (the parent tip it was last
rebased onto, set on `branch`/`attach`/`split` and updated after every successful rebase). The
fork commit is what lets a rebase replay *only* the branch's own commits: once the parent
moves ahead, `merge-base(parent, child)` drifts off the real fork point, so the stored commit
is the only reliable boundary. That is what makes an interrupted propagate resumable and keeps
a parent's reorder, split, or `git pull --rebase` from corrupting its descendants.

For details of what each subcommand does under the hood, see [AGENTS.md](AGENTS.md).

## Worktrees

All branches in the tree must have linked worktrees. Operations that touch multiple branches (propagate, rebase, push) verify this upfront and abort with an error listing any branches missing worktrees. Dirty worktrees are automatically stashed/popped during rebase.

## Submodules

`git tree branch` automatically runs `git submodule update --init --recursive` after creating the worktree (skip with `--no-submodule-init`). `propagate` and `rebase` check submodule health before starting: if a worktree's submodule `.git` state is corrupted, they abort with a message pointing to `git tree rebuild`.

## Development

```sh
uv sync
uv run pytest tests/ -q
uv run ruff check . --fix
uv run ruff format .
uv run ty check git_tree/
```

## Agent mode (`--json`)

git-tree can be driven by an AI agent or any script. Pass `--json` in any position (`git tree --json <cmd>` or `git tree <cmd> --json`) to get **exactly one JSON object** on stdout, with every diagnostic on stderr; it also implies `--no-input`, so it never prompts. The full contract (the envelope shape, `error.kind` values, exit codes, and the `git tree --json` forest query) is documented for agents in [AGENTS.md](AGENTS.md).

