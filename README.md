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

```sh
uv tool install -e /path/to/git_tree
```

This creates an isolated venv, installs `git-tree` in editable mode, and symlinks the executable to `~/.local/bin/git-tree`. Git auto-discovers it as `git tree`.

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
git tree continue                      # resume a cascade after resolving a conflict
git tree rebase <target>               # rebase current branch + descendants onto new base
git tree split                         # split current branch into parent + child
git tree push                          # push current branch + descendants (--force-with-lease)
git tree manpage [--install]           # emit the man page (roff); --install writes it to the man path
git tree --version                     # print git-tree <version>
```

Interactive commands also take flags so they can run unattended:

- `git tree split --after <commit> --name <branch> [--worktree <path> | --no-worktree]`: split with no prompts (omit any flag to be prompted for just that piece).
- `git tree split --child` inverts the split: the current branch (and its worktree) keeps the commits *up to* the split and stays the parent, while the new branch takes the *later* commits as a child; existing children follow the new branch. Default split does the reverse (the new branch is the parent, holding the earlier commits).
- `propagate`, `rebase`, `push`, `remove`, `rebuild`, `detach` accept `-y`/`--yes` to skip the confirmation prompt. (`--dry-run` on `propagate`/`rebase`/`push`/`remove` previews without executing.)
- `git tree rebuild [branch] [--force]`: rebuilds a worktree whose submodule state is corrupted (broken `.git` pointer, missing modules dir). Refuses if the worktree has uncommitted changes unless `--force` is passed.

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
# fsdp2-sharding was squash-merged into main; re-home bf16-mixed-precision onto main.
git tree rebase main

  main                                main
  └── fsdp2-sharding   (merged)   →   ├── fsdp2-sharding
      └── * bf16-mixed-precision      └── * bf16-mixed-precision
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

Works immediately after `git tree branch` or `git tree attach`, which record the fork commit.

### Propagate

After adding commits to a parent branch, run `git tree propagate` to rebase all descendants. Branches are processed in topological order (parents first), and each branch's result is printed as it completes. On conflict the cascade stops: the branches already rebased are shown, then git-tree exits (code 3) telling you where to resolve. Resolve the conflict and `git add` the files, then run `git tree continue` to finish the rebase, record the new fork point, and continue to the remaining descendants (it replaces the old `git rebase --continue` + `git tree propagate <parent>` two-step).

### Rebase

`git tree rebase <target>` (for when a parent is squash-merged upstream, see [Examples](#examples)) is equivalent to: `git rebase --onto <target> <fork-point>` + `git tree attach <target>` + `git tree propagate`.

### Push

`git tree push` pushes the current branch and all descendants with `--force-with-lease`. Branches that are stale (behind their parent) are skipped with a warning to run `propagate` first. It pushes with `-u`, so git also writes each pushed branch's own `branch.<b>.remote`/`.merge`; git-tree ignores those and always resolves the tree's remote from the root.

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

<details>
<summary><h2 style="display:inline">Agent mode (<code>--json</code>)</h2></summary>

git-tree is built to be driven by an AI agent (or any script) as well as by hand. Pass `--json` in any position (`git tree --json <cmd>` or `git tree <cmd> --json`) to enter **agent mode**, which:

- implies `--no-input` (never prompts: a prompt would deadlock an agent that isn't feeding stdin);
- prints **exactly one JSON object** on stdout; every diagnostic (the `+ git …` echoes, git's own output, warnings) goes to stderr;
- disables color.

### Envelope

The object is **flat** (no nesting). A success carries just `command` and `ok`:

```json
{"command": "propagate", "ok": true}
```

**Success is bare; re-query for state.** A mutation returns just `{ok: true}`; re-run `git tree --json` (the forest) for authoritative post-op state. The forest already carries it, so the envelope stays lean.

An error sets `ok: false` and adds an `error` object; it may also carry `branches` (the ones already processed) and a `remedy` (an argv array you can run):

```json
{
  "command": "push", "ok": false,
  "error": {"kind": "precondition", "code": 4, "message": "…"}
}
```

A conflict is `error.kind == "conflict"` plus the location and the resume command:

```json
{
  "command": "propagate", "ok": false,
  "error": {
    "kind": "conflict", "code": 3, "message": "…",
    "branch": "feat2", "worktree": "/abs/path",
    "conflicted_files": ["foo.py"],
    "remedy": ["git", "tree", "continue"]
  }
}
```

A confirmation you must supply comes back as `confirmation_required`; re-run your command with `-y`:

```json
{
  "command": "remove", "ok": false,
  "error": {
    "kind": "confirmation_required", "code": 4, "message": "confirmation required; pass -y/--yes"
  }
}
```

`remedy` (where present, e.g. the conflict envelope) is always an argv array, never a shell string.

### `error.kind`

Derived from the exit code: `usage` (2), `conflict` (3), `precondition` (4), `not_a_tree_branch` (5), `error` (1). Three specific overrides:

- `input_required`: a required value or flag is missing.
- `confirmation_required`: needs `-y`; re-run the command with it.
- `lease_rejected`: a `--force-with-lease` push was rejected because the remote moved.
- `unresolved_conflicts`: `git tree continue` was run with conflicts still unresolved.

### Forward-compat contract

Agents **must ignore unknown fields and default-arm unknown enum values**, so adding a field or a new `kind` is non-breaking. There is no version field in the envelope; a breaking change is just a breaking change, so pin the tool version if you need long-term stability.

### `-y`/`--yes`

`-y`/`--yes` skips the confirmation prompt on `propagate`/`rebase`/`push`/`remove`/`rebuild`/`detach`, the first-class way to run destructive ops unattended (no more `echo y | git tree …`). `--json` does **not** auto-imply it: it won't silently confirm a destructive op. Instead a needed confirmation returns `confirmation_required` (re-run with `-y`).

### The forest query

With no subcommand, `git tree --json` prints the whole forest (every branch's parent, children, root, remote, fork commit, worktree, and status): always the full forest, regardless of the current branch. It stays **backward-compatible**: `command` is `"tree"` and, the envelope being flat, its `roots`/`cycles`/`orphans`/`branches` keys sit as siblings, so existing consumers are unchanged. Each branch now also carries a `rebase_in_progress` boolean.

```json
{
  "command": "tree", "ok": true,
  "roots": ["main"],
  "cycles": [],
  "orphans": [],
  "branches": [
    {"name": "feat", "parent": "main", "children": ["feat2"], "root": "main",
     "remote": "origin", "fork_commit": "…", "worktree": "/abs/path",
     "dirty": true, "staged": 0, "modified": 1, "untracked": 0, "conflicted": 0,
     "rebase_in_progress": false, "ahead": 2, "behind": 0, "pending_from_parent": 3}
  ]
}
```

Worktree/status fields are `null` for a branch with no worktree; `parent`/`pending_from_parent` are `null` for a root. Broken branches appear here too, so you can see their state while repairing a tree: an orphaned branch (its configured parent is gone) carries `orphaned_parent: "<missing parent>"`, and a branch in a dependency cycle carries `cyclic: true` (the cycle itself is listed in `cycles`). Those tags mark a branch as not a healthy root.

### Other agent surface

- **`git tree push`** returns `{ok: true}` with a `skipped` list of branches it did **not** push, each `{branch, reason}`, where `reason` is `"stale"` (behind its parent, run `propagate` first) or `"ancestor_not_pushed"`. On any push failure it exits non-zero with `error.kind` `lease_rejected` (the remote moved) or a generic `error`, and `error.branches` naming the failures.

- **`git tree continue`** resumes a cascade after you resolve a conflict: it finishes the in-progress rebase (editor disabled, so no `$EDITOR` hang), records the new fork point, and re-propagates from the tree root so every branch the cascade would have reached is covered.

- **`--no-input`** (global, without `--json`) never prompts: if a value would be asked for interactively (a confirmation, a branch/parent selection, a name), it errors instead, naming the flag that supplies it. Compose with `--yes` to auto-confirm confirmations while still erroring on other missing input.

- **Exit codes** let you branch on the failure class: `0` success, `2` usage error, `3` resumable conflict (resolve, then `git tree continue`), `4` precondition/dirty state, `5` not a tree-branch.

- **Discovering the command surface**: `git tree -h` (or `git-tree --help`) lists every subcommand and flag; the help epilog has a `FOR AGENTS` section. `git tree --help` works too once the man page is installed (see Install). All three share one source, the argparse parser.

</details>
