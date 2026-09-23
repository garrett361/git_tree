"""The `attach` command: record a branch's parent edge in git config."""

from __future__ import annotations

from typing import TYPE_CHECKING

from git_tree._errors import TreeError
from git_tree._git import (
    _get_tree_parent,
    _register_child,
    _would_cycle,
    all_branch_names,
    current_branch,
    git,
    git_ok,
)
from git_tree._graph import _get_fork_commit, _resolve_fork_arg
from git_tree._prompt import _require_input, _select_one
from git_tree._registry import subcommand
from git_tree._render import _set_completer

if TYPE_CHECKING:
    import argparse


def arguments(p: argparse.ArgumentParser) -> None:
    _set_completer(
        p.add_argument("parent", nargs="?", help="Parent branch (fzf if omitted)"),
        "git_heads",
    )
    p.add_argument(
        "--fork",
        metavar="COMMIT",
        help="Record COMMIT (an ancestor of the branch) as the fork: the next propagate or "
        "rebase replays only the commits after it. Default: merge-base with the parent, except "
        "that re-attaching to the same parent keeps a recorded fork that is still an ancestor "
        "of the branch and above the merge-base (pass the merge-base as COMMIT to reset it)",
    )


@subcommand(
    "attach",
    "Attach current branch to tree",
    arguments=arguments,
)
def cmd_attach(args: argparse.Namespace) -> None:
    branch = current_branch()
    parent: str | None = args.parent

    if not parent:
        _require_input(args, "parent branch", "the parent argument")
        candidates = [b for b in all_branch_names() if b != branch]
        if not candidates:
            raise TreeError("No other branches available.")
        parent = _select_one(candidates, prompt="Select parent> ", header="Choose parent branch")

    # A tree-parent is always a local branch: discover() drops an edge whose parent is not one and
    # reports the child as orphaned. Reject a tag, a remote-tracking ref, or a raw commit here
    # rather than writing an edge that breaks on the next command. A typo lands here too, instead
    # of reaching _register_child and being reported as "No common history".
    if not git_ok("rev-parse", "--verify", "--quiet", f"refs/heads/{parent}"):
        raise TreeError(
            f"'{parent}' is not a local branch; git-tree can only attach to a local branch. "
            f"Create it first, or pick an existing branch.",
            code=4,
        )

    if parent == branch:
        raise TreeError(f"Cannot attach {branch} to itself.")
    if _would_cycle(branch, parent):
        raise TreeError(
            f"Cannot attach {branch} to {parent}: {parent} descends from {branch} "
            f"in the tree (would create a cycle)."
        )

    if args.fork is None:
        kept_fork = None
        if _get_tree_parent(branch) == parent:
            recorded = _get_fork_commit(branch, parent)
            merge_base = git("merge-base", parent, branch, check=False)
            if (
                recorded
                and merge_base
                and not git_ok("merge-base", "--is-ancestor", recorded, merge_base)
            ):
                kept_fork = recorded
        _register_child(branch, parent, fork=kept_fork)
        kept_note = f" (kept fork {kept_fork[:9]})" if kept_fork else ""
        print(f"Attached {branch} to {parent}{kept_note}")
        return
    fork = _resolve_fork_arg(branch, args.fork)
    _register_child(branch, parent, fork=fork, warn_if_not_descendant=False)
    print(f"Attached {branch} to {parent} (fork {fork[:9]})")
