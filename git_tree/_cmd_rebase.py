"""The `rebase` command: reparent a branch onto a new base, then cascade."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from git_tree._display import _subtree_lines
from git_tree._engine import (
    _auto_rerere,
    _propagate_descendants,
    _rebase_branch,
    _resume_cmd,
)
from git_tree._errors import TreeError
from git_tree._git import (
    _active_rebase_branch,
    _carry_remote_to_root,
    _get_tree_parent,
    _has_active_rebase,
    _is_git_tree_rebase,
    _would_cycle,
    current_branch,
    git,
    git_lines,
    git_ok,
)
from git_tree._graph import _get_fork_commit, discover, root_of
from git_tree._guards import (
    _mid_rebase_branches,
    _require_clean_state,
    _require_healthy_submodules,
    _require_ready,
)
from git_tree._prompt import _proceed

if TYPE_CHECKING:
    import argparse


def cmd_rebase(args: argparse.Namespace) -> None:
    if args.branch is not None:
        branch = args.branch
        # Naming a branch that doesn't exist would otherwise surface as "no tree-parent-branch
        # configured", which reads as a tree problem rather than the typo it is.
        if not git_ok("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"):
            raise TreeError(f"No such branch: {branch}", code=4)
    else:
        try:
            branch = current_branch()
        except TreeError:
            # A detached HEAD here usually means a prior `git tree rebase` conflicted and left this
            # worktree mid-rebase. Rebase is never the resume verb, so point at the propagate that
            # is (naming the stuck branch). `git_ok` guards the non-repo case so we don't mask it.
            cwd = Path.cwd()
            if git_ok("rev-parse", "--git-dir", cwd=cwd) and _has_active_rebase(cwd):
                stuck = _active_rebase_branch(cwd)
                hint = f"git tree propagate {stuck}" if stuck else "git tree propagate <branch>"
                raise TreeError(
                    "This worktree is mid-rebase from an earlier `git tree rebase`. Resolve the "
                    f"conflicts and `git add`, then resume with: {hint}",
                    code=4,
                ) from None
            raise
    target: str = args.target
    graph = discover()
    resume_cmd = _resume_cmd(branch)

    old_parent = graph.parent_of.get(branch) or _get_tree_parent(branch)
    if not old_parent:
        raise TreeError(f"{branch} has no tree-parent-branch configured.", code=5)

    if not git_ok("rev-parse", "--verify", old_parent):
        raise TreeError(f"Old parent {old_parent} does not exist.")

    # The target becomes branch's tree-parent, and tree-parents are always local branches
    # (discover() flags any non-branch parent as orphaned). Reject a tag/commit/remote-tracking
    # ref up front rather than writing an edge the next discover() would break.
    if not git_ok("rev-parse", "--verify", "--quiet", f"refs/heads/{target}"):
        raise TreeError(
            f"Rebase target '{target}' is not a local branch; git-tree can only reparent onto "
            f"a local branch. Create it first, or pick an existing branch.",
            code=4,
        )

    if target == branch:
        raise TreeError(f"Cannot rebase {branch} onto itself.")
    if _would_cycle(branch, target):
        raise TreeError(
            f"Cannot rebase {branch} onto {target}: {target} descends from {branch} "
            f"in the tree (would create a cycle)."
        )

    fork_point = _get_fork_commit(branch, old_parent, graph.branches.get(branch))
    commit_count = len(git_lines("rev-list", f"{fork_point}..{branch}"))

    descendants = graph.downstream_from(branch)

    siblings = [b for b, p in graph.parent_of.items() if p == old_parent and b != branch]

    info = graph.branches.get(branch)
    if not info or not info.worktree:
        raise TreeError(
            f"{branch} needs a worktree. Add one with: git worktree add <path> {branch}",
            code=4,
        )
    # Naming a branch reaches a state the current-branch form cannot: a mid-rebase worktree has a
    # detached HEAD, so `current_branch()` fails there and the hint above fires instead. Reached by
    # name, the reparent below would commit and the rebase would then drive the in-progress rebase
    # to completion, `--skip`ping past the very commits being replayed. `_require_clean_state` does
    # not cover this: it admits a resolved git-tree mid-rebase as a resume point, by design.
    if _has_active_rebase(info.worktree):
        # Same predicate the cascade uses, so the advice below cannot send the user to a
        # `propagate` that then refuses this very worktree as foreign.
        ours = _is_git_tree_rebase(info.worktree, old_parent)
        fix = (
            f"resolve the conflicts and `git add` them, then run: {' '.join(resume_cmd)}"
            if ours
            else "finish it or `git rebase --abort` first"
        )
        raise TreeError(
            f"{branch} is mid-rebase in {info.worktree}; rebase is not the resume verb. {fix}",
            code=4,
        )

    # Rewriting `branch` invalidates any rebase in progress below it: its base is about to stop
    # being an ancestor of its parent, so the cascade would reach it and refuse it as a rebase
    # git-tree did not start, with no way back to this state. `_require_ready` below admits such a
    # rebase as a resume point, which is right for `propagate` and wrong here, so refuse while it
    # can still be finished, and before the reparent write.
    if stopped := _mid_rebase_branches(descendants, graph):
        lines = [f"Rebasing {branch} would invalidate a rebase already in progress below it:"]
        for d, wt in stopped:
            fix = (
                f"finish it with: git tree propagate {d}"
                if _is_git_tree_rebase(wt, graph.parent_of.get(d))
                else "not started by git-tree: finish it or `git rebase --abort` there"
            )
            lines.append(f"  {d}  (in: {wt}) {fix}")
        lines.append(f"\nThen re-run: git tree rebase {target} {branch}")
        raise TreeError("\n".join(lines), code=4, branches=[d for d, _ in stopped])

    _require_healthy_submodules([branch], graph)
    _require_clean_state([branch], graph, resume_cmd)
    if descendants:
        _require_ready(descendants, graph, resume_cmd)

    print(f"Rebasing onto {target}:")
    print(f"  {branch}  [{commit_count} commits]  (old parent: {old_parent})")

    if descendants:
        print()
        print("Will propagate to:")
        for line in _subtree_lines(graph, branch):
            print(f"  {line}")

    if siblings:
        print()
        print(f"Warning: these branches also have {old_parent} as parent (will NOT be updated):")
        for s in siblings:
            print(f"  {s}")

    print()
    if args.dry_run or not _proceed(args, "Proceed?"):
        return

    auto_rerere = _auto_rerere(args)

    # Reparent + carry the remote BEFORE the rebase so a conflict leaves config pointing at
    # the intended new parent. That makes a conflict resumable the same way any propagate conflict
    # is: `git tree propagate <branch>` (the reparent is already committed, so propagate finishes
    # branch's own rebase onto target, then cascades to descendants).
    # Reparenting onto an out-of-tree target re-roots branch's tree; both roots come from the
    # pre-rebase in-memory graph, which the config write doesn't perturb.
    git("config", f"branch.{branch}.tree-parent-branch", target)
    _carry_remote_to_root(root_of(graph, branch), root_of(graph, target))
    r = _rebase_branch(
        branch, target, fork_point, info, auto_rerere=auto_rerere, resume_cmd=resume_cmd
    )
    if r.unpopped_stash:
        print(
            f"Warning: could not pop worktree stash; your changes are still in it. Restore them "
            f"with: cd {info.worktree} && git stash apply {r.unpopped_stash}",
            file=sys.stderr,
        )
    print(f"Rebased {branch} onto {target}")

    if descendants:
        print()
        print("Cascading to descendants...")
        _propagate_descendants(branch, graph, auto_rerere=auto_rerere, resume_cmd=resume_cmd)
