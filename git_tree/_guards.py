"""Pre-flight safety gates run before any cascade rewrites a branch."""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

from git_tree._errors import ErrorKind, TreeError
from git_tree._git import (
    _check_submodule_health,
    _config_bool,
    _gitlink_paths,
    _has_active_rebase,
    _has_active_rebase_safe,
    _has_unmerged,
    _is_git_tree_rebase,
    _pending_sequencer_op,
    _run,
    _submodule_paths,
    _worktree_status,
    git_lines,
)

if TYPE_CHECKING:
    from pathlib import Path

    from git_tree._graph import Graph


def _remove_blocking_dirt(wt: Path) -> bool:
    """True if worktree `wt` — or any submodule at any depth — has uncommitted work that a
    force-removal (`shutil.rmtree`) would irreversibly delete.

    Force-removal bypasses git's own dirty/submodule refusals, so this is the sole backstop
    and is deliberately conservative: `--ignore-submodules=none` overrides any submodule-ignore
    config, and `--untracked-files=normal` overrides `status.showUntrackedFiles`, which is a
    common large-repo perf setting that would otherwise hide the files this protects;
    `foreach --recursive` reaches every depth; a populated-but-uninitialized submodule
    (which `foreach` skips) and an inner `git status` that errors both count as "cannot prove
    clean". `--quiet` drops foreach's translated "Entering '<path>'" banner so any remaining
    stdout is real dirt.
    """
    if _worktree_status(wt, ignore_submodules="none", untracked_files="normal").dirty:
        return True
    try:
        subs = _submodule_paths(wt)
    except TreeError:
        return True  # unreadable .gitmodules: cannot prove there is no submodule work to lose
    for sub in subs:
        d = wt / sub
        try:
            if not (d / ".git").exists() and d.is_dir() and any(d.iterdir()):
                return True
        except OSError:
            return True  # can't inspect it, so can't prove it clean
    proc = _run(
        "git",
        "submodule",
        "--quiet",
        "foreach",
        "--recursive",
        "git status --porcelain --ignore-submodules=none --untracked-files=normal",
        cwd=wt,
        check=False,
    )
    return proc.returncode != 0 or bool(proc.stdout.strip())


def _refuse_unfinished_replay(
    branch: str, cwd: Path, stash: str | None, resume_cmd: list[str]
) -> NoReturn:
    """The rebase stopped with no conflict but with changes present, so it cannot be skipped."""
    # Unstaged only (worktree vs index), not `diff HEAD`: those are the files that block
    # `git rebase --continue`, and they are the only ones the advised stash may take. A pathspec
    # built from `diff HEAD` would also list the staged conflict resolution, so running the advice
    # verbatim would stash it, leave index == HEAD, and empty the replay.
    files = git_lines("diff", "--name-only", cwd=cwd)
    paths = " ".join(files) or "<file>..."
    lines = [
        f"{branch}'s rebase in {cwd} is stopped with unstaged changes that are not a conflict:",
        *(f"  {f}" for f in files),
        "`git rebase --continue` refuses while they are unstaged and `git rebase --skip` would",
        "discard them. Move just these aside, leaving any staged conflict resolution in the index:",
        f"    git -C {cwd} stash push -- {paths}",
        f"Then re-run: {' '.join(resume_cmd)}",
    ]
    if stash:
        lines.append(f"An earlier stash from this run is also waiting: git stash apply {stash}")
    raise TreeError(
        "\n".join(lines),
        code=4,
        branches=[branch],
        remedy=list(resume_cmd),
    )


def _require_worktrees(branches: list[str], graph: Graph) -> None:
    missing = [b for b in branches if not graph.worktree_of.get(b)]
    if not missing:
        return
    lines = ["These branches need worktrees before this operation can proceed:"]
    for b in missing:
        lines.append(f"  {b}")
    lines.append("\nAdd worktrees with: git worktree add <path> <branch>")
    raise TreeError("\n".join(lines), code=4, branches=missing)


def _mid_rebase_branches(branches: list[str], graph: Graph) -> list[tuple[str, Path]]:
    """The branches whose worktree has a rebase in progress, with that worktree."""
    # A worktree that cannot be inspected answers no: the worktree and cleanliness gates report
    # that far better than a guard built on top of them.
    found: list[tuple[str, Path]] = []
    for b in branches:
        info = graph.branches.get(b)
        if info and info.worktree and _has_active_rebase_safe(info.worktree):
            found.append((b, info.worktree))
    return found


def _require_clean_state(branches: list[str], graph: Graph, resume_cmd: list[str]) -> None:
    # An in-scope branch that is mid-rebase is a *resume point*, not a failure — PROVIDED the
    # rebase is git-tree's own cascade (its `onto` is an ancestor-or-equal of the branch's
    # tree-parent) and its conflicts are resolved; `_advance_branch` will finish it. So:
    #   - clean git-tree mid-rebase        -> allow
    #   - git-tree mid-rebase, unmerged    -> refuse: resolve, then re-run resume_cmd
    #   - foreign mid-rebase (bad onto)    -> refuse: not ours to drive
    #   - conflicted, not mid-rebase       -> refuse (as before)
    # Plain dirty (no conflict, no rebase) still passes: `_rebase_branch` stashes it.
    unresolved: list[tuple[str, Path]] = []
    foreign: list[tuple[str, Path]] = []
    pending: list[tuple[str, Path, str]] = []
    for b in branches:
        info = graph.branches.get(b)
        if not info or not info.worktree:
            continue
        wt = info.worktree
        if _has_active_rebase(wt):
            if not _is_git_tree_rebase(wt, graph.parent_of.get(b)):
                foreign.append((b, wt))
            elif _has_unmerged(wt):
                unresolved.append((b, wt))
            # else: a clean, git-tree-owned mid-rebase — allow (it will be finished).
        elif (op := _pending_sequencer_op(wt)) is not None:
            pending.append((b, wt, op))
        elif _worktree_status(wt).conflicted:
            unresolved.append((b, wt))
    if pending:
        raise TreeError(
            "These branches have an operation in progress that rebasing would discard:\n"
            + "\n".join(f"  {b}  (a {op} is in progress in: {wt})" for b, wt, op in pending)
            + "\n\nFinish or abort it there, then re-run.",
            code=4,
            branches=[b for b, _, _ in pending],
        )
    if unresolved and not foreign:
        lines = [
            "Resolve the conflicts and `git add` them, then re-run:",
            f"    {' '.join(resume_cmd)}",
        ]
        lines += [f"  {b}  (in: {wt})" for b, wt in unresolved]
        raise TreeError(
            "\n".join(lines),
            code=4,
            kind=ErrorKind.UNRESOLVED_CONFLICTS,
            branches=[b for b, _ in unresolved],
        )
    if foreign or unresolved:
        lines = ["These branches are not in a clean state:"]
        lines += [
            f"  {b}  (a rebase not started by git-tree is in progress — resolve or "
            f"`git rebase --abort` in: {wt})"
            for b, wt in foreign
        ]
        lines += [f"  {b}  (unresolved conflicts — resolve in: {wt})" for b, wt in unresolved]
        raise TreeError("\n".join(lines), code=4, branches=[b for b, _ in foreign + unresolved])


def _require_ready(branches: list[str], graph: Graph, resume_cmd: list[str]) -> None:
    """Preflight gate for a cascade: worktrees present, submodules healthy, worktrees clean
    (an in-scope git-tree mid-rebase is allowed — it's a resume point, see `_require_clean_state`).

    Order matters: worktree and submodule-health checks run before `_require_clean_state`,
    because `git status` crashes on a corrupted submodule. Each underlying check is a no-op
    on an empty list.
    """
    _require_worktrees(branches, graph)
    _require_healthy_submodules(branches, graph)
    _require_clean_state(branches, graph, resume_cmd)


def _require_healthy_submodules(branches: list[str], graph: Graph) -> None:
    """Pre-flight: verify each worktree's submodules have valid .git state."""
    unhealthy: list[tuple[str, str]] = []
    for b in branches:
        info = graph.branches.get(b)
        if not info or not info.worktree:
            continue
        try:
            sub_paths = _submodule_paths(info.worktree)
        except TreeError:
            # Unlike the removal gate, nothing here is about to delete anything, so an
            # unreadable .gitmodules is not worth blocking a cascade over. The rebase itself
            # will surface any real submodule problem.
            continue
        for sub_path in sub_paths:
            if not _check_submodule_health(info.worktree, sub_path):
                unhealthy.append((b, sub_path))
    if not unhealthy:
        return
    lines = ["These branches have corrupted submodule state:"]
    for b, sub_path in unhealthy:
        lines.append(f"  {b}  (submodule: {sub_path})")
    lines.append("\nFix with: git tree rebuild <branch>")
    raise TreeError("\n".join(lines), code=4)


def _require_initialized_submodules(worktree: Path, commit: str, branch: str) -> None:
    """Pre-flight for a `git reset --hard <commit>` in `worktree`: refuse when a submodule the
    target records is not usable and `submodule.recurse` is set.

    `submodule.recurse` makes `reset` recurse, and recursing into a submodule whose gitdir it
    cannot open aborts with `could not reset submodule index` *after* the superproject index and
    worktree are partly rewritten. `git worktree add` populates no submodules, so any worktree not
    created by `git tree` is a candidate. `_require_healthy_submodules` cannot stand in for this:
    it reads the worktree and calls a missing `.git` benign, which is right for a cascade but is
    exactly the case that breaks a reset.

    Both unusable states block, and they need different repairs: no `.git` at all is uninitialized
    and an init fixes it, while a `.git` whose gitdir does not resolve is corrupted and an init can
    fail on it outright, so that one goes to `rebuild`.

    Gated on the config because with recursion off the reset succeeds, merely leaving submodules
    stale, which is a supported choice and not ours to block.
    """
    if not _config_bool("submodule.recurse", cwd=worktree):
        return
    uninitialized: list[str] = []
    corrupted: list[str] = []
    for p in _gitlink_paths(commit, cwd=worktree):
        if not (worktree / p / ".git").exists():
            uninitialized.append(p)
        elif not _check_submodule_health(worktree, p):
            corrupted.append(p)
    if not uninitialized and not corrupted:
        return
    lines = [
        f"{worktree} cannot be rewound: submodule.recurse is set, so `git reset --hard` will "
        f"recurse into submodules that {commit[:12]} records, and these are not usable:"
    ]
    lines += [f"  {p}  (uninitialized)" for p in uninitialized]
    lines += [f"  {p}  (corrupted .git pointer)" for p in corrupted]
    lines.append("\nIt would fail partway and leave the worktree half-rewound. Fix with:")
    if uninitialized:
        lines.append(f"  git -C {worktree} submodule update --init --recursive")
    if corrupted:
        lines.append(f"  git tree rebuild {branch}")
    raise TreeError("\n".join(lines), code=4)
