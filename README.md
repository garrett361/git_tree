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

## Usage

```sh
git tree                               # show the current branch's tree
git tree --all                         # show every tree
git tree branch <path> <name>          # create or adopt a child branch with a worktree
git tree attach [parent]               # attach current branch to tree
git tree detach                        # remove current branch from tree (keeps branch + worktree)
git tree remove [branch]               # remove a subtree's worktrees + unregister its branches (keeps refs)
git tree rebuild [branch]              # rebuild a corrupted worktree from the branch tip (keeps branch + tree config)
git tree propagate                     # cascade current branch's changes to descendants
git tree continue <origin>             # resume a cascade after a conflict (from the origin branch it began at)
git tree rebase <target>               # rebase current branch + descendants onto new base
git tree split                         # split current branch into parent + child
git tree push                          # push current branch + descendants (--force-with-lease)
git tree log                           # show a git log graph across the whole tree
git tree manpage [--install]           # emit the man page (roff); --install writes it to the man path
git tree completions <zsh|bash>        # emit the shell completion script
git tree --version                     # print git-tree <version>
```

> [!WARNING]
> These commands touch more than the current branch:
>
> - **`propagate`, `rebase`, `push`**: the current branch and all its descendants (rebase or force-push the whole subtree).
> - **`continue <origin>`**: resumes the cascade from `origin` (the branch the original `propagate`/`rebase` began at) and its descendants — not the whole tree.
> - **`remove`**: deletes worktrees and unregisters the entire subtree (branch refs are kept).
> - **`split --child`**: re-points the current branch's existing child branches onto the new branch.
>
> `propagate`/`rebase`/`push`/`remove` support `--dry-run` to preview first.

Interactive commands (`split`, and the confirmation-gated `propagate`/`rebase`/`push`/`remove`/`rebuild`/`detach`) take flags to run without prompting: `-y`/`--yes` skips a confirmation, and `--dry-run` previews `propagate`/`rebase`/`push`/`remove` first. See `git tree <cmd> -h` for each command's flags.

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
#   2. git tree rebase main             (from bf16-mixed-precision: re-parent onto main,
#                                        drop fsdp2-sharding's merged commits, propagate to bf16-eval)
#   3. git tree remove fsdp2-sharding   (tear down the merged, now-childless parent)

  main                                main
  └── fsdp2-sharding  (merged)        ├── fsdp2-sharding          (merged; remove it)
      └── * bf16-mixed-precision  →   └── * bf16-mixed-precision  (re-homed onto main)
          └── bf16-eval                   └── bf16-eval           (propagated)
```

`git tree rebase` excludes the old parent's now-redundant commits and cascades the result to
every descendant, so the whole subtree comes along.

## How it works

The tree lives entirely in git config: no external files, no commit labels, no hooks. Each
branch records its edge and fork point, and the tree's single remote lives on the root:

```
git config branch.<name>.tree-parent-branch <parent-branch>   # which branch it stacks on
git config branch.<name>.tree-fork-commit   <commit>          # where it forks from that parent
git config branch.<root>.remote             <remote>          # the tree's one remote (on the root)
```

`tree-parent-branch` is the structural edge; `tree-fork-commit` is the parent's tip the
branch was last rebased onto (set on `branch`/`attach`/`split` and updated after every
successful rebase). The fork commit is what lets a rebase replay *only* the branch's own
commits: once a parent moves ahead of its child, `merge-base(parent, child)` drifts off the
real fork, so the stored commit is the only reliable boundary. This is what makes an
interrupted propagate resumable, and keeps a reorder/split or `git pull --rebase` of a parent
from corrupting its descendants.

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

