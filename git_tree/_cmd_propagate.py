"""The `propagate` command: cascade to descendants, and resume an interrupted rebase."""

from __future__ import annotations

from typing import TYPE_CHECKING

from git_tree._display import _subtree_lines
from git_tree._engine import (
    _advance_branch,
    _auto_rerere,
    _propagate_descendants,
    _resume_cmd,
)
from git_tree._errors import TreeError
from git_tree._git import _has_active_rebase, current_branch
from git_tree._graph import _get_fork_commit, discover
from git_tree._guards import _require_ready
from git_tree._prompt import _proceed

if TYPE_CHECKING:
    import argparse


def cmd_propagate(args: argparse.Namespace) -> None:
    branch = args.branch or current_branch()
    graph = discover()

    descendants = graph.downstream_from(branch)
    resume_cmd = _resume_cmd(branch)

    # `propagate` is also the universal resume: if a conflict stopped an earlier cascade, the
    # user resolves + `git add` and re-runs this command, which finishes the interrupted rebase
    # before continuing. The stuck branch is either `branch` itself (a `git tree rebase` left it
    # mid-rebase) or one of its descendants.
    info = graph.branches.get(branch)
    named_mid = bool(info and info.worktree and _has_active_rebase(info.worktree))
    descendant_mid = any(
        (di := graph.branches.get(d)) and di.worktree and _has_active_rebase(di.worktree)
        for d in descendants
    )
    is_resume = named_mid or descendant_mid

    # A leaf left mid-rebase by `git tree rebase` still needs finishing, so don't take the
    # no-descendants shortcut when `branch` itself is the stuck one.
    if not descendants and not named_mid:
        print("No descendants to propagate to.")
        return

    _require_ready(descendants, graph, resume_cmd)

    print(f"Propagating from {branch}:")
    for line in _subtree_lines(graph, branch, show_counts=True):
        print(line)
    print()

    if args.dry_run:
        return
    # A resume continues an already-confirmed cascade — don't re-prompt (this also keeps the
    # agent `remedy` runnable without -y). Only a fresh propagate asks.
    if not is_resume and not _proceed(args, "Proceed?"):
        return
    auto_rerere = _auto_rerere(args)

    print()
    if named_mid:
        assert info is not None
        parent = graph.parent_of.get(branch)
        if parent is None:
            raise TreeError(
                f"{branch} is mid-rebase but has no tree-parent; finish it with "
                f"`git rebase --continue` in its worktree.",
                code=4,
            )
        _advance_branch(
            branch,
            parent,
            info,
            _get_fork_commit(branch, parent, info),
            auto_rerere=auto_rerere,
            resume_cmd=resume_cmd,
        )
        graph = discover()  # branch's tip/fork moved; re-read before cascading
    _propagate_descendants(branch, graph, auto_rerere=auto_rerere, resume_cmd=resume_cmd)
