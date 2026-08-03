# Refactor plan: split `git_tree/cli.py` into modules

Status: **not started** (see "Current state" at the end). This document is the agreed plan,
reviewed by four independent reviewer passes and revised twice. Read it in full before starting.

## Goal

`git_tree/cli.py` is a single ~3500-line module holding every layer of the tool: error classes,
git subprocess wrappers, the branch/graph data model, discovery, tree rendering, prompts, safety
guards, the cascade engine, all 14 `cmd_*` handlers, shell-completion and man-page generation,
the argparse parser, and the JSON envelope. It has outgrown one file, and the layering is
invisible: `discover()` calls `_active_rebase_branch()` a thousand lines below it, and the
functions bound by `_require_ready`'s ordering invariant sit hundreds of lines apart with
unrelated code wedged between them.

This is a **migration only**. Move code, adjust imports, nothing else. No renames, no signature
changes, no docstring rewrites, no dead-code removal, no behavior changes. The outcome is 21
modules with an acyclic import graph and a `cli.py` holding only the CLI surface.

One deliberate exception to "nothing else": `cli.py`'s 13 decorative section banners
(`# ---` / `# Title` / `# ---`) do **not** travel with their code. A new module's name says what
its banner said, and decorative headers are banned by the project's style rules, so new modules
get none and a banner is deleted from `cli.py` once the last definition under it leaves. Every
definition itself still moves byte-identically; the exception covers only the text between
definitions.

The structure stays **flat** (`git_tree/_git.py`, not `git_tree/core/git.py`). The `_cmd_` prefix
already groups the command modules, a module's level is legible from its import block, and
directories would not enforce the layering anyway. One concrete hazard also rules subpackages
out: `_bundled_skills()` resolves `Path(__file__).parent / "skills"`, so moving `_cmd_skills.py`
into a subdirectory silently breaks the bundled-skill lookup.

## Target layout

| Module | ~Lines | Contents |
|---|---|---|
| `cli.py` | 350 | `_version`, `_EPILOG`, `_KIND_BY_CODE`, `_envelope`, `_error_envelope`, `_render_error`, `cmd_completions`, `cmd_manpage`, `_build_parser`, `main` |
| `_errors.py` | 75 | `ErrorKind`, `TreeError`, `ConflictError` |
| `_git.py` | 500 | `Color`/`_use_color`/`_color`; `_run`, `git`, `git_lines`, `git_ok`, `git_echo`, `git_echo_ok`, `_git_dir`; `_is_conflict`, `WorktreeStatus`, `_worktree_status`; config accessors (`_set_fork_commit`, `_get_tree_parent`, `_unset_tree_config`, `_would_cycle`, `_register_child`, `_branch_remote`, `_set_branch_remote`, `_carry_remote_to_root`, `_all_branch_config`); ref readers (`current_branch`, `all_branch_names`, `_is_tree_branch`); worktree/submodule filesystem (`_force_remove_worktree`, `_submodule_paths`, `_check_submodule_health`, `_init_submodules`, `_init_submodules_or_warn`); rebase-state readers (`_has_active_rebase`, `_rebase_state_file`, `_active_rebase_onto`, `_active_rebase_branch`, `_has_active_rebase_safe`, `SequencerOp`, `_SEQUENCER_STATES`, `_pending_sequencer_op`, `_is_interactive_rebase`, `ForeignRebase`, `_foreign_rebase_reason`, `_foreign_rebase_phrase`, `_is_git_tree_rebase`, `_has_unmerged`, `_stash_push_if_created`) |
| `_graph.py` | 250 | `BranchSnapshot`, `BranchInfo`, `Graph`, `_get_fork_commit`, `roots`, `root_of`, `_root_remote`, `_find_cycles`, `discover` |
| `_prompt.py` | 85 | `_prompt`, `confirm`, `_no_input`, `_require_input`, `_proceed`, `fzf_select`, `_fallback_select`, `_select_one` |
| `_display.py` | 250 | `BOX_*`, `_ahead_behind`, `_git_status_summary`, `_pending_commit_count`, `format_tree`, `_format_subtree`, `_subtree_lines`, `_hydrate`, `_tree_json` |
| `_guards.py` | 190 | `_require_worktrees`, `_require_healthy_submodules`, `_require_clean_state`, `_require_ready`, `_refuse_unfinished_replay`, `_remove_blocking_dirt`, `_mid_rebase_branches` |
| `_engine.py` | 340 | `_replay_is_empty`, `_skip_empty_commits`, `_rerere_args`, `_auto_rerere`, `_rebase_onto`, `_drive_conflicted_rebase`, `_conflict_exit`, `RebaseResult`, `_rebase_branch`, `_advance_branch`, `_resume_cmd`, `_propagate_descendants` |
| `_render.py` | 230 | `_ZSH_TEMPLATE`, `_BASH_TEMPLATE`, `_UNIVERSAL_OPTS`, `_ZSH_COMPLETER`, `_completable_actions`, `_arg_label`, `_zsh_escape`, `_zsh_value`, `_zsh_spec`, `_render_zsh`, `_bash_value_lines`, `_render_bash`, `_render_completions`, `_set_completer`, `_render_manpage` |
| `_cmd_skills.py` | 135 | `_SKILL_INSTALL_DIRS`, `_bundled_skills`, `_is_git_tree_skill`, `_place_skill`, `cmd_skills`, `_install_or_list_skills` |
| `_cmd_tree.py` | 80 | `cmd_tree` |
| `_cmd_branch.py` | 90 | `cmd_branch` |
| `_cmd_attach.py` | 70 | `cmd_attach` |
| `_cmd_detach.py` | 80 | `cmd_detach` |
| `_cmd_remove.py` | 255 | `cmd_remove` |
| `_cmd_rebuild.py` | 130 | `_prunable_worktree_path`, `cmd_rebuild` |
| `_cmd_propagate.py` | 90 | `cmd_propagate` |
| `_cmd_rebase.py` | 150 | `cmd_rebase` |
| `_cmd_split.py` | 190 | `_resolve_split_point`, `_worktree_choice`, `_add_split_worktree`, `_split_child`, `cmd_split` |
| `_cmd_push.py` | 110 | `cmd_push` |
| `_cmd_log.py` | 50 | `cmd_log` |

`git_tree/skills/` (the data directory) does not move, and `git_tree/__init__.py` stays empty.

**Verify this table before writing code.** At the last check it covered all 144 top-level names
in `cli.py` exactly once, but the file is under active development and the baseline has already
moved twice during planning (`ErrorKind`, `SequencerOp`, `_foreign_rebase_reason`, then
`ForeignRebase`, `_foreign_rebase_phrase`, `_mid_rebase_branches` all appeared mid-plan). Take
the symbol set fresh and diff it against this table; place anything new by its real call sites,
not by name.

## Import DAG

```
L0  _errors (sys)                    _render (argparse, os)
L1  _git    -> _errors               _prompt -> _errors
L2  _graph  -> _git
L3  _display -> _git, _graph         _guards -> _errors, _git, _graph
    _engine  -> _errors, _git, _graph, _guards
    _cmd_skills -> _errors
L4  _cmd_tree / _cmd_branch / _cmd_attach / _cmd_detach / _cmd_remove / _cmd_rebuild
    _cmd_propagate / _cmd_rebase / _cmd_split / _cmd_push / _cmd_log
        -> _errors, _git, _graph, _display, _guards, _engine, _prompt  (never each other)
L5  cli -> _errors, _render, _cmd_skills, _cmd_*
```

Acyclic, but *not* because the levels strictly increase: `_engine` (L3) imports `_guards` (L3),
since `_skip_empty_commits` calls `_refuse_unfinished_replay`. Acyclicity rests on the declared
edge set. No module needs a function-local or deferred import. Five placement rules keep it that
way; violating any one reintroduces a cycle.

0. **No command module may import another command module.** With one module per command this is
   what keeps L4 flat, and two helpers threaten it. `_remove_blocking_dirt` is called by both
   `cmd_remove` and `cmd_rebuild`, so leaving it with either forces `_cmd_rebuild -> _cmd_remove`;
   it goes to `_guards.py` as the conservative "cannot prove deleting this is safe" predicate.
   `_auto_rerere` is called by both `cmd_propagate` and `cmd_rebase`, and goes to `_engine.py`
   beside `_rerere_args`, the other half of the same rerere knob. `_prunable_worktree_path` is
   genuinely `cmd_rebuild`-only and stays there.
1. **`_build_parser` and every `cmd_*` it names via `set_defaults(func=…)` must not import each
   other's module.** `cmd_completions` calls `_build_parser()`, so it and `cmd_manpage` stay in
   `cli.py` (the parser sets no `func` for either, so `cli` importing the `_cmd_*` modules is
   one-way). `cmd_skills` lives in `_cmd_skills.py`; `cli` imports it for the parser.
2. **Color stays in `_git.py`.** `git_echo` calls `_color(..., Color.DIM)`, so moving it to
   `_display.py` would make `_git -> _display -> _git`.
3. **`WorktreeStatus` + `_worktree_status` stay in `_git.py`, apart from the other dataclasses.**
   `BranchInfo.is_dirty` calls `_worktree_status` at *runtime*, so grouping all four dataclasses
   into one model module creates a real runtime cycle that only a function-local import breaks.
4. **`_tree_json` goes to `_display.py`, not `_graph.py`.** It calls `_pending_commit_count`,
   `_ahead_behind` and `_has_active_rebase`; in `_graph.py` that would be
   `_graph -> _display -> _graph`.

`_get_fork_commit` moves to `_graph.py` (its `info: BranchInfo | None` parameter would otherwise
force a type-only back-edge from `_git`), while `_set_fork_commit` stays in `_git.py` because
`_register_child` calls it. The asymmetry is deliberate and buys a clean DAG. `ErrorKind` belongs
in `_errors.py` because it is referenced from `_prompt`, `_guards`, `_engine`, `_cmd_push` and
`cli`. `_foreign_rebase_reason` must be in `_git.py`, not `_guards.py`, because `_is_git_tree_rebase`
(a `_git` reader) calls it.

## `from __future__ import annotations` and `if TYPE_CHECKING:`

**Every new module must open with `from __future__ import annotations`.** This is a prerequisite,
not a style choice: without it, a name imported only under `if TYPE_CHECKING:` is evaluated at
function-definition time and raises `NameError` on import. It is also what makes ruff's `TCH`
rules propose deferring an import in the first place.

Two facts about `TCH`, both verified empirically:

- `TC001`/`TC003` fire only when the **entire** `from X import …` statement is annotation-only.
  A statement that also carries a runtime name is never flagged. Since isort merges each module's
  imports from a given source into one statement, `_display` and `_engine` pull runtime names
  (`BranchSnapshot`, `roots`, `_root_remote`, `_get_fork_commit`) from `_graph` alongside
  `Graph`/`BranchInfo`, so neither needs a block for those.
- These rules are **not auto-fixable**: `ruff check --fix` reports "No fixes available" and leaves
  them. Each block is written by hand, so the per-step `ruff check` gate fails until it is added.

Expected needs: `_errors` (`Path`), `_guards` (`Graph`, `Path` — **not** `argparse`; no guard takes
a Namespace), `_prompt` (`argparse`), `_cmd_skills` (`argparse`), `_engine` (`Path`), and every
`_cmd_*` (`argparse`). No block needed in `_render` (runtime `isinstance(a, argparse._SubParsersAction)`),
`_graph` (runtime `Path(...)` in `discover`), `_git`, `_display`, or `cli`. `typing.NoReturn` is
never flagged — ruff exempts `typing` by default. Let the per-step `ruff check` settle the exact
set; no ruff config change either way.

Keeping `Graph` and `BranchInfo` in the same module (rule 3) is what keeps every dataclass *field*
annotation pointing at a runtime-available name.

## Migration order

One commit per step, `refactor(git_tree): extract <module> from cli.py`. Run the full gate after
each: `uv run ruff format --check . && uv run ruff check . && uv run ty check git_tree/ && uv run
pytest tests/ -q`. Run `ruff check . --fix` *within* each step, not at the end, so `cli.py`'s
import header (`configparser`, `shutil`, `StrEnum`, `ThreadPoolExecutor`, `dataclass`/`field`) is
pruned as its last user leaves.

`_errors.py` must precede everything except `_render.py`, since every other module raises
`TreeError`; extract `_cmd_skills.py` or `_guards.py` first and you get a cycle against `cli`.

**Step 0 is test-only** (`test(git_tree): route prompt stubs through a stable seam`). No production
changes, so the suite must pass unchanged before and after. The problem it solves: 36 monkeypatch
sites name a *module path* (`"git_tree.cli.fzf_select"`), coupling the tests to where a function
currently lives, so a pure code move breaks them.

- Repoint the 6 `"git_tree.cli.confirm"` patches at `"builtins.input"`. `confirm` calls `input()`,
  so this keeps the "assert loudly if consulted" semantics while being independent of which module
  `confirm` lives in. Verified equivalent: all 6 install a `_no_confirm` raising `AssertionError`,
  none relies on `confirm` returning `True`, none also patches `builtins.input` (no collision),
  `AssertionError` is not swallowed by `_prompt`'s `except (EOFError, KeyboardInterrupt)`, and none
  of those 6 tests reaches `input()` for a non-confirm reason. The existing
  `(_message: str) -> bool` fakes are already `input`-compatible.
- Route the 30 `"git_tree.cli.fzf_select"` stubs through conftest fixtures so the module path is
  written once: a `no_fzf` that fails if consulted (21 uniform sites, all in `test_split.py`) and a
  `pick_fzf(chosen)` factory returning a test-specific value (8 sites in `test_split`, `test_fork`,
  `test_remove`). The ninth, `fake_fzf` in `test_remove`, inspects its `items` argument and keeps
  its own stub. Do **not** reroute any of these through `builtins.input`: that would exercise
  `_fallback_select`'s numbered-list protocol instead of bypassing the picker, changing which code
  path is under test.

Step 0 leaves the module path named in conftest (and the one bespoke stub); step 3 retargets it to
`git_tree._prompt.fzf_select`. That keeps working afterward because `_select_one` stays in
`_prompt.py` and resolves `fzf_select` from `_prompt`'s globals at call time.

| # | Module | Test references to retarget |
|---|---|---|
| 1 | `_render.py` | test_agentic (`_render_completions`) |
| 2 | `_errors.py` | `TreeError` across ~10 test files |
| 3 | `_prompt.py` | the conftest fixtures' patch target plus the one bespoke stub |
| 4 | `_git.py` | test_submodules (`_check_submodule_health`, `_force_remove_worktree`, `_submodule_paths`, **and the `import git_tree.cli as cli_mod` / `setattr(cli_mod, "git_echo_ok", …)` pair**), test_engine (`_stash_push_if_created`), test_display (`_worktree_status`), test_json/test_propagate/test_rebase/test_rebuild/test_remove (`_has_active_rebase`), test_refs (`current_branch`), **conftest's second function-local import inside `RepoHelper.stop_rebase_clean` (`_active_rebase_onto`, `_has_active_rebase`)** |
| 5 | `_graph.py` | `discover`, `roots`, `root_of`, `BranchInfo`, `_get_fork_commit`, `_root_remote` across ~14 files |
| 6 | `_display.py` | test_display (`format_tree`, `_git_status_summary`) |
| 7 | `_guards.py` | none |
| 8 | `_engine.py` | none |
| 9 | `_cmd_skills.py` | test_skills (`_bundled_skills`) |
| 10-20 | `_cmd_tree`, `_cmd_branch`, `_cmd_attach`, `_cmd_detach`, `_cmd_remove`, `_cmd_rebuild`, `_cmd_propagate`, `_cmd_rebase`, `_cmd_split`, `_cmd_push`, `_cmd_log` | `cli.py` keeps a binding for every dispatched `cmd_*` (the parser passes them to `set_defaults`), so direct test imports of those need not move |
| 21 | docs | AGENTS.md, three statements: rewrite the "Single module: `git_tree/cli.py`" Architecture paragraph; update "Adding a subcommand" (a command now touches three sites — handler in a `_cmd_*` module, parser block in `cli.py`, dispatch unchanged); and fix "Submodule awareness (helpers near `_require_clean_state`)", which becomes false |
| 22 | `tests/test_repo_structure.py` | none (a new test; see "Import layering test" below) |

Order constraints, verified by AST against each handler's real call set: run 8 after 7
(`_engine` needs `_guards`), and steps 10-20 after 6, 7 and 8 (`_cmd_propagate`/`_cmd_rebase` need
`_engine`, `_guards`, `_display`; `_cmd_push` needs `_guards`; `_cmd_remove`/`_cmd_rebuild` need
`_guards`; `_cmd_tree`/`_cmd_remove` need `_display`). Steps 6, 7 and 9 may be reordered among
themselves, and so may 10-20. Inverting a cross-level constraint produces a cycle that exists only
mid-migration: the extracted module would import from `cli.py` while `cli.py` imports from it.

**The retarget column is a reminder, not the authority.** Derive the list mechanically at every
step. Use the loose pattern — conftest has two *function-local* `from git_tree.cli import`
statements, and `test_submodules.py` reaches the module a third way (`import git_tree.cli as
cli_mod`), which a `git_tree\.cli\.`-with-a-trailing-dot pattern silently misses:

```sh
grep -rn "git_tree\.cli" tests/
```

There is no accidental safety net: a re-export left in `cli.py` is unused there, so the step's own
`ruff check . --fix` deletes it as F401. Retarget a reference at the step its name moves, even when
`from git_tree.cli import X` would still resolve. **The same rule governs attribute-patch targets**:
`monkeypatch.setattr(cli_mod, "git_echo_ok", …)` must follow the function whose globals are
actually consulted, not the module the test happens to import.

## Risks

1. **`test_submodules.py`'s `cli_mod.git_echo_ok` patch (step 4) fails misleadingly if missed.**
   `_force_remove_worktree` would resolve the real `git_echo_ok` from `git_tree._git.__dict__`,
   stage 1 of the removal would genuinely succeed, and `pytest.raises(TreeError)` would not fire —
   a plumbing bug wearing a logic bug's clothes.
2. **`description=__doc__` in `_build_parser`** resolves to `cli.py`'s module docstring. This is why
   `_build_parser` stays in `cli.py`: move it and the description silently disappears from
   `git tree -h`, `--help`, and the man page body, and no test catches it (test_agentic's assertion
   is satisfied by `_render_manpage`'s hard-coded `.SH NAME`). That line is user-visible output, so
   step 21 must leave `cli.py`'s module docstring byte-identical.
3. **`_set_completer`'s `action.__dict__["completer"]` duck-punch.** The writer, both readers
   (`_zsh_value`, `_bash_value_lines`) and the tag vocabulary (`_ZSH_COMPLETER`, the literal
   `"git_heads"`/`"directories"` comparisons) all go to `_render.py` together. Split them and a
   mistyped tag becomes a `KeyError` at completion-generation time instead of parser-build time.
4. **`main()`'s `contextlib.redirect_stdout(sys.stderr)` works by rebinding `sys.stdout`.** Safe
   across modules because every `print()` resolves `sys.stdout` at call time — provided no new
   module writes `from sys import stdout`. It must not.
5. **`git_tree/skills/` must not move, and `_cmd_skills` must be a module, not a package.**
   `_bundled_skills()` resolves `Path(__file__).parent / "skills"`, and `_is_git_tree_skill` matches
   an installed symlink's stored target by the shape `("git_tree", "skills", <name>)` — a shape
   already baked into links in users' `~/.claude/skills/` and `~/.agents/skills/`. Relocating the
   data, or making `git_tree/_cmd_skills/__init__.py`, changes `__file__.parent` and git-tree
   disowns its own installed links. Packaging needs no change: hatchling infers the `git_tree`
   package and ships every non-ignored file under it.
6. **Cross-module `_`-prefixed imports become normal** (e.g. `_guards` importing `_run` from
   `_git`). Keep names byte-identical; do not promote anything to a public name, and do not add
   `__all__` — both would be code changes.
7. **`ty` watch items**: the private argparse API (`parser._actions`, `argparse._SubParsersAction`,
   `sub._choices_actions`) moving into `_render.py`, and `_completable_actions`'s missing return
   annotation now being inferred across a module boundary. Adding the annotation is out of scope;
   just confirm `ty` stays quiet.
8. **Concurrent edits to `cli.py` are the dominant execution risk.** A change landing in `cli.py`
   after its function has moved out becomes silently dead code with no error, and any parser change
   invalidates the goldens mid-run so every later diff is a false alarm. Require quiescence on
   `git_tree/cli.py` and `tests/` for the duration; at minimum assert `git status --porcelain` is
   empty before each step's commit. This already bit twice during planning — six symbols appeared
   in `cli.py` mid-plan and only the AST name-set diff caught them.
9. **The strict xfails are both a safety net and the project's todo list.** Known gaps are
   documented as `@pytest.mark.xfail(strict=True, reason=…)` tests, so each marker is a record of
   outstanding work. `strict=True` means an accidental behavior change that alters one of those
   paths turns the suite red via XPASS instead of passing silently. **Never remove or relax a marker
   here** — that deletes documentation, not just a test.

## Verification

Capture goldens before step 1 and diff after every step:

```sh
git tree manpage > gold.man
git tree completions zsh > gold.zsh
git tree completions bash > gold.bash
COLUMNS=80 git tree -h > gold.help        # -h wraps to COLUMNS; pin it or the diff lies
```

`manpage` pins `COLUMNS=80` internally and `.TH GIT-TREE 1` carries no date, so it and both
completion scripts are byte-stable run to run. After each step, re-run those three plus
`COLUMNS=80 git tree -h` and `python -m git_tree --version` (which exercises `__main__.py`'s
import path).

**Nothing else in the gate catches a copy instead of a move.** Every check passes if a definition
is left behind in `cli.py` *and* added to the new module: `cli.py` re-imports the same name and one
definition silently shadows the other, leaving two copies to drift. Snapshot the top-level name set
before step 1 and assert after every step that the union across `git_tree/*.py` still equals it,
with no name owned twice:

Save this as `check_partition.py` outside the repo and run it **from the repo root** (its paths
are repo-relative): `python check_partition.py save` once before step 1, then
`python check_partition.py` after every step. Verified against commit `594e3cf`: 144 names, no
duplicates, exit 0.

```python
import ast, collections, json, pathlib, sys

BASELINE = pathlib.Path("/tmp/git_tree_baseline.json")

def owners(paths):
    found = collections.defaultdict(list)
    for f in paths:
        for node in ast.parse(f.read_text()).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                found[node.name].append(f.stem)
            elif isinstance(node, ast.Assign):
                found.update({t.id: found[t.id] + [f.stem]
                              for t in node.targets if isinstance(t, ast.Name)})
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                found[node.target.id].append(f.stem)
    return found

pkg = pathlib.Path("git_tree")
if len(sys.argv) > 1 and sys.argv[1] == "save":
    names = sorted(owners([pkg / "cli.py"]))
    BASELINE.write_text(json.dumps(names))
    sys.exit(f"baseline: {len(names)} names")

found = owners(p for p in sorted(pkg.glob("*.py")) if p.name != "__init__.py")
base = set(json.loads(BASELINE.read_text()))
dupes = {k: v for k, v in found.items() if len(v) > 1}
print("defined twice:", dupes or "none")
print("lost:", sorted(base - set(found)) or "none")
print("new:", sorted(set(found) - base) or "none")
sys.exit(1 if (dupes or base ^ set(found)) else 0)
```

A "new" name mid-migration means someone added a symbol to `cli.py` while the split was in
flight (see risk 8) — place it by its call sites and add it to the layout table before continuing.

**Nor does the partition check catch a definition rewritten rather than moved.** It compares name
sets, so a tidied docstring, a reflowed comment, or a "harmless" simplification inside a moved
function passes every gate above: ruff, ty, the test suite, and the goldens are all satisfied by
behavior-preserving edits. A second script closes that gap: for every top-level definition, extract
its source segment (decorator line through `end_lineno`) from `cli.py` at the pre-refactor commit
via `git show`, and assert byte-equality against the same definition wherever it now lives.
Definition-scoped, so the dropped section banners above do not trip it. Extraction needs
`min(d.lineno for d in node.decorator_list)` as the start line, not `node.lineno`, or the five
decorated definitions (`WorktreeStatus`, `BranchSnapshot`, `BranchInfo`, `Graph`, `RebaseResult`)
lose their decorator.

A third script enforces the rank rule from "Import layering test" while the split is in flight,
where the committed test cannot yet run. Both scripts live outside the repo alongside
`check_partition.py`.

At the end, on a scratch stacked repo: `git tree --json` emits exactly one JSON object on stdout
with diagnostics on stderr; `git tree skills` lists the same three skills with the same destination
paths and states; a deliberate conflict during `git tree propagate` still exits 3 with a `remedy`
of `["git","tree","propagate",<branch>]`, and re-running that argv finishes the cascade.

## Current state

**Step 0 done** (the test-only seam). Next: step 1, `_render.py`. This section is updated with
every step's commit, so a migration interrupted between sessions can be resumed from it.

Baseline re-verified at commit `d727b82`, unchanged since `594e3cf`: 144 top-level names in
`cli.py` (3493 lines); full gate green at 292 passed, 5 xfailed; all four goldens and the three
partition/byte-identity/import scripts green.

## Import layering test

An earlier revision of this plan declined an import-layering test to keep the migration pure. That
was reversed: a documented convention that nothing checks is one careless import away from being
false, and the DAG is the whole point of the split. It lands as step 22, after the migration, as
`tests/test_repo_structure.py`.

It cannot land earlier. Mid-migration, every not-yet-extracted module still imports from `cli.py`,
so the test would fail against work in progress.

The invariant is expressed as **ranks**, not an explicit edge list: a module may import only from
strictly lower ranks.

| Rank | Modules |
|---|---|
| 0 | `_errors`, `_render` |
| 1 | `_git`, `_prompt` |
| 2 | `_graph` |
| 3 | `_display`, `_guards`, `_engine`, `_cmd_skills` |
| 4 | the eleven `_cmd_*` command modules |
| 5 | `cli` |

One declared same-rank exception: `_engine` may import `_guards`, because `_skip_empty_commits`
calls `_refuse_unfinished_replay`. Two further assertions are invariants rather than rankings:
nothing may import `cli`, and no `_cmd_*` may import another `_cmd_*` (which the same-rank rule
already gives, but which is worth asserting by name since it is placement rule 0).

Ranks beat an edge list here because they stay correct as the real import sets settle (whether
`_graph` ends up needing `_errors`, say) while still forbidding every back-edge. The test walks
the real module set rather than a hard-coded file list, so a module added later is covered by
default and a module at no declared rank fails loudly.
