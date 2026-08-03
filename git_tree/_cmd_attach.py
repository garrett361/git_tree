"""The `attach` command: record a branch's parent edge in git config."""

from __future__ import annotations

from typing import TYPE_CHECKING

from git_tree._errors import TreeError
from git_tree._git import _register_child, _would_cycle, all_branch_names, current_branch
from git_tree._prompt import _require_input, _select_one

if TYPE_CHECKING:
    import argparse


def cmd_attach(args: argparse.Namespace) -> None:
    branch = current_branch()
    parent: str | None = args.parent

    if not parent:
        _require_input(args, "parent branch", "the parent argument")
        candidates = [b for b in all_branch_names() if b != branch]
        if not candidates:
            raise TreeError("No other branches available.")
        parent = _select_one(candidates, prompt="Select parent> ", header="Choose parent branch")

    if parent == branch:
        raise TreeError(f"Cannot attach {branch} to itself.")
    if _would_cycle(branch, parent):
        raise TreeError(
            f"Cannot attach {branch} to {parent}: {parent} descends from {branch} "
            f"in the tree (would create a cycle)."
        )

    _register_child(branch, parent)
    print(f"Attached {branch} to {parent}")
