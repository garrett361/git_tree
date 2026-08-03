"""The `rebuild` command: recreate a corrupted worktree from the branch tip."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from git_tree._errors import TreeError
from git_tree._git import (
    _force_remove_worktree,
    _has_active_rebase_safe,
    _init_submodules,
    _worktree_status,
    git,
    git_echo_ok,
    git_ok,
)
from git_tree._graph import discover
from git_tree._guards import _remove_blocking_dirt
from git_tree._prompt import _proceed, _require_input, _select_one

if TYPE_CHECKING:
    import argparse


def _prunable_worktree_path(branch: str) -> Path | None:
    """Path of a stale (prunable) worktree registration for `branch`, if any.

    Its directory was deleted (rm -rf'd) without `git worktree prune`, so `discover()`
    drops it and the branch looks worktree-less. `cmd_rebuild` uses this to point the user
    at recovery rather than a bare "nothing to rebuild"."""
    porcelain = git("worktree", "list", "--porcelain")
    for entry in porcelain.split("\n\n"):
        lines = entry.splitlines()
        if not any(line == f"branch refs/heads/{branch}" for line in lines):
            continue
        if not any(line.startswith("prunable") for line in lines):
            continue
        path = next((line.split(" ", 1)[1] for line in lines if line.startswith("worktree ")), None)
        if path:
            return Path(path)
    return None


def cmd_rebuild(args: argparse.Namespace) -> None:
    """Rebuild a corrupted worktree from the branch tip, preserving branch ref and tree config."""
    graph = discover()

    target = args.branch
    if target is None:
        _require_input(args, "branch to rebuild", "the branch argument")
        candidates = sorted(
            b for b in graph.parent_of if (info := graph.branches.get(b)) and info.worktree
        )
        if not candidates:
            raise TreeError("No tree-branch worktrees available to rebuild.")
        target = _select_one(
            candidates,
            prompt="Rebuild worktree> ",
            header="Select a tree-branch whose worktree to rebuild",
        )

    if target not in graph.parent_of:
        raise TreeError(
            f"git tree rebuild only acts on tree-branches; {target} has no tree-parent "
            f"(rebuild won't touch a tree root).",
            code=5,
        )

    info = graph.branches.get(target)
    if not info or not info.worktree:
        stale = _prunable_worktree_path(target)
        if stale is not None:
            raise TreeError(
                f"{target}'s worktree at {stale} is gone, but git still has a stale "
                f"registration for it. `git tree rebuild` recreates a corrupted worktree in "
                f"place; it can't resurrect a deleted directory.\n"
                f"Recover with:\n"
                f"  git worktree prune\n"
                f"  git worktree add {stale} {target}",
                code=4,
            )
        raise TreeError(f"{target} has no worktree registered. Nothing to rebuild.", code=4)

    wt_path = info.worktree

    # Refuse if cwd is inside the target worktree
    try:
        cwd = Path.cwd().resolve()
        wt = wt_path.resolve()
        if cwd.is_relative_to(wt):
            raise TreeError(
                f"Cannot rebuild {target}: your shell is inside its worktree ({wt_path}).\n"
                f"cd to a different directory first.",
                code=4,
            )
    except (OSError, ValueError):
        pass  # cwd resolution failed; proceed anyway

    force = args.force
    if not force and _has_active_rebase_safe(wt_path):
        raise TreeError(
            f"{target} has a rebase in progress in {wt_path}. Rebuilding discards it, along with "
            f"every conflict already resolved in it (the branch ref has not moved, so that work "
            f"exists only in this worktree).\nFinish it (`git tree propagate {target}`) or "
            f"`git rebase --abort` there, or re-run with --force.",
            code=4,
        )

    # Same gate `remove` uses, not a bare `git status`: that misses submodule dirt whenever
    # .gitmodules carries `ignore = all`, and misses a populated-but-uninitialized submodule
    # directory entirely. Both are real content, and rebuild deletes the worktree.
    #
    # Read the worktree's own state first, with submodules excluded so a corrupted one cannot
    # make this fail. It answers two questions. If it cannot run at all, nothing here is
    # inspectable, so refuse rather than delete blind. If the fuller gate below then fails, that
    # failure is the submodule corruption rebuild exists to repair, so fall back to this answer
    # instead of refusing: demanding --force there would demand it for rebuild's whole purpose.
    own_dirt: bool | None = None
    try:
        own_dirt = _worktree_status(
            wt_path, ignore_submodules="all", untracked_files="normal"
        ).dirty
    except subprocess.CalledProcessError as err:
        if not force:
            raise TreeError(
                f"Could not read {wt_path} at all (git status failed there), so git-tree cannot "
                f"tell whether it holds uncommitted work.\nRescue anything you need from that "
                f"directory, then re-run with --force.",
                code=4,
            ) from err
    try:
        blocked = _remove_blocking_dirt(wt_path)
    except subprocess.CalledProcessError:
        print(
            f"Warning: could not inspect submodules under {wt_path}; "
            f"checking the worktree itself only.",
            file=sys.stderr,
        )
        blocked = bool(own_dirt)
    if blocked and not force:
        raise TreeError(
            f"{target} has uncommitted changes in {wt_path} (possibly inside a submodule).\n"
            f"Pass --force to rebuild anyway (uncommitted work will be lost).",
            code=4,
        )

    # The branch's committed .gitmodules is the reliable signal: the (possibly corrupted)
    # worktree may be missing files, but the recreated one checks out the branch tip.
    has_submodules = git_ok("cat-file", "-e", f"{target}:.gitmodules")
    steps = ["Remove corrupted worktree", "Recreate worktree"]
    if has_submodules:
        steps.append("Initialize submodules")
    print(f"Rebuilding {target} at {wt_path}:")
    for i, step in enumerate(steps, 1):
        print(f"  {i}. {step}")
    print()
    if not _proceed(args, "Proceed?"):
        return

    _force_remove_worktree(wt_path, target)
    if not git_echo_ok("worktree", "add", str(wt_path), target):
        raise TreeError(f"Failed to recreate worktree at {wt_path}.")
    # Rebuild exists to make submodule state healthy, so a failed init is a failed rebuild; don't
    # claim "is healthy" over it.
    if not _init_submodules(wt_path):
        raise TreeError(
            f"Recreated {target}'s worktree at {wt_path}, but submodule init failed (see output "
            f"above). Fix the submodule issue, then re-run `git tree rebuild {target}`.",
            code=4,
        )
    print(f"\nRebuilt {target}: worktree at {wt_path} is healthy.")
