# git-tree

Cascading rebase tool for branch dependency chains. Manages branches that form trees (A → B → C) and automates propagating changes downstream.

## Design philosophy

**Goal**: edit any branch in a dependency tree (each in its own worktree) with plain git and propagate the change to all descendants, replaying only each branch's own commits so already-upstream work never re-conflicts. See README "Goals".

git-tree is a deliberately **light wrapper around plain git**. It automates the bookkeeping of cascading rebases but should never obscure what git is doing. Two principles guide changes:

- **Minimal state**: dependency edges live in git config (see Architecture), not external files or commit labels. Anything git already knows is read from git, never duplicated.
- **Explicit and transparent**: prefer surfacing the underlying git operations over hiding them. Side-effecting commands echo the git invocation and reprint git's own output (`git_echo`); `git tree log` streams git directly. Data-query calls and the internal tree-config bookkeeping (the `branch.<name>.tree-*` / root `remote` writes) are captured silently — each command narrates the structural change in prose instead. When in doubt, show the git command and its output rather than a hand-rolled summary.
- **Standalone**: git-tree lives inside a larger repo today but is written as an independent piece of work that could be `git init`'d into its own repo tomorrow with zero edits. Nothing under `git_tree/` may presume that host or read/write files outside it: use location-neutral paths like `/path/to/git_tree`, keep install/man-page logic self-contained (no external scripts), and let `pyproject.toml` declare everything needed to build alone. A host repo may wrap the install as an *external consumer*, but that wrapper lives on the host's side, never here — such integration is the host's concern, not git-tree's.

## Install

```sh
uv tool install -e /path/to/git_tree   # editable, on PATH as git-tree
```

No git alias needed — git auto-discovers `git-tree` on PATH as `git tree`.

**Man page**: `git tree manpage --install` writes `~/.local/share/man/man1/git-tree.1` (roff generated from the argparse parser, so it never drifts from `-h`). This is what makes `git tree --help` work: git routes `--help` to `man git-tree`. `uv tool install` can't place a discoverable page (it symlinks only the script onto PATH; the packaged page stays in the venv), so git-tree writes its own. Keep it self-contained here (no external script) so it survives if `git_tree` becomes a standalone repo.

## Dev commands

```sh
uv sync                          # install deps
uv run pytest tests/ -q          # run tests
uv run ruff check . --fix        # lint + autofix
uv run ruff format .             # format
uv run ty check git_tree/        # type check
```

## Architecture

Single module: `git_tree/cli.py`. All commands, git helpers, graph discovery, and tree display in one file. Entry point: `git_tree.cli:main` (registered as `git-tree` console script).

**Dependency storage** (git config, no external files/commit labels):
- `branch.<name>.tree-parent-branch <parent>` — the parent branch (structural edge)
- `branch.<name>.tree-fork-commit <commit>` — the parent tip this branch last rebased onto;
  the `--onto <old-base>` exclude boundary, set on branch/attach/split and updated after each
  successful rebase. Required for correct propagate once a parent moves ahead of its child
  (`merge-base` drifts). `_get_fork_commit`/`_set_fork_commit` manage it; a missing key falls
  back to `merge-base`.

**Key abstractions**:
- `Graph` dataclass: `parent_of`, `children_of`, `branches` dicts + `downstream_from()` BFS. Also carries `worktree_of` (path per branch, roots included — roots have no `BranchInfo`), and `cycles`/`orphans` diagnostics surfaced by discovery.
- `BranchInfo` dataclass: `name`, `worktree` (optional Path), `fork_commit`, `is_dirty`. A tree has one remote, defined on its **root** (`branch.<root>.remote`); push and status resolve it via `_root_remote`/`root_of` rather than per-branch.
- `discover()`: reads worktree list + git config to build the graph

**Submodule awareness** (helpers near `_require_clean_state`):
- `_submodule_paths(worktree)`: parses `.gitmodules` via `configparser`, returns paths that exist on disk.
- `_check_submodule_health(worktree, submodule_path)`: resolves `.git` file → gitdir target → checks HEAD exists. Never shells out (the submodule may be corrupted).
- `_require_healthy_submodules(branches, graph)`: pre-flight gate in `propagate`/`rebase`. Must run BEFORE `_require_clean_state` (`git status` crashes on corrupted submodules).
- `_init_submodules(worktree)`: runs `git submodule update --init --recursive` via `git_echo_ok`.
- `_force_remove_worktree(path, branch)`: multi-stage removal (worktree remove → shutil.rmtree + prune → verify).

**Agentic surface** (keep these working when editing — they exist for non-interactive/agent use):
- `git tree --json` = **agent mode** (global, any position): emits exactly one JSON envelope on stdout, routes all diagnostics (git echoes, warnings) to stderr, and disables color. Implies `--no-input` (a prompt would deadlock an agent).
- **Envelope** (`_envelope`/`_error_envelope`, flat — no nesting): success is `{command, ok:true}`; mutations stay bare (re-query the forest for state). The no-subcommand forest query is `command:"tree"` and keeps its `roots`/`cycles`/`orphans`/`branches` siblings (backward-compatible), with each branch gaining a `rebase_in_progress` bool. Error adds `ok:false` + `error:{kind, code, message}` (optional `branches`, `remedy` argv); a `ConflictError` adds `branch`/`worktree`/`conflicted_files` and `remedy:["git","tree","continue"]`.
- **`error.kind`** derives from the exit code via `_KIND_BY_CODE` (usage/conflict/precondition/not_a_tree_branch, else `error`), overridden by `TreeError.kind`: `input_required`, `confirmation_required` (re-run with `-y`), `lease_rejected` (push `--force-with-lease` rejection), `unresolved_conflicts` (`continue` with conflicts still unresolved / not yet `git add`ed). Forward-compat: consumers ignore unknown fields and default-arm unknown enums (no envelope version field; pin the tool for stability).
- **Forest extras**: `git tree --json` `branches[]` also lists broken branches — `orphaned_parent: <missing parent>` (configured parent gone) or `cyclic: true` (in a dependency cycle) — with their worktree/status, so an agent can repair them. `cmd_push` returns `{skipped: [{branch, reason}]}` (reason `stale`/`ancestor_not_pushed`) on success — the one non-bare success payload, since the skip set isn't re-derivable.
- `-y`/`--yes` skips confirmation on propagate/rebase/push/remove/repair/detach. `--json` does **not** auto-imply it (`_proceed` raises `confirmation_required` instead of silently confirming).
- `git tree continue` (`cmd_continue`): resumes a cascade after a conflict — finishes the in-progress rebase (editor disabled), records the new fork point, propagates to descendants. Replaces raw `git rebase --continue` + `git tree propagate`.
- `--version` prints `git-tree <version>` (`_version`, the package version).
- `--no-input` (`_no_input`/`_require_input`, threaded via `args`): errors instead of prompting.
- Exit codes via `TreeError(msg, code=…)`: 3 resumable conflict, 4 precondition/state, 5 not-a-tree-branch (1 generic, 2 argparse usage).
- `--dry-run` on propagate/rebase/push/remove.
- `git tree manpage [--install]` (`_render_manpage`/`cmd_manpage`): roff man page generated from the argparse parser (single source of truth with `-h`/`--help`); `--install` writes it to the man path so `git tree --help` works. Handled inline in `main()` since it needs the parser.

## Testing

Real git operations against isolated repos (no mocking). The `repo` fixture (`tests/conftest.py`) creates a bare origin + clone in `tmp_path` and `chdir`s into it. `RepoHelper` provides `commit()`, `branch()`, `checkout()`, `set_parent()`, `worktree()`, `push()`.

## Conventions

- Python 3.11+, stdlib only (no runtime deps)
- ruff for lint+format (line-length 100, select E/F/I/UP/B/SIM/TCH, TCH ignored in tests)
- ty for type checking
- Tests assert behavior, not implementation details
- Commits use conventional format with `(git_tree)` scope: `feat(git_tree): ...`, `fix(git_tree): ...`, `test(git_tree): ...`
