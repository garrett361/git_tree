"""The `remove` command: drop a subtree's worktrees and unregister its branches."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from git_tree._display import format_tree
from git_tree._errors import TreeError
from git_tree._git import (
    _force_remove_worktree,
    _has_active_rebase_safe,
    _unset_tree_config,
    current_branch,
)
from git_tree._graph import discover
from git_tree._guards import _remove_blocking_dirt
from git_tree._prompt import _proceed, _require_input, _select_one

if TYPE_CHECKING:
    import argparse


def cmd_remove(args: argparse.Namespace) -> None:
    """Tear down a subtree's worktrees and unregister its branches from the tree.

    Removes worktree directories and unsets tree config; it never deletes a branch ref, so no
    committed work can be lost. Uncommitted work IS at risk (worktrees are force-removed), so by
    default it refuses if any worktree or submodule is dirty; `--force` overrides that (and
    destroys the uncommitted work, including inside submodules and git-ignored files).
    """
    graph = discover()
    try:
        cur: str | None = current_branch()
    except TreeError:
        cur = None

    target = args.branch
    if target is None:
        _require_input(args, "branch to remove", "the branch argument")
        # No branch given: pick from removable tree-branches that have a worktree. The
        # picker doesn't pre-filter dirty ones — the clean gate below still catches them.
        candidates = sorted(
            b
            for b in graph.parent_of
            if b != cur and (info := graph.branches.get(b)) and info.worktree
        )
        if not candidates:
            raise TreeError("No tree-branch worktrees available to remove.")
        target = _select_one(
            candidates,
            prompt="Remove worktree> ",
            header="Select a tree-branch to remove (its worktree + subtree)",
        )

    # Only non-root tree-branches: this never touches a tree's trunk / main worktree.
    if target not in graph.parent_of:
        raise TreeError(
            f"{target} is not a removable tree-branch — it has no tree-parent "
            f"(git tree remove won't touch a tree root).",
            code=5,
        )

    subtree = [target] + graph.downstream_from(target)  # parents-first

    if cur in subtree:
        raise TreeError(
            f"cannot remove {cur}: it's the branch you're on. "
            f"Switch to a branch outside the subtree first."
        )

    force = args.force

    # cwd guard: force-removal deletes the directory outright (bypassing git's "can't delete the
    # tree you're standing in" protection), and a following `git worktree prune` from a deleted
    # cwd errors. Refuse if the shell is inside any worktree being removed.
    try:
        cwd = Path.cwd().resolve()
        inside = [
            b
            for b in subtree
            if (info := graph.branches.get(b))
            and info.worktree
            and cwd.is_relative_to(info.worktree.resolve())
        ]
        if inside:
            raise TreeError(
                f"Your shell is inside a worktree being removed ({', '.join(inside)}). "
                f"cd to a different directory first.",
                code=4,
                branches=inside,
            )
    except (OSError, ValueError):
        pass  # cwd resolution failed; proceed

    # A stopped rebase is usually dirty, so the gate below caught this by accident rather than on
    # purpose. It stops at a clean point often enough to matter (an --exec failure, git-tree's own
    # empty-patch stop), and then the worktree holds the only reference to every conflict already
    # resolved in that rebase: the branch ref hasn't moved, so the work lives on the detached HEAD
    # and in the worktree's own HEAD reflog, both of which go with the directory.
    mid_rebase = [
        b
        for b in subtree
        if (info := graph.branches.get(b))
        and info.worktree
        and _has_active_rebase_safe(info.worktree)
    ]
    if mid_rebase and not force:
        raise TreeError(
            "Refusing to remove: a rebase is in progress in:\n"
            + "\n".join(f"  {b}  ({graph.branches[b].worktree})" for b in mid_rebase)
            + "\n\nFinish it (`git tree propagate <branch>`) or `git rebase --abort` there, or "
            "re-run with --force to discard it. Nothing was removed.",
            code=4,
            branches=mid_rebase,
        )

    # Safety gate (all-or-nothing): worktrees are force-removed, so uncommitted work (in the
    # worktree OR any submodule at any depth) is at risk. Refuse unless --force. Branch refs are
    # kept, so committed work is never at risk.
    dirty = [
        b
        for b in subtree
        if (info := graph.branches.get(b))
        and info.worktree
        and _remove_blocking_dirt(info.worktree)
    ]
    if dirty and not force:
        lines = [
            "Refusing to remove: these worktrees have uncommitted changes "
            "(possibly inside a submodule):"
        ]
        lines += [f"  {b}  ({graph.branches[b].worktree})" for b in dirty]
        lines.append(
            "\nCommit, stash, or discard them, or re-run with --force to remove anyway. "
            "Nothing was removed."
        )
        raise TreeError("\n".join(lines), code=4, branches=dirty)

    print(f"Removing worktrees and unregistering {target} + its subtree (branch refs kept):")
    print(format_tree(graph, root=target))
    # Said on every path, not just --force: `git status` never reports ignored files, so they are
    # deleted without ever having been counted as dirt. `.env` files and venvs live here.
    print("\nThis deletes each worktree directory, git-ignored files included.")
    if dirty:  # implies force
        print("\nWarning: --force will destroy uncommitted changes (and any git-ignored files) in:")
        for b in dirty:
            print(f"  {b}  ({graph.branches[b].worktree})")
    if mid_rebase:  # implies force
        print("\nWarning: --force will discard the rebase in progress in:")
        for b in mid_rebase:
            print(f"  {b}  ({graph.branches[b].worktree})")
    print()
    if args.dry_run:
        return
    if not _proceed(args, "Remove these worktrees and detach the branches?"):
        return

    # Re-scan once, all-or-nothing, before deleting anything: closes the check->delete window
    # that plain `git worktree remove` used to guard (a worktree could go dirty during the
    # prompt). --force already opted out of the gate.
    if not force:
        late = [
            b
            for b in subtree
            if (info := graph.branches.get(b))
            and info.worktree
            and (_remove_blocking_dirt(info.worktree) or _has_active_rebase_safe(info.worktree))
        ]
        if late:
            raise TreeError(
                "These worktrees became dirty after confirmation; nothing was removed:\n"
                + "\n".join(f"  {b}  ({graph.branches[b].worktree})" for b in late),
                code=4,
                branches=late,
            )

    # Children-first. `_force_remove_worktree` handles submodule worktrees git refuses to remove
    # and raises TreeError on genuine failure (so removal stops rather than report false success).
    removed_worktrees = 0
    for b in reversed(subtree):
        info = graph.branches.get(b)
        if info and info.worktree:
            _force_remove_worktree(info.worktree, b)
            removed_worktrees += 1
        _unset_tree_config(b)

    print(
        f"\nDetached {len(subtree)} branch(es) from the tree; "
        f"removed {removed_worktrees} worktree(s). Branch refs kept."
    )
