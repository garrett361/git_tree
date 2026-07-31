# git-tree

Cascading rebase tool for branch dependency chains. Manages branches that form trees (A → B → C) and automates propagating changes downstream.

## Design philosophy

**Goal**: edit any branch in a dependency tree (each in its own worktree) with plain git and propagate the change to all descendants, replaying only each branch's own commits so already-upstream work never re-conflicts. See README "Goals".

git-tree is a deliberately **light wrapper around plain git**. It automates the bookkeeping of cascading rebases but should never obscure what git is doing. Two principles guide changes:

- **Minimal state**: dependency edges live in git config (see Architecture), not external files or commit labels. Anything git already knows is read from git, never duplicated.
- **Explicit and transparent**: prefer surfacing the underlying git operations over hiding them. Side-effecting commands echo the git invocation and reprint git's own output (`git_echo`); `git tree log` streams git directly. Data-query calls and the internal tree-config bookkeeping (the `branch.<name>.tree-*` / root `remote` writes) are captured silently — each command narrates the structural change in prose instead. When in doubt, show the git command and its output rather than a hand-rolled summary.
- **Standalone**: git-tree lives inside a larger repo today but is written as an independent piece of work that could be `git init`'d into its own repo tomorrow with zero edits. Nothing under `git_tree/` may presume that host or read/write files outside it: use location-neutral paths like `/path/to/git_tree`, keep install/man-page/skill-install logic self-contained (no external scripts), and let `pyproject.toml` declare everything needed to build alone. A host repo may wrap the install as an *external consumer*, but that wrapper lives on the host's side, never here — such integration is the host's concern, not git-tree's.

## Install

```sh
uv tool install -e /path/to/git_tree   # editable, on PATH as git-tree
```

No git alias needed — git auto-discovers `git-tree` on PATH as `git tree`.

**Skills**: `git tree skills --install` symlinks the bundled [Agent Skills](https://agentskills.io) under `git_tree/skills/` into `~/.claude/skills/` (Claude Code) and `~/.agents/skills/` (Codex), at user scope so they apply in every repo. Both harnesses read the same `<name>/SKILL.md` layout, ignore frontmatter keys they don't know, and follow directory symlinks, so one bundled copy serves both and an editable install stays live. `--dir DIR` installs elsewhere. Install only ever writes symlinks, and ownership is decided by the link's *stored target* shape (`.../git_tree/skills/<name>`), not by resolving it: a moved clone, a non-editable reinstall, or a rebuilt uv venv would otherwise make git-tree disown its own links and refuse to reinstall over them, and reading rather than resolving keeps a dangling link repairable. Anything that is not such a symlink belongs to someone else and is refused rather than overwritten.

`git_tree/skills/` is the **source of truth for user-facing workflow procedures** (squash-merge cleanup, conflict resolution, repairing a broken tree). It is deliberately not duplicated here: those procedures run on machines that have no clone of this repo, so they have to ship with the package. Change resume, rebase, or repair semantics and the matching `SKILL.md` is a second edit site. `tests/test_skills.py` asserts every `git tree …` in them still parses under the real parser, but it cannot catch semantic drift.

**Man page**: `git tree manpage --install` writes `~/.local/share/man/man1/git-tree.1` (roff generated from the argparse parser, so it never drifts from `-h`). This is what makes `git tree --help` work: git routes `--help` to `man git-tree`. `uv tool install` can't place a discoverable page (it symlinks only the script onto PATH; the packaged page stays in the venv), so git-tree writes its own. Keep it self-contained here (no external script) so it survives if `git_tree` becomes a standalone repo.

## Dev commands

```sh
uv sync                          # install deps
uv run pytest tests/ -q          # run tests
uv run ruff check . --fix        # lint + autofix
uv run ruff format .             # format
uv run ty check git_tree/        # type check
```

Verify everything before committing (checking forms, no mutation — mirrors CI):

```sh
uv run ruff format --check . && uv run ruff check . && uv run ty check git_tree/ && uv run pytest tests/ -q
```

Fast inner loop: test files map 1:1 to commands (`test_rebase.py`, `test_push.py`, ...), so iterate on the matching file (seconds) and run the full suite (~2.5 min) before committing.

## Adding a subcommand

A command touches two sites:

1. The `cmd_<name>(args)` handler.
2. `sub.add_parser(...)` in `_build_parser()`, with `.set_defaults(func=cmd_<name>)`. The parser is the single source of truth: that one block wires dispatch (`main()` calls `args.func`), `-h`, the man page (`_render_manpage`), and both shell completions (`_render_completions`). If a value arg should complete branches or paths, tag it with `_set_completer(parser.add_argument(...), "git_heads"/"directories")`.

Commands that emit non-envelope output (`manpage`, `completions`) or have no JSON form (`log`) are special-cased in `main()` before the `args.func` dispatch; `manpage`/`completions` therefore set no `func` (and `cmd_manpage` takes the parser, so it could not be dispatched generically anyway). Completions are generated from the parser, so they cannot drift; `tests/test_agentic.py::TestCompletionGeneration` asserts the generated scripts complete the right tokens per subcommand and parse under the real shells.

## Architecture

Single module: `git_tree/cli.py`. All commands, git helpers, graph discovery, and tree display in one file. Entry point: `git_tree.cli:main` (registered as `git-tree` console script).

**Dependency storage** (git config, no external files/commit labels):
- `branch.<name>.tree-parent-branch <parent>` — the parent branch (structural edge)
- `branch.<name>.tree-fork-commit <commit>` — the parent tip this branch last rebased onto;
  the `--onto <old-base>` exclude boundary, set on branch/attach/split and updated after each
  successful rebase. Required for correct propagate once a parent moves ahead of its child
  (`merge-base` drifts). `_get_fork_commit`/`_set_fork_commit` manage it; a missing key falls
  back to `merge-base`. This boundary is what makes `git tree rebase <target>` equivalent to
  `git rebase --onto <target> <fork-commit>` + `git tree attach <target>` + `git tree propagate`.

**Key abstractions**:
- `Graph` dataclass: `parent_of`, `children_of`, `branches` dicts + `downstream_from()` BFS. Also carries `worktree_of` (path per branch, roots included — roots have no `BranchInfo`), and `cycles`/`orphans` diagnostics surfaced by discovery.
- `BranchInfo` dataclass: `name`, `worktree` (optional Path), `fork_commit`, `is_dirty`. A tree has one remote, defined on its **root** (`branch.<root>.remote`); push and status resolve it via `_root_remote`/`root_of` rather than per-branch.
- `discover()`: reads worktree list + git config to build the graph

**Submodule awareness** (helpers near `_require_clean_state`):
- `_submodule_paths(worktree)`: parses `.gitmodules` via `configparser`, returns paths that exist on disk. Raises `TreeError` when the file cannot be parsed (git accepts things `configparser` rejects, e.g. a repeated `[submodule "x"]`); callers deciding whether deleting is safe must treat that as "cannot prove clean" rather than "no submodules".
- `_check_submodule_health(worktree, submodule_path)`: resolves `.git` file → gitdir target → checks HEAD exists. Never shells out (the submodule may be corrupted).
- `_require_healthy_submodules(branches, graph)`: pre-flight gate in `propagate`/`rebase`. Must run BEFORE `_require_clean_state` (`git status` crashes on corrupted submodules).
- `_init_submodules(worktree)`: runs `git submodule update --init --recursive` via `git_echo_ok`.
- `_force_remove_worktree(path, branch)`: multi-stage removal (worktree remove → shutil.rmtree + prune → verify).

**Agentic surface** (keep these working when editing — they exist for non-interactive/agent use):
- `git tree --json` = **agent mode** (global, any position): emits exactly one JSON envelope on stdout, routes all diagnostics (git echoes, warnings) to stderr, and disables color. Implies `--no-input` (a prompt would deadlock an agent).
- **Envelope** (`_envelope`/`_error_envelope`, flat — no nesting): success is `{command, ok:true}`; mutations stay bare (re-query the forest for state). The no-subcommand forest query is `command:"tree"` and keeps its `roots`/`cycles`/`orphans`/`branches` siblings (backward-compatible), with each branch gaining a `rebase_in_progress` bool. Error adds `ok:false` + `error:{kind, code, message}` (optional `branches`, `remedy` argv); a `ConflictError` adds `branch`/`worktree`/`conflicted_files` and `remedy:["git","tree","propagate", <branch>]` — re-running that command finishes the interrupted rebase and continues the cascade (a single runnable argv; no raw `git rebase --continue`).
- **`error.kind`** derives from the exit code via `_KIND_BY_CODE` (usage/conflict/precondition/not_a_tree_branch, else `error`), overridden by `TreeError.kind`: `input_required`, `confirmation_required` (re-run with `-y`), `lease_rejected` (push `--force-with-lease` rejection), `unresolved_conflicts` (a resume — re-run `propagate` — with conflicts still unresolved / not yet `git add`ed). Every one of these is a member of the `ErrorKind` `StrEnum`, which is the closed set: raise with a member, never a bare string, so a tag that does not exist fails at the raise site instead of reaching a consumer. It is a `StrEnum`, so it serializes and interpolates as the bare tag. Forward-compat: consumers ignore unknown fields and default-arm unknown enums (no envelope version field; pin the tool for stability).
- **Forest extras**: `git tree --json` `branches[]` also lists broken branches — `orphaned_parent: <missing parent>` (configured parent gone) or `cyclic: true` (in a dependency cycle) — with their worktree/status, so an agent can repair them. `cmd_push` returns `{skipped: [{branch, reason}]}` (reason `stale`/`ancestor_not_pushed`) on success — the one non-bare **mutation** payload, since the skip set isn't re-derivable. Queries do return payloads (`cmd_tree`'s forest, `cmd_skills`' listing); the bare-`{ok:true}` rule is about mutations.
- `-y`/`--yes` skips confirmation on propagate/rebase/push/remove/rebuild/detach. `--json` does **not** auto-imply it (`_proceed` raises `confirmation_required` instead of silently confirming).
- **Resuming a conflict** (there is no `continue` subcommand): the resume verb is `git tree propagate <branch>`, naming the branch that was being operated on; the procedure for driving one is the `git-tree-propagate` skill. `cmd_propagate` detects an in-progress rebase in scope (the named branch itself, left by a `git tree rebase`, or a descendant) and finishes it (`_advance_branch`: `git rebase --continue` with the editor disabled, rerere on, empty commits skipped, fork recorded at the actual replay base), then cascades. Resume is scoped to `<branch>`'s subtree, so it never sweeps siblings. A resume skips the confirm prompt (so the `remedy` argv runs without `-y`). Guards: a mid-rebase whose `onto` is not an ancestor-or-equal of its tree-parent is refused as "not started by git-tree"; still-unresolved conflicts refuse with `kind=unresolved_conflicts`. `git tree rebase <target> [branch]` reparents the branch (named, or current) onto `target` and rebases it there, then propagates (it is *not* just `attach` + `propagate`: `attach` records the edge without rewriting, so it omits the rebase of the branch itself). Because the reparent is committed before the rebase runs, a `rebase` conflict (on the rebased branch *or* a descendant) is resumed like any propagate conflict: `git tree propagate <branch>`. Conflict resolutions auto-replay across the cascade via rerere; `--no-auto-rerere` (on propagate/rebase) disables it.
- `--version` prints `git-tree <version>` (`_version`, the package version).
- `--no-input` (`_no_input`/`_require_input`, threaded via `args`): errors instead of prompting.
- Exit codes via `TreeError(msg, code=…)`: 3 resumable conflict, 4 precondition/state, 5 not-a-tree-branch (1 generic, 2 argparse usage).
- `--dry-run` on propagate/rebase/push/remove.
- `git tree skills [--install] [--dir DIR]` (`cmd_skills`): lists the bundled skills and their per-harness destinations, or installs them (see Install). Dispatches normally (no `main()` special case). The listing is a query and returns `{skills, destinations: [{skill, path, state}]}` with `state` one of `installed`/`occupied by another skill`/`not installed`; `--install` is a mutation and stays bare, so re-query the listing for post-install state.
- `git tree manpage [--install]` (`_render_manpage`/`cmd_manpage`): roff man page generated from the argparse parser (single source of truth with `-h`/`--help`); `--install` writes it to the man path so `git tree --help` works. Handled inline in `main()` since it needs the parser.

## Testing

Real git operations against isolated repos (no mocking). The `repo` fixture (`tests/conftest.py`) creates a bare origin + clone in `tmp_path` and `chdir`s into it. `RepoHelper` provides `commit()`, `branch()`, `checkout()`, `set_parent()`, `worktree()`, `push()`.

To call a `cmd_*` handler directly, build its args with `cli_args(**overrides)` (`tests/conftest.py`), which returns a **complete** namespace (every flag at its parser default, derived by walking `_build_parser()`). Handlers read `args.<flag>` directly (no `getattr` defaults), so a missing field raises `AttributeError`: a partial hand-built `argparse.Namespace(...)` is a fixture bug, not something production code should tolerate. Always go through `cli_args`.

## Conventions

- Python 3.11+, stdlib only (no runtime deps)
- ruff for lint+format (line-length 100, select E/F/I/UP/B/SIM/TCH, TCH ignored in tests)
- ty for type checking
- Tests assert behavior, not implementation details
- Commits use conventional format with `(git_tree)` scope: `feat(git_tree): ...`, `fix(git_tree): ...`, `test(git_tree): ...`
