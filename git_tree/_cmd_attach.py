"""The `attach` command: record a branch's parent edge in git config."""

from __future__ import annotations

from typing import TYPE_CHECKING

from git_tree._errors import TreeError
from git_tree._git import (
    _register_child,
    _would_cycle,
    all_branch_names,
    current_branch,
    git_ok,
)
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

    _register_child(branch, parent)
    print(f"Attached {branch} to {parent}")
