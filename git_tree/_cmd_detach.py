"""The `detach` command: drop a branch's tree edges, keeping the ref."""

from __future__ import annotations

from typing import TYPE_CHECKING

from git_tree._display import format_tree
from git_tree._errors import TreeError
from git_tree._git import (
    _carry_remote_to_root,
    _get_tree_parent,
    _unset_tree_config,
    current_branch,
)
from git_tree._graph import discover, root_of, roots
from git_tree._prompt import _proceed
from git_tree._registry import subcommand
from git_tree._render import _set_completer

if TYPE_CHECKING:
    import argparse


def arguments(p: argparse.ArgumentParser) -> None:
    _set_completer(
        p.add_argument("branch", nargs="?", help="Branch to detach (default: current)"),
        "git_heads",
    )
    p.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")


@subcommand(
    "detach",
    "Remove a branch from tree",
    arguments=arguments,
)
def cmd_detach(args: argparse.Namespace) -> None:
    branch = args.branch or current_branch()
    parent = _get_tree_parent(branch)
    if not parent:
        raise TreeError(f"{branch} is not in the tree.", code=5)

    # detach is the recovery path for hand-edited cyclic config; discover() prunes cycles and
    # returns a usable graph, so the normal child lookup and subtree preview work here too.
    graph = discover()
    children = graph.children_of.get(branch, [])

    print(f"Detaching {branch} from {parent}.")
    if children:
        print(f"{branch} has children — they will form a separate tree:")
        print(format_tree(graph, root=branch))

    if not _proceed(args, "Proceed?"):
        return

    # A tree's remote is anchored on its root, so `branch` needs one when it becomes a root of
    # something. Only when it has children: `branch.<name>.remote` is git's own key, not a
    # git-tree one, so writing it on a branch that is leaving the tree entirely would retarget
    # plain `git push`. Read the old root before unsetting the config that leads to it.
    if children:
        _carry_remote_to_root(root_of(graph, branch), branch)
    _unset_tree_config(branch)
    print(f"Detached {branch} (was child of {parent})")

    if children:
        graph = discover()
        other_roots = [r for r in roots(graph) if r != branch]
        if other_roots:
            print("\nRemaining tree(s):")
            print("\n\n".join(format_tree(graph, root=r) for r in other_roots))
